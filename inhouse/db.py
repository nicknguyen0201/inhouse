"""Load a day's filings, extractions and insider transactions into Postgres.

The three sources arrive in different shapes and only become useful together:

    manifest JSONL    -> filings        what was filed, by whom, when
    extraction JSONL  -> extractions    what the model made of an 8-K
    raw Form 4 XML    -> insider_txns   what an insider actually did

Until they are in one database the join at the centre of this project cannot
run: an 8-K reporting a CFO departure is a filing; the same 8-K with the CEO
having sold 40,000 shares three days earlier is a story, and finding that means
correlating two form types on CIK inside a date window.

Loads are idempotent. Re-running a day updates rows rather than duplicating
them, because you will re-extract as the schema changes -- that is the whole
reason the raw documents are kept.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .extract import read_manifest
from .form4 import parse_form4
from .parse import ParseError

log = logging.getLogger(__name__)

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


class DatabaseError(Exception):
    """Loading failed."""


@dataclass
class LoadCounts:
    filings: int = 0
    extractions: int = 0
    insider_txns: int = 0
    skipped: int = 0

    def __str__(self) -> str:
        return (
            f"filings {self.filings}, extractions {self.extractions}, "
            f"insider_txns {self.insider_txns}, skipped {self.skipped}"
        )


def _hint(dsn: str, exc: Exception) -> str:
    """A readable message for the ways a DSN is usually malformed.

    Connection strings are URLs, so a password containing %, @, / or : breaks
    parsing in a way whose error message names none of that.
    """
    import re

    if "percent-encoded" in str(exc):
        return (
            "the password appears to contain a '%', which is the escape "
            "character in a URL. Percent-encode it as '%25', or reset the "
            "password to one without % / @ : ? #"
        )
    if not re.match(r"^postgres(ql)?://", dsn):
        return (
            "expected a full connection string, e.g.\n"
            "  postgresql://user:password@host:5432/dbname\n"
            f"got: {dsn[:60]!r}"
        )
    return str(exc)


def connect(dsn: str):
    """Open a connection. psycopg 3 preferred, psycopg2 accepted."""
    try:
        import psycopg
    except ImportError:
        pass
    else:
        try:
            return psycopg.connect(dsn)
        except psycopg.ProgrammingError as exc:
            raise DatabaseError(f"could not parse DATABASE_URL: {_hint(dsn, exc)}") from exc
    try:
        import psycopg2

        return psycopg2.connect(dsn)
    except ImportError as exc:
        raise DatabaseError(
            "no Postgres driver installed. Install one with:\n"
            "  pip install 'psycopg[binary]'"
        ) from exc


def apply_schema(conn, path: Path | str = SCHEMA_SQL) -> None:
    """Create tables and indexes. Safe to run repeatedly."""
    sql = Path(path).read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _parse_ts(value: str) -> datetime:
    """Manifest timestamps are acceptance datetimes; some rows carry only a date."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise DatabaseError(f"unparseable filed_at: {value!r}")


# --- filings ---------------------------------------------------------------


FILINGS_SQL = """
INSERT INTO filings (accession, cik, company, form_type, filed_at, filing_date,
                     sic, sic_description, s3_key, source_url)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (accession, cik) DO UPDATE SET
    company         = EXCLUDED.company,
    form_type       = EXCLUDED.form_type,
    filed_at        = EXCLUDED.filed_at,
    filing_date     = EXCLUDED.filing_date,
    sic             = EXCLUDED.sic,
    sic_description = EXCLUDED.sic_description,
    s3_key          = EXCLUDED.s3_key,
    source_url      = EXCLUDED.source_url
"""


def load_filings(conn, manifest_body: str) -> int:
    """Every manifest row, including the several a shared accession produces.

    A Form 4 is listed once per party to it, so one document yields a row for
    the issuer and another for the insider. Both are kept: that pairing is what
    lets the dashboard reach an issuer's SIC from an insider's transaction
    without re-parsing the XML.
    """
    rows = []
    for line in manifest_body.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rows.append((
            r["accession"], r["cik"], r["company"], r["form"],
            _parse_ts(r["filed_at"]),
            # Older manifests predate the column; their acceptance date is the
            # best available answer.
            r.get("filing_date") or _parse_ts(r["filed_at"]).date().isoformat(),
            r.get("sic"), r.get("sic_description"),
            r["s3_key"], r.get("source_url"),
        ))

    with conn.cursor() as cur:
        cur.executemany(FILINGS_SQL, rows)
    conn.commit()
    return len(rows)


# --- extractions -----------------------------------------------------------


EXTRACTIONS_SQL = """
INSERT INTO extractions (accession, event_type, direction, summary, materiality,
                         facts_in_exhibit, truncated, primary_document,
                         model, extracted_at, latency_s)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (accession) DO UPDATE SET
    event_type       = EXCLUDED.event_type,
    direction        = EXCLUDED.direction,
    summary          = EXCLUDED.summary,
    materiality      = EXCLUDED.materiality,
    facts_in_exhibit = EXCLUDED.facts_in_exhibit,
    truncated        = EXCLUDED.truncated,
    primary_document = EXCLUDED.primary_document,
    model            = EXCLUDED.model,
    extracted_at     = EXCLUDED.extracted_at,
    latency_s        = EXCLUDED.latency_s
"""


def load_extractions(conn, jsonl_body: str) -> int:
    """One row per 8-K. A re-extraction overwrites, which is the point of
    storing `model` and `extracted_at` alongside."""
    rows = []
    for line in jsonl_body.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rows.append((
            r["accession"], r["event_type"], r.get("direction"),
            r["summary"], r["materiality"],
            bool(r.get("facts_in_exhibit")), bool(r.get("truncated")),
            r.get("primary_document"),
            r.get("model", "unknown"),
            _parse_ts(r["extracted_at"]) if r.get("extracted_at") else datetime.utcnow(),
            r.get("latency_s"),
        ))

    with conn.cursor() as cur:
        cur.executemany(EXTRACTIONS_SQL, rows)
    conn.commit()
    return len(rows)


# --- insider transactions --------------------------------------------------


INSIDER_SQL = """
INSERT INTO insider_txns (accession, txn_index, cik, issuer_name, issuer_symbol,
                          insider_cik, insider, role,
                          is_director, is_officer, is_ten_pct,
                          security_title, code, acquired_disposed,
                          shares, price_usd, shares_after, direct_ownership,
                          derivative, txn_date, footnotes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
-- DO UPDATE, not DO NOTHING: the key is now positional, so a parser fix
-- propagates on the next load instead of leaving stale rows behind.
ON CONFLICT (accession, txn_index) DO UPDATE SET
    cik = EXCLUDED.cik, issuer_name = EXCLUDED.issuer_name,
    issuer_symbol = EXCLUDED.issuer_symbol, insider_cik = EXCLUDED.insider_cik,
    insider = EXCLUDED.insider, role = EXCLUDED.role,
    is_director = EXCLUDED.is_director, is_officer = EXCLUDED.is_officer,
    is_ten_pct = EXCLUDED.is_ten_pct, security_title = EXCLUDED.security_title,
    code = EXCLUDED.code, acquired_disposed = EXCLUDED.acquired_disposed,
    shares = EXCLUDED.shares, price_usd = EXCLUDED.price_usd,
    shares_after = EXCLUDED.shares_after,
    direct_ownership = EXCLUDED.direct_ownership,
    derivative = EXCLUDED.derivative, txn_date = EXCLUDED.txn_date,
    footnotes = EXCLUDED.footnotes
"""


def load_insider_txns(conn, manifest_body: str, load_document) -> tuple[int, int]:
    """Parse every Form 4 in the manifest and insert its transactions.

    Holdings are not transactions and never reach here -- the parser drops them,
    because a standing position has no code, date or price and would arrive as a
    row of nulls that looks like a failed parse.

    Returns (inserted, skipped).
    """
    rows, skipped = [], 0
    for row in read_manifest(manifest_body, form="4"):
        try:
            filing = parse_form4(load_document(row["s3_key"]), accession=row["accession"])
        except ParseError as exc:
            log.error("form 4 parse failed for %s: %s", row["accession"], exc)
            skipped += 1
            continue

        for t in filing.transactions:
            if t.transaction_date is None:
                # txn_date is NOT NULL and the join windows on it; a transaction
                # without one cannot participate.
                skipped += 1
                continue
            rows.append((
                filing.accession, t.index,
                # The issuer's CIK: what the join keys on. An insider's own CIK
                # carries no SIC and is not what a sector filter looks up.
                t.issuer_cik, t.issuer_name, t.issuer_symbol,
                t.insider_cik, t.insider_name, t.role,
                t.is_director, t.is_officer, t.is_ten_percent_owner,
                t.security_title, t.code, t.acquired_disposed,
                t.shares, t.price_per_share, t.shares_owned_after,
                t.direct_ownership, t.derivative, t.transaction_date,
                json.dumps(t.footnotes),
            ))

    with conn.cursor() as cur:
        cur.executemany(INSIDER_SQL, rows)
    conn.commit()
    return len(rows), skipped


# --- orchestration ---------------------------------------------------------


def load_day(conn, manifest_body: str, extractions_body: str, load_document) -> LoadCounts:
    """Load one day from its three sources."""
    counts = LoadCounts()
    counts.filings = load_filings(conn, manifest_body)
    if extractions_body.strip():
        counts.extractions = load_extractions(conn, extractions_body)
    counts.insider_txns, counts.skipped = load_insider_txns(
        conn, manifest_body, load_document
    )
    return counts


DASHBOARD_SQL = """
SELECT company, sic, sic_description, event_type, materiality, summary,
       insider, role, code, shares, price_usd, txn_date, txn_value_usd
FROM daily_dashboard
WHERE filed_at::date = %s
ORDER BY CASE materiality WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
         txn_value_usd DESC NULLS LAST
"""


def dashboard_rows(conn, day: str) -> list[tuple]:
    """The query the whole project is built around."""
    with conn.cursor() as cur:
        cur.execute(DASHBOARD_SQL, (day,))
        return cur.fetchall()
