"""Extraction tests. No GPU: the Client protocol is the only seam that needs one."""

import json

import pytest

from inhouse.extract import (
    MAX_DOCUMENT_CHARS,
    Extraction,
    ExtractionError,
    build_prefix,
    build_prompt,
    extract_day,
    extract_one,
    load_schema,
    read_manifest,
    to_jsonl,
)
from inhouse.parse import Filing

SCHEMA = load_schema()

VALID = {
    "event_type": "earnings",
    "direction": None,
    "summary": "Best Buy released second quarter results. The figures are in the attached release.",
    "materiality": "medium",
    "entities": ["Best Buy Co., Inc."],
    "amounts": [],
    "themes": ["consumer electronics retail"],
    "facts_in_exhibit": True,
}


class FakeClient:
    """Records prompts so prefix stability can be asserted."""

    def __init__(self, response=None, error=None):
        self.response = json.dumps(response if response is not None else VALID)
        self.error = error
        self.prompts = []
        self.schemas = []

    def generate(self, prompt, schema):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.error:
            raise self.error
        return self.response


def filing(text="Item 2.02 Results of Operations. The company reported results.",
           accession="0000000000-26-000001"):
    return Filing(accession=accession, form="8-K", text=text)


# --- the cached prefix -----------------------------------------------------


def test_prefix_is_byte_identical_across_calls():
    """Day 4's throughput depends on this. A varying prefix costs the cache
    silently -- the pipeline still works, just slower."""
    assert build_prefix() == build_prefix()


def test_every_prompt_starts_with_the_same_prefix():
    client = FakeClient()
    prefix = build_prefix()
    for i in range(3):
        extract_one(filing(text=f"Item 8.01 filing number {i}."), client, SCHEMA)

    assert len(client.prompts) == 3
    for prompt in client.prompts:
        assert prompt.startswith(prefix)
    # The prefixes are identical; only the tails differ.
    heads = {p[: len(prefix)] for p in client.prompts}
    assert len(heads) == 1


def test_prompt_contains_the_filing_text():
    prompt, truncated = build_prompt(filing(text="Item 5.02 CFO departed."))
    assert "Item 5.02 CFO departed." in prompt
    assert not truncated


def test_long_filings_are_truncated_from_the_tail():
    """8-Ks state the event first and trail into exhibit lists and signatures."""
    head = "Item 1.01 the important part. "
    long_text = head + ("x" * (MAX_DOCUMENT_CHARS + 5_000))
    prompt, truncated = build_prompt(filing(text=long_text))
    assert truncated
    assert head in prompt
    assert len(prompt) < len(build_prefix()) + MAX_DOCUMENT_CHARS + 100


# --- extraction ------------------------------------------------------------


def test_extract_one_returns_parsed_data_and_provenance():
    result = extract_one(filing(), FakeClient(), SCHEMA, model="Qwen2.5-7B-AWQ")
    assert result.data["event_type"] == "earnings"
    assert result.model == "Qwen2.5-7B-AWQ"
    assert result.extracted_at.startswith("20")
    assert result.latency_s >= 0


def test_schema_is_passed_to_the_client():
    """Constrained decoding is the whole claim; it lives in this parameter."""
    client = FakeClient()
    extract_one(filing(), client, SCHEMA)
    assert client.schemas[0] is SCHEMA
    assert "event_type" in client.schemas[0]["properties"]


def test_non_json_output_raises_with_a_pointed_message():
    """Constrained decoding makes this impossible -- if it fires, the schema
    was not actually applied, which is worth failing loudly."""
    client = FakeClient()
    client.response = "Sure! Here is the JSON you asked for:"
    with pytest.raises(ExtractionError, match="json_schema"):
        extract_one(filing(), client, SCHEMA)


def test_missing_required_fields_raise():
    partial = {"event_type": "earnings", "summary": "x" * 50}
    with pytest.raises(ExtractionError, match="missing required fields"):
        extract_one(filing(), FakeClient(response=partial), SCHEMA)


# --- manifest reading ------------------------------------------------------


MANIFEST = "\n".join(json.dumps(r) for r in [
    {"accession": "a1", "form": "8-K", "s3_key": "raw/d/a1.txt"},
    {"accession": "a2", "form": "4", "s3_key": "raw/d/a2.txt"},
    # One Form 4 document, two rows -- issuer and insider.
    {"accession": "a2", "form": "4", "s3_key": "raw/d/a2.txt"},
    {"accession": "a3", "form": "8-K", "s3_key": "raw/d/a3.txt"},
]) + "\n"


def test_read_manifest_filters_by_form():
    assert [r["accession"] for r in read_manifest(MANIFEST, "8-K")] == ["a1", "a3"]


def test_read_manifest_deduplicates_shared_documents():
    """A Form 4 appears once per party; extraction wants the document once."""
    assert len(read_manifest(MANIFEST, "4")) == 1


def test_read_manifest_tolerates_blank_lines():
    assert len(read_manifest(MANIFEST + "\n\n", "8-K")) == 2


# --- day run ---------------------------------------------------------------


DOCS = {
    "raw/d/a1.txt": b"""<SEC-HEADER>x
</SEC-HEADER>
<DOCUMENT>
<TYPE>8-K
<TEXT>
<html><body><p>Item 2.02 Results of Operations. Revenue rose.</p></body></html>
</TEXT>
</DOCUMENT>""",
}
DOCS["raw/d/a3.txt"] = DOCS["raw/d/a1.txt"]


def test_extract_day_processes_every_row():
    rows = read_manifest(MANIFEST, "8-K")
    run = extract_day("2026-08-27", rows, DOCS.__getitem__, FakeClient(), schema=SCHEMA)
    assert run.ok == 2 and not run.failures


def test_a_failed_filing_does_not_sink_the_run():
    """One bad document should not cost the other several hundred."""
    rows = read_manifest(MANIFEST, "8-K")
    docs = dict(DOCS)
    docs["raw/d/a1.txt"] = b"<SEC-HEADER>x</SEC-HEADER>"  # no primary document
    run = extract_day("2026-08-27", rows, docs.__getitem__, FakeClient(), schema=SCHEMA)
    assert run.ok == 1
    assert [a for a, _ in run.failures] == ["a1"]


def test_limit_caps_the_run():
    rows = read_manifest(MANIFEST, "8-K")
    run = extract_day("2026-08-27", rows, DOCS.__getitem__, FakeClient(),
                      schema=SCHEMA, limit=1)
    assert run.ok == 1


def test_output_rows_carry_model_and_timestamp():
    """Day 5 needs to know which rows came from which schema version."""
    rows = read_manifest(MANIFEST, "8-K")
    run = extract_day("2026-08-27", rows, DOCS.__getitem__, FakeClient(),
                      schema=SCHEMA, model="Qwen2.5-7B-AWQ")
    row = json.loads(to_jsonl(run).splitlines()[0])
    assert row["model"] == "Qwen2.5-7B-AWQ"
    assert "extracted_at" in row and "accession" in row
    assert row["event_type"] == "earnings"


def test_jsonl_output_is_sorted_and_one_object_per_line():
    rows = read_manifest(MANIFEST, "8-K")
    run = extract_day("2026-08-27", rows, DOCS.__getitem__, FakeClient(), schema=SCHEMA)
    lines = to_jsonl(run).splitlines()
    accs = [json.loads(l)["accession"] for l in lines]
    assert accs == sorted(accs)


# --- schema sanity ---------------------------------------------------------


def test_schema_and_prompt_agree_on_field_names():
    """A prompt that teaches fields the schema forbids produces constant retries."""
    prompt = build_prefix()
    for name in SCHEMA["required"]:
        assert f'"{name}"' in prompt, f"{name} missing from few-shot examples"


def test_every_few_shot_example_validates_against_the_schema():
    import re

    allowed = set(SCHEMA["properties"])
    required = set(SCHEMA["required"])
    enum = set(SCHEMA["properties"]["event_type"]["enum"])

    blocks = re.findall(r"JSON:\n(\{.*?\})\n\n---", build_prefix(), re.S)
    assert len(blocks) >= 3, "expected several few-shot examples"
    for block in blocks:
        obj = json.loads(" ".join(block.split()))
        assert required <= obj.keys()
        assert obj.keys() <= allowed
        assert obj["event_type"] in enum
