# Day 1 — EDGAR ingestion

Fetch only. No parsing, no model, no database.

```bash
cp .env.example .env          # set SEC_USER_AGENT to a real name and email
make install
make ingest DATE=2026-08-27
```

Output:

```
data/raw/2026-08-27/0000320193-26-000123.txt      an 8-K, raw as EDGAR served it
data/raw/2026-08-27/0001234567-26-000124.txt      a Form 4
... a few hundred more
data/manifest/2026-08-27.jsonl
```

Set `STORAGE_URI=s3://your-bucket` to write to S3 instead of `./data`. Nothing
else changes — the key layout is identical, which is the point of the two
backends sharing one interface.

## The manifest

One JSON object per filing:

```json
{"accession":"0001043298-26-000002","cik":"0001018724","company":"AMAZON COM INC",
 "form":"4","filed_at":"2026-08-27T18:30:15","sic":"5961",
 "sic_description":"Retail-Catalog & Mail-Order Houses",
 "s3_key":"raw/2026-08-27/0001043298-26-000002.txt",
 "source_url":"https://www.sec.gov/Archives/edgar/data/1018724/0001043298-26-000002.txt",
 "bytes":7211}
```

Beyond the fields the plan called for:

- **`sic_description`** — comes free with the SIC code in the same response, and
  saves hardcoding the SIC taxonomy into the dashboard on day 6.
- **`source_url`** — provenance. When an extraction looks wrong on day 3, the
  first question is what the filing actually said.
- **`bytes`** — 10-K-sized documents exceed the context window, so day 3 needs a
  cheap way to spot them before sending anything to the GPU.

## Things about EDGAR that the plan's sketch gets wrong

Three assumptions cost a debugging cycle each, and are worth writing down since
anyone re-deriving this pipeline will hit them in the same order.

**The daily index header wraps.** `Date Filed` and `File Name` are on a second
line, so column offsets taken from the header row disagree with the data rows.
The separator is also a single continuous bar rather than one run of dashes per
column, so offsets cannot be read off it either. The parser anchors on regex
instead — CIK, date, and path cannot contain spaces, so whatever sits between
the form type and the CIK is the company name.

**Dates are `YYYYMMDD` in the daily index**, not `YYYY-MM-DD` as in the
quarterly ones. Both are accepted.

**Index paths are relative to `/Archives/`, not the site root.** Serving
`edgar/data/1800/…` from `https://www.sec.gov/` 404s on every document, which
looks alarmingly like a rate-limit block until you read the body.

**A missing index returns 403, not 404** — the same status the rate limiter
uses. A weekend date therefore burns four retries with backoff and ends in a
message that reads like throttling. Weekends are rejected before any request is
made, and 403 is excluded from retries on this one endpoint so a market holiday
fails immediately with an accurate message.

## What is stored is an SGML wrapper, not bare XML

A Form 4 arrives inside EDGAR's submission wrapper:

```
<SEC-DOCUMENT>0000950142-26-002448.txt : 20260827
<SEC-HEADER>...
<ACCEPTANCE-DATETIME>20260827191459
...
<XML>
  ...the actual Form 4...
</XML>
```

That is stored exactly as served. Extracting the inner XML is day 2's job, and
doing it now would mean redoing it once the Form 4 parser exists.

One thing is read out of the header at ingest time: `<ACCEPTANCE-DATETIME>`,
which is where `filed_at` gets a real timestamp. The daily index carries only a
date, and day 5's join windows insider transactions against 8-K filing *time* —
a filing accepted at 16:32 sits differently against market close than one at
06:00. It is read from the first 2 KB rather than parsed, which is not parsing
the document so much as reading its envelope.

## Rate limiting

SEC publishes a 10 req/sec ceiling and blocks for ~10 minutes when you exceed
it. The client paces at 8/sec by default, shared across every thread and across
both endpoints — documents and submissions draw on the same limiter, because
SEC counts them against the same budget.

Pacing is a minimum-interval limiter rather than a token bucket, deliberately: a
bucket permits bursts, and bursts are exactly what gets blocked.

A full day is roughly 600 documents plus ~600 uncached SIC lookups, all drawing
on the same 8/sec budget. Measured on 2026-08-27: **about an hour** cold, and
**1.1 seconds** warm, when every document is already stored and the SIC cache is
populated. The cold number is the floor set by the rate limit, not by the code,
and it is why day 3's extraction reads from S3 rather than re-fetching.

## Re-running is cheap

A second run of the same date skips every document already stored and re-fetches
nothing:

```
  indexed   1051 index rows -> 606 documents
  fetched   0
  skipped   606 (already stored)
  failed    0
  manifest  data/manifest/2026-08-27.jsonl (1051 rows)
```

This makes an interrupted run resumable, which matters when the whole thing
takes minutes under a rate limit. `--force` re-downloads.

SIC lookups are cached to `~/.cache/inhouse/sic.json` and deduplicated by CIK
within a run — a day of filings has far fewer distinct companies than filings,
and SIC codes effectively never change. The second day you ingest is noticeably
faster than the first.

The manifest is byte-stable across re-runs: rows are sorted, and a skipped
document keeps the size and timestamp the first run recorded rather than
degrading to coarser values. A re-run that produces a different manifest means
EDGAR changed, which is worth knowing.

## One accession, several CIKs

The daily index lists a submission once per *party* to it, not once per
document. A Form 4 appears twice — under the issuer's CIK and under the
insider's — sharing one accession number:

```
4   BARBEE ANGELA        1915962  edgar/data/1915962/0000058492-26-000491.txt
4   LEGGETT & PLATT INC    58492  edgar/data/58492/0000058492-26-000491.txt
```

Both paths serve byte-identical documents. On 2026-08-27, 1051 index rows were
606 actual documents; keying storage on the accession alone meant downloading
445 of them twice and racing two writers onto one key.

So documents are fetched once per accession, and **the manifest keeps a row per
CIK**. Dropping the duplicate rows would be the obvious tidy-up and would be
wrong: that issuer↔insider pairing is exactly what day 5's join needs, and it
arrives here for free rather than having to be recovered by parsing Form 4 XML.

Each row carries its own party's SIC, which is why the issuer's row has one and
the insider's does not.

## Scope notes

- **Amendments are excluded.** `8-K/A` and `4/A` restate earlier filings, and
  handling restatement is a v2 concern. Form matching is exact.
- **Individual filers have no SIC.** Form 4s filed by a person rather than a
  company return no SIC code, so `sic` is null. That is normal, not a failure —
  day 5's join reaches the issuer's SIC through the CIK relationship.
- **A day with no filings writes an empty manifest** rather than erroring, so
  downstream days can tell "ran, found nothing" from "never ran."
- **Weekends and holidays** have no index at all; the error says so explicitly
  rather than surfacing a bare 403.

## Failure behaviour

A document that fails after retries is logged and skipped; the run continues and
the manifest omits it. The exit code is non-zero if anything failed, so a
nightly cron notices. This is deliberate — one unavailable filing should not
cost the other thousand.
