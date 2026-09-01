"""Extraction: filings in, schema-valid JSON out.

The submission side of the pipeline. Concurrency is SGLang's job -- continuous
batching packs concurrent requests into steps -- so what this builds is the
queue feeding it.

    manifest -> parse -> PREFIX + document -> SGLang -> JSON -> storage

The one thing here that matters for throughput is that PREFIX is byte-identical
on every request. It is ~1,400 tokens of schema and few-shot examples; the first
document pays for it and the rest hit prefix cache. Interpolating anything into
it -- a date, a company name, re-serialised JSON -- silently costs that, and the
pipeline still works, just slower. Hence `build_prefix` is pure and cached.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Protocol

from .parse import Filing, ParseError, parse_filing

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_SCHEMA = CONFIG_DIR / "schema.json"
DEFAULT_PROMPT = CONFIG_DIR / "prompt.txt"

# Leaves room for the model's own output within a 7B's context while staying
# well clear of the T4's KV cache budget. p90 of the corpus is ~9k characters,
# so this truncates only a handful of filings per day.
MAX_DOCUMENT_CHARS = 24_000


class ExtractionError(Exception):
    """The model did not return usable output for a filing."""


class Client(Protocol):
    """Anything that can turn a prompt into JSON text.

    Kept narrow deliberately: it is the only part of extraction that needs a
    GPU, so everything else stays testable without one.
    """

    def generate(self, prompt: str, schema: dict) -> str: ...


@dataclass
class Extraction:
    accession: str
    data: dict
    model: str
    extracted_at: str
    prompt_chars: int
    latency_s: float
    truncated: bool = False

    def to_row(self) -> dict:
        """Flattened for the manifest/JSONL. Day 5 maps this onto `extractions`."""
        return {
            "accession": self.accession,
            **self.data,
            "model": self.model,
            "extracted_at": self.extracted_at,
            "latency_s": round(self.latency_s, 3),
            "truncated": self.truncated,
        }


@dataclass
class ExtractionRun:
    day: str
    results: list[Extraction] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return len(self.results)


# --- the cached prefix -----------------------------------------------------


@lru_cache(maxsize=4)
def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_schema(path: Path | str = DEFAULT_SCHEMA) -> dict:
    return json.loads(_read(str(path)))


@lru_cache(maxsize=4)
def build_prefix(prompt_path: str = str(DEFAULT_PROMPT)) -> str:
    """The shared prompt prefix, identical on every request.

    Cached so repeated calls return the same object rather than re-reading and
    risking a difference. Nothing filing-specific may be added here.
    """
    return _read(prompt_path)


def build_prompt(filing: Filing, prefix: str | None = None) -> tuple[str, bool]:
    """Prefix plus one filing's text. Returns (prompt, truncated)."""
    prefix = build_prefix() if prefix is None else prefix
    text = filing.text
    truncated = len(text) > MAX_DOCUMENT_CHARS
    if truncated:
        # Keep the head: an 8-K states its event in the first paragraphs and
        # trails into exhibit lists and signatures.
        text = text[:MAX_DOCUMENT_CHARS]
        log.warning("%s truncated to %d chars", filing.accession, MAX_DOCUMENT_CHARS)
    return prefix + text + "\n\nJSON:\n", truncated


# --- extraction ------------------------------------------------------------


def extract_one(
    filing: Filing,
    client: Client,
    schema: dict,
    *,
    model: str = "unknown",
    prefix: str | None = None,
) -> Extraction:
    """One filing through the model. Raises ExtractionError on unusable output."""
    prompt, truncated = build_prompt(filing, prefix)

    started = time.monotonic()
    raw = client.generate(prompt, schema)
    latency = time.monotonic() - started

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Constrained decoding should make this impossible. If it fires, the
        # schema was not actually applied -- worth failing loudly rather than
        # silently dropping the filing.
        raise ExtractionError(
            f"{filing.accession}: model returned non-JSON ({exc}). "
            f"Is json_schema being passed to the server?"
        ) from exc

    missing = set(schema.get("required", [])) - data.keys()
    if missing:
        raise ExtractionError(f"{filing.accession}: missing required fields {sorted(missing)}")

    return Extraction(
        accession=filing.accession,
        data=data,
        model=model,
        # Recorded on every row: when the schema changes -- and it will -- you
        # need to know which rows came from which version.
        extracted_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        prompt_chars=len(prompt),
        latency_s=latency,
        truncated=truncated,
    )


def read_manifest(body: str, form: str = "8-K") -> list[dict]:
    """Manifest rows for one form type, de-duplicated by document.

    A Form 4 appears once per party to the filing, so the manifest holds several
    rows per document. Extraction wants each document once.
    """
    seen, out = set(), []
    for line in body.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("form") != form or row["s3_key"] in seen:
            continue
        seen.add(row["s3_key"])
        out.append(row)
    return out


def extract_day(
    day: str,
    rows: Iterable[dict],
    load_document,
    client: Client,
    *,
    schema: dict | None = None,
    model: str = "unknown",
    limit: int | None = None,
) -> ExtractionRun:
    """Run extraction over a day's filings.

    `load_document(s3_key) -> bytes` keeps this independent of storage, so the
    same code runs against local files or S3.

    A filing that fails is logged and skipped: one bad document should not cost
    the other several hundred, and the failures are reported so they can be
    re-run.
    """
    schema = schema or load_schema()
    prefix = build_prefix()
    run = ExtractionRun(day=day)

    rows = list(rows)
    if limit is not None:
        rows = rows[:limit]

    for row in rows:
        accession = row["accession"]
        try:
            filing = parse_filing(load_document(row["s3_key"]), accession=accession)
            result = extract_one(filing, client, schema, model=model, prefix=prefix)
        except (ParseError, ExtractionError) as exc:
            log.error("extraction failed for %s: %s", accession, exc)
            run.failures.append((accession, str(exc)))
            continue
        run.results.append(result)

    return run


def to_jsonl(run: ExtractionRun) -> str:
    return "".join(
        json.dumps(r.to_row(), separators=(",", ":")) + "\n"
        for r in sorted(run.results, key=lambda r: r.accession)
    )
