"""The day 1 job: fetch a date's 8-Ks and Form 4s into storage, plus a manifest.

Nothing is parsed and nothing is interpreted. Documents are stored exactly as
EDGAR served them, because the extraction schema does not exist yet and will
change once it does. Re-running extraction against local copies is cheap;
re-fetching from EDGAR is slow and rude.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .edgar import EdgarClient, EdgarError, document_url
from .index import IndexEntry, parse_index
from .sic import SicLookup
from .storage import Storage

log = logging.getLogger(__name__)

# EDGAR's SGML wrapper carries the acceptance timestamp, which the daily index
# does not -- the index only has a date. Times matter downstream: the day 5 join
# windows insider transactions against 8-K filing time, and a filing accepted
# at 16:32 sits differently against market close than one at 06:00.
ACCEPTANCE_RE = re.compile(rb"<ACCEPTANCE-DATETIME>(\d{14})")


def _filed_at(body: bytes | None, fallback: date) -> str:
    """Acceptance timestamp from the SGML header, or the index date if absent."""
    if body:
        match = ACCEPTANCE_RE.search(body[:2048])
        if match:
            stamp = match.group(1).decode()
            try:
                return datetime.strptime(stamp, "%Y%m%d%H%M%S").isoformat()
            except ValueError:
                pass
    return fallback.isoformat()


@dataclass(frozen=True)
class ManifestRecord:
    accession: str
    cik: str
    company: str
    form: str
    filed_at: str          # acceptance timestamp, to the second
    filing_date: str       # the date EDGAR files it under, from the index
    sic: str | None
    sic_description: str | None
    s3_key: str
    source_url: str
    bytes: int


@dataclass
class IngestResult:
    day: date
    indexed: int          # index rows of our form types
    documents: int        # distinct accessions behind those rows
    fetched: int          # newly downloaded
    skipped: int          # already present in storage
    failed: int
    manifest_key: str
    records: list[ManifestRecord]

    @property
    def stored(self) -> int:
        return len(self.records)


def raw_key(day: date, entry: IndexEntry) -> str:
    """Storage key for a document, keeping the extension EDGAR served."""
    return f"raw/{day:%Y-%m-%d}/{entry.accession}.{entry.extension}"


def manifest_key(day: date) -> str:
    return f"manifest/{day:%Y-%m-%d}.jsonl"


def ingest(
    day: date,
    config: Config,
    storage: Storage,
    client: EdgarClient | None = None,
    *,
    limit: int | None = None,
    force: bool = False,
    cache_path=None,
) -> IngestResult:
    client = client or EdgarClient(config.user_agent, config.rate_limit)

    log.info("fetching daily index for %s", day)
    entries = parse_index(client.daily_index(day), form_types=config.form_types)
    log.info(
        "index lists %d filings of types %s",
        len(entries), ", ".join(config.form_types),
    )
    if limit is not None:
        entries = entries[:limit]
        log.info("limited to %d filings", len(entries))

    # One accession is one submission, but the daily index lists it once per
    # party to the filing: a Form 4 appears under the issuer's CIK and again
    # under the insider's. Fetch each document once and keep every CIK -- the
    # issuer/insider pairing is precisely what day 5's join needs.
    groups: dict[str, list[IndexEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.accession, []).append(entry)
    if len(groups) != len(entries):
        log.info(
            "%d filings across %d distinct accessions (%d listed under multiple CIKs)",
            len(entries), len(groups), len(entries) - len(groups),
        )

    if not entries:
        # A valid trading day with no matching filings is an empty manifest, not
        # an error -- downstream days should see "ran, found nothing".
        storage.put(manifest_key(day), b"")
        return IngestResult(day, 0, 0, 0, 0, 0, storage.uri(manifest_key(day)), [])

    # SIC first: the lookups are deduplicated by CIK, so this is far fewer
    # requests than there are filings, and the manifest needs them inline.
    sic = SicLookup(client, cache_path=cache_path, max_workers=config.max_concurrency)
    sic.warm(e.cik for e in entries)

    records: list[ManifestRecord] = []
    counts = {"fetched": 0, "skipped": 0, "failed": 0}
    lock = threading.Lock()

    def handle(item: tuple[str, list[IndexEntry]]) -> None:
        _, group = item
        # The document is identical whichever party it is listed under; fetch it
        # from the first and emit a manifest row for each.
        entry = group[0]
        key = raw_key(day, entry)
        try:
            if not force and storage.exists(key):
                # Resume behaviour: a re-run after a partial failure only fetches
                # what is missing. This is what makes the rate limit survivable.
                # Re-read the stored header so a skipped filing still gets its
                # acceptance timestamp -- otherwise a re-run would rewrite the
                # manifest with coarser data than the first run produced.
                body = storage.head_bytes(key, 2048)
                with lock:
                    counts["skipped"] += 1
                size = storage.size(key)
            else:
                body = client.document(entry.path)
                storage.put(key, body)
                size = len(body)
                with lock:
                    counts["fetched"] += 1
        except EdgarError as exc:
            log.error("failed %s (%s): %s", entry.accession, entry.form, exc)
            with lock:
                counts["failed"] += 1
            return

        for member in group:
            info = sic.get(member.cik)
            record = ManifestRecord(
                accession=member.accession,
                cik=member.cik,
                # The index company name is the filer; for a Form 4 that is the
                # insider, not the issuer. Prefer the submissions name when present.
                company=info.get("company") or member.company,
                form=member.form,
                filed_at=_filed_at(body, member.filed_date),
                # From the index, not derived from the acceptance time. EDGAR's
                # cutoff is 17:30 ET, so a submission accepted at 21:05 on
                # Tuesday is filed on Wednesday and appears in Wednesday's
                # index. Grouping by the acceptance date invents a day.
                filing_date=member.filed_date.isoformat(),
                sic=info.get("sic"),
                sic_description=info.get("sic_description"),
                s3_key=key,
                source_url=document_url(member.path),
                bytes=size,
            )
            with lock:
                records.append(record)

    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        list(pool.map(handle, groups.items()))

    # Sorted so the manifest is byte-stable across runs -- a re-run of the same
    # day should produce the same file, which makes diffing it meaningful.
    records.sort(key=lambda r: (r.form, r.accession, r.cik))

    body = "".join(json.dumps(asdict(r), separators=(",", ":")) + "\n" for r in records)
    mkey = manifest_key(day)
    storage.put(mkey, body.encode("utf-8"))

    return IngestResult(
        day=day,
        indexed=len(entries),
        documents=len(groups),
        fetched=counts["fetched"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        manifest_key=storage.uri(mkey),
        records=records,
    )
