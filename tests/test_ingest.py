"""End-to-end ingest against a stub EDGAR, exercising the real manifest path."""

import json
from datetime import date
from pathlib import Path

import pytest

from inhouse.config import Config
from inhouse.edgar import EdgarError, Response
from inhouse.ingest import ingest
from inhouse.storage import LocalStorage

DAY = date(2026, 8, 26)

INDEX = """Form Type   Company Name                         CIK         Date Filed  File Name
--------------------------------------------------------------------------------------
4           DOE JOHN A                           1500001     2026-08-26  edgar/data/1500001/0001500001-26-000124.xml
8-K         APPLE INC                            320193      2026-08-26  edgar/data/320193/0000320193-26-000123.txt
10-K        IGNORED CORP                         111111      2026-08-26  edgar/data/111111/0000111111-26-000002.txt
"""

SUBMISSIONS = {
    "0000320193": {"sic": "3571", "sicDescription": "Electronic Computers", "name": "Apple Inc."},
    # Individual insiders filing Form 4s have no SIC, which is normal.
    "0001500001": {"sic": "", "sicDescription": "", "name": "Doe John A"},
}


class FakeClient:
    """Stands in for EdgarClient, recording what was requested."""

    def __init__(self, *, fail_paths=()):
        self.fail_paths = set(fail_paths)
        self.document_calls = []
        self.submission_calls = []

    def daily_index(self, day):
        return INDEX

    def document(self, path):
        self.document_calls.append(path)
        if path in self.fail_paths:
            raise EdgarError(f"{path} -> HTTP 404")
        return f"CONTENT OF {path}".encode()

    def submissions(self, cik):
        self.submission_calls.append(cik)
        payload = SUBMISSIONS.get(cik.zfill(10))
        if payload is None:
            raise EdgarError("not found")
        return Response(200, json.dumps(payload).encode())


@pytest.fixture
def config():
    return Config(
        user_agent="Test test@example.com",
        storage_uri="./data",
        rate_limit=1000.0,
        max_concurrency=2,
        form_types=("8-K", "4"),
    )


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "store")


def run(config, storage, tmp_path, client=None, **kwargs):
    return ingest(
        DAY, config, storage,
        client=client or FakeClient(),
        cache_path=tmp_path / "sic.json",
        **kwargs,
    )


def read_manifest(storage) -> list[dict]:
    text = Path(storage.uri("manifest/2026-08-26.jsonl")).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_stores_raw_documents_under_dated_prefix(config, storage, tmp_path):
    run(config, storage, tmp_path)
    root = Path(storage.root) / "raw" / "2026-08-26"
    assert sorted(p.name for p in root.iterdir()) == [
        "0000320193-26-000123.txt",
        "0001500001-26-000124.xml",
    ]


def test_documents_are_stored_byte_for_byte(config, storage, tmp_path):
    run(config, storage, tmp_path)
    stored = Path(storage.uri("raw/2026-08-26/0000320193-26-000123.txt")).read_bytes()
    assert stored == b"CONTENT OF edgar/data/320193/0000320193-26-000123.txt"


def test_only_requested_form_types_are_fetched(config, storage, tmp_path):
    client = FakeClient()
    run(config, storage, tmp_path, client=client)
    assert not any("111111" in p for p in client.document_calls)


def test_manifest_has_one_row_per_filing_with_expected_fields(config, storage, tmp_path):
    result = run(config, storage, tmp_path)
    rows = read_manifest(storage)

    assert len(rows) == 2 == result.stored
    apple = next(r for r in rows if r["form"] == "8-K")
    assert apple == {
        "accession": "0000320193-26-000123",
        "cik": "0000320193",
        "company": "Apple Inc.",
        "form": "8-K",
        "filed_at": "2026-08-26",
        "sic": "3571",
        "sic_description": "Electronic Computers",
        "s3_key": "raw/2026-08-26/0000320193-26-000123.txt",
        "source_url": "https://www.sec.gov/Archives/edgar/data/320193/0000320193-26-000123.txt",
        "bytes": len(b"CONTENT OF edgar/data/320193/0000320193-26-000123.txt"),
    }


def test_filers_without_a_sic_still_appear_in_the_manifest(config, storage, tmp_path):
    run(config, storage, tmp_path)
    form4 = next(r for r in read_manifest(storage) if r["form"] == "4")
    assert form4["sic"] is None
    assert form4["s3_key"].endswith(".xml")


def test_sic_is_fetched_once_per_cik_not_once_per_filing(config, storage, tmp_path):
    client = FakeClient()
    run(config, storage, tmp_path, client=client)
    assert sorted(client.submission_calls) == ["0000320193", "0001500001"]


def test_sic_cache_persists_across_runs(config, storage, tmp_path):
    run(config, storage, tmp_path)
    second = FakeClient()
    run(config, storage, tmp_path, client=second)
    assert second.submission_calls == []


def test_rerun_skips_documents_already_stored(config, storage, tmp_path):
    run(config, storage, tmp_path)
    second = FakeClient()
    result = run(config, storage, tmp_path, client=second)
    assert second.document_calls == []
    assert result.skipped == 2 and result.fetched == 0
    # A skipped document still belongs in the manifest.
    assert len(read_manifest(storage)) == 2


def test_force_redownloads(config, storage, tmp_path):
    run(config, storage, tmp_path)
    second = FakeClient()
    result = run(config, storage, tmp_path, client=second, force=True)
    assert len(second.document_calls) == 2 and result.fetched == 2


def test_one_failed_document_does_not_sink_the_run(config, storage, tmp_path):
    client = FakeClient(fail_paths={"edgar/data/320193/0000320193-26-000123.txt"})
    result = run(config, storage, tmp_path, client=client)
    assert result.failed == 1
    rows = read_manifest(storage)
    assert [r["form"] for r in rows] == ["4"]


def test_manifest_is_byte_stable_across_runs(config, storage, tmp_path):
    run(config, storage, tmp_path)
    first = Path(storage.uri("manifest/2026-08-26.jsonl")).read_bytes()
    run(config, storage, tmp_path, force=True)
    assert Path(storage.uri("manifest/2026-08-26.jsonl")).read_bytes() == first


def test_limit_caps_the_number_of_filings(config, storage, tmp_path):
    result = run(config, storage, tmp_path, limit=1)
    assert result.stored == 1


def test_skipped_filings_keep_their_size_and_timestamp(config, storage, tmp_path):
    """A re-run must not degrade manifest fields for documents it skipped."""
    run(config, storage, tmp_path)
    first = {r["accession"]: r for r in read_manifest(storage)}
    run(config, storage, tmp_path, client=FakeClient())
    second = {r["accession"]: r for r in read_manifest(storage)}
    assert first == second
    assert all(r["bytes"] > 0 for r in second.values())


def test_filed_at_uses_the_sgml_acceptance_timestamp(config, storage, tmp_path):
    class Stamped(FakeClient):
        def document(self, path):
            self.document_calls.append(path)
            return (
                b"<SEC-DOCUMENT>x.txt : 20260826\n"
                b"<ACCEPTANCE-DATETIME>20260826163200\n"
                b"rest of filing"
            )

    run(config, storage, tmp_path, client=Stamped())
    assert all(r["filed_at"] == "2026-08-26T16:32:00" for r in read_manifest(storage))


def test_filed_at_falls_back_to_the_index_date(config, storage, tmp_path):
    """Documents without an SGML header still get a usable filed_at."""
    run(config, storage, tmp_path)
    assert all(r["filed_at"] == "2026-08-26" for r in read_manifest(storage))


# EDGAR lists one submission once per party to it: a Form 4 appears under the
# issuer's CIK and again under the insider's, with the same accession number.
MULTI_PARTY_INDEX = """Form Type   Company Name                         CIK         Date Filed  File Name
--------------------------------------------------------------------------------------
4           BARBEE ANGELA                        1915962     2026-08-26  edgar/data/1915962/0000058492-26-000491.txt
4           LEGGETT & PLATT INC                  58492       2026-08-26  edgar/data/58492/0000058492-26-000491.txt
8-K         APPLE INC                            320193      2026-08-26  edgar/data/320193/0000320193-26-000123.txt
"""


class MultiPartyClient(FakeClient):
    def daily_index(self, day):
        return MULTI_PARTY_INDEX

    def submissions(self, cik):
        self.submission_calls.append(cik)
        names = {
            "0001915962": {"sic": "", "sicDescription": "", "name": "Barbee Angela"},
            "0000058492": {"sic": "2510", "sicDescription": "Household Furniture", "name": "Leggett & Platt"},
            "0000320193": SUBMISSIONS["0000320193"],
        }
        return Response(200, json.dumps(names[cik.zfill(10)]).encode())


def test_shared_accession_is_downloaded_once(config, storage, tmp_path):
    client = MultiPartyClient()
    result = run(config, storage, tmp_path, client=client)
    # Two index rows share one accession, so only two documents are fetched.
    assert len(client.document_calls) == 2
    assert result.indexed == 3 and result.documents == 2
    assert result.fetched == 2


def test_shared_accession_keeps_a_row_per_cik(config, storage, tmp_path):
    """The issuer/insider pairing is what day 5's join is built on -- keep both."""
    run(config, storage, tmp_path, client=MultiPartyClient())
    rows = read_manifest(storage)
    assert len(rows) == 3

    form4 = [r for r in rows if r["accession"] == "0000058492-26-000491"]
    assert {r["cik"] for r in form4} == {"0001915962", "0000058492"}
    # Both rows point at the same stored document.
    assert len({r["s3_key"] for r in form4}) == 1
    # And each carries its own party's SIC: the issuer has one, the person does not.
    assert {r["sic"] for r in form4} == {"2510", None}


def test_shared_accession_stores_one_file(config, storage, tmp_path):
    run(config, storage, tmp_path, client=MultiPartyClient())
    stored = sorted(p.name for p in (Path(storage.root) / "raw" / "2026-08-26").iterdir())
    assert stored == ["0000058492-26-000491.txt", "0000320193-26-000123.txt"]
