# inhouse

**Self-hosted batch document extraction. Runs on your own GPU, in your own network — nothing leaves.**

Documents go in, schema-valid JSON comes out. The pipeline makes no outbound call during
extraction, so it can run against documents that could not be sent to a hosted API.

The included demo processes each day's SEC 8-K filings and Form 4 insider transactions — a few
hundred documents nightly on a single T4, for roughly $16/month. Fork it, swap the source and the
schema, and point it at your own corpus.

> For a single document, a frontier API is better and cheaper. This is for the case where you need
> hundreds processed identically every night, with guaranteed-valid output, on data that has to
> stay put.

---

## What it does

```
EDGAR daily index  (yesterday's filings, filtered by form type)
        |
        +-- Form 4 --> lxml parse -----------------+   no GPU
        |                                          |
        +-- 8-K    --> SGLang, constrained JSON ---+   GPU, batched
                                                   |
                                              Postgres
                                                   |
                                        FastAPI + one HTML page
```

Two form types, treated differently on purpose:

- **8-K** is narrative text describing a material event. It needs a model.
- **Form 4** is already-structured XML — issuer, insider, transaction code, shares, price. Parsing
  it with an LLM would burn GPU on data that is already machine-readable.

The join between them is the point. A CFO departure is a filing. A CFO departure with the CEO
having sold 40,000 shares three days earlier is a story, and finding it means correlating two form
types on CIK inside a date window.

---

## Why not just read EDGAR

EDGAR gives you a chronological list of filings and a link to each. To know whether one matters,
you open it and read it. An 8-K runs from one page to twenty and the material fact is often a
single sentence.

For an analyst covering one sector the daily workflow is: filter by SIC code, open forty filings,
read enough of each to decide, discard thirty-five.

This collapses that into a sorted table — a two-sentence summary per filing, a materiality flag,
and any insider transactions from the surrounding week already attached.

**It is not competing with Bloomberg.** The SEC corpus is a public demo of a private pipeline.

---

## Why not just call an API

| | Hosted API | inhouse |
|---|---|---|
| Cost, 5k documents | tokens x rate | GPU hours |
| Malformed JSON | possible | impossible by construction |
| Rate limits | tier-dependent | you own the queue |
| Data egress | leaves your network | none |
| Extraction quality | **better** | worse |

The last row is real and stated on purpose. Quantify it before claiming anything else — run the
same 50 filings through both and publish the gap.

---

## Stack, and why

| Layer | Choice | Reasoning |
|---|---|---|
| Inference | SGLang | Grammar-constrained decoding makes invalid JSON impossible rather than unlikely. At 500 documents a night, a 1% parse failure is 5 retries you would otherwise handle by hand. |
| Hardware | `g4dn.xlarge` | Cheapest current-gen GPU instance (~$0.53/hr). One T4, 16 GB. |
| Model | Qwen2.5-7B, 4-bit | 1.5B is too weak for 8-K summarisation; 7B fp16 is ~14 GB and will not fit alongside KV cache on a T4. 4-bit lands near 5 GB. |
| Database | Postgres (RDS) | The core query is a join across form types on CIK within a date window. Relational, with JSONB for the extraction payload. DynamoDB would force denormalising a natural join. |
| Raw storage | S3 | Keep originals so you can re-extract when the schema changes — and it will. Re-downloading from EDGAR is slow and rude. |
| Scheduling | EventBridge + user-data | Deliberately boring. A rule starts the instance, a script runs the job and stops it. Step Functions is the right answer at ten pipelines, not one. |
| API + UI | FastAPI, server-rendered HTML | The dashboard is a sorted table. A SPA is a day of work that adds nothing. |

### The cost decision that matters most

Filings land after market close. The GPU has no reason to run during the day.

```
always-on g4dn.xlarge  . . . . . . ~$380/month
started 6pm, ~1 hr, stopped  . . .  ~$16/month
```

Build start/stop in from day one rather than retrofitting. Boot plus model load is two to three
minutes, which is nothing for a batch job.

---

## Build plan

Seven days to a working MVP. Each day ends with something runnable.

### Day 1 — EDGAR ingestion, no model

Get data on disk before touching a GPU.

**Before writing code:** read the SEC access policy. They require a `User-Agent` header with real
contact details and enforce a 10 req/sec limit. They will block you otherwise.

**Tasks**

- [ ] Request the **G and VT vCPU quota increase** (Service Quotas -> EC2). Ask for 8 vCPUs.
      *Do this first — approval can take a day and it blocks day 3.*
- [ ] S3 bucket, private.
- [ ] Fetch the daily index for a chosen date:
      `https://www.sec.gov/Archives/edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx`
- [ ] Parse it, filter to form types `8-K` and `4`.
- [ ] Download each document to `s3://bucket/raw/YYYY-MM-DD/{accession}.txt`
- [ ] Pull the **CIK -> SIC mapping** from `data.sec.gov/submissions/CIK##########.json` and cache
      it. This is the sector filter the whole UI hangs off — grab it now while you are already
      fetching.
- [ ] Write a manifest, one JSON object per line:

```json
{"accession": "0001234567-26-000123", "cik": "0000320193",
 "form": "8-K", "filed_at": "2026-08-26T16:32:00",
 "sic": "3571", "s3_key": "raw/2026-08-26/0001234567-26-000123.txt"}
```

**Done when** one command produces yesterday's filings in S3 plus that manifest. Nothing parsed,
nothing summarised, no database.

> **Why no database yet.** You do not know your schema. It arrives tomorrow, and building tables
> now means rebuilding them then.

> **Store the raw documents, not just URLs.** You will re-run extraction many times while
> iterating on the schema, and EDGAR's rate limit makes re-fetching cost hours across the week.
> Recomputing from local source is cheap; re-acquiring source is not.

---

### Day 2 — Form 4 parser, and the schema

Form 4 first because there is no model in the loop and it will work today.

**Tasks**

- [ ] Parse Form 4 XML with `lxml`. Fields: `issuerCik`, `rptOwnerName`, `officerTitle`,
      `transactionCode`, `transactionShares`, `transactionPricePerShare`, `transactionDate`.
- [ ] Transaction codes are the signal: `P` purchase, `S` sale, `A` grant. A large unscheduled `S`
      is interesting; routine grants are not.
- [ ] Write the 8-K extraction schema.

**The schema is the artefact everything else is built around.** It becomes the cached prefix, so
make it long and make it good.

```json
{
  "event_type":  "enum[acquisition, departure, earnings, auditor_change,
                       restatement, agreement, bankruptcy, other]",
  "summary":     "string, two sentences, plain language",
  "materiality": "enum[high, medium, low]",
  "entities":    ["string"],
  "amounts":     [{"label": "string", "value_usd": "number"}],
  "themes":      ["string"]
}
```

Enums are where constrained decoding earns its place. Free-text categories drift across a few
hundred documents — "CFO departure", "executive departure", "officer resignation" — and enums make
that impossible.

**Done when** a day of Form 4s parses into structured rows, and the schema is written down with
three or four few-shot examples drafted.

---

### Day 3 — SGLang on a GPU instance

**Tasks**

- [ ] Launch `g4dn.xlarge`, Deep Learning AMI. Security group: SSH from your IP, and the group as
      its own source for internal traffic.
- [ ] Install SGLang, launch the server:

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-7B-Instruct-AWQ \
  --host 0.0.0.0 --port 30000 \
  --mem-fraction-static 0.85
```

- [ ] One 8-K in, valid JSON out, using `sampling_params={"json_schema": json.dumps(SCHEMA)}`
- [ ] Then twenty, **checked by hand against the filings**.

**Done when** twenty hand-checked filings hit your quality bar. Set that bar now — say, 80% correct
`event_type` — hit it, and move on.

> **The scope failure that will actually happen** is not feature creep. It is spending three days
> tuning extraction quality: the model gets some filings wrong, you tune the prompt, it gets
> different ones wrong, and the week disappears. Quality is measurable and improvable later. A
> pipeline that never ran is not.

---

### Day 4 — batch the pipeline

Concurrency is SGLang's job — continuous batching packs concurrent requests into steps. What you
build is the submission side.

**Tasks**

- [ ] Async submitter with a semaphore:

```python
sem = asyncio.Semaphore(32)
async def process(doc):
    async with sem:
        return await post(worker, PREFIX + doc)
```

- [ ] **Put the schema and few-shot examples in a long shared prefix**, byte-identical across every
      filing. The first document pays for it; the next several hundred hit prefix cache.
- [ ] Sweep the semaphore value — 8, 16, 32, 64 — and record documents/hour. Find the knee.
- [ ] Read cache hit rate from SGLang's `/metrics` rather than inferring it.
- [ ] Retry on failure, and log which documents failed and why.

**Done when** a full day of 8-Ks processes end to end, with throughput and cache hit rate recorded.

---

### Day 5 — Postgres and the join

**Tasks**

- [ ] RDS `db.t4g.micro`, or Postgres on the same box if you want to skip RDS setup.
- [ ] Schema:

```sql
filings(accession pk, cik, form_type, filed_at, sic, s3_key)

extractions(accession fk, event_type, summary, materiality,
            entities jsonb, amounts jsonb, themes jsonb,
            model text, extracted_at timestamptz)

insider_txns(accession fk, cik, insider, role, code,
             shares, price_usd, txn_date)
```

- [ ] The query the dashboard is built on:

```sql
select f.cik, f.sic, e.summary, e.materiality,
       i.insider, i.code, i.shares
from extractions e
join filings f using (accession)
left join insider_txns i
       on i.cik = f.cik
      and i.txn_date between f.filed_at - interval '5 days' and f.filed_at
where f.filed_at::date = :day
order by e.materiality desc;
```

**Store `model` and `extracted_at` on every extraction.** When the schema changes — and it will —
you need to know which rows came from which version.

**Done when** a day's filings are queryable and the join returns rows.

---

### Day 6 — dashboard

One page. A table of yesterday's filings sorted by materiality, filterable by SIC sector.

**Tasks**

- [ ] FastAPI on `t3.micro`, server-rendered HTML. No React.
- [ ] Sector filter using the two-digit SIC prefix — `60-67` finance, `73` business services,
      `28` chemicals and pharma.
- [ ] Each row: company, event type, summary, materiality, and any insider transactions in the
      surrounding week.

**The row that sells it:**

```
ACME CORP                                          SIC 6021
  8-K   Item 5.02 - CFO departure, effective immediately   [HIGH]
  Form 4 - CEO sold 40,000 shares three days prior
```

Neither filing alone is remarkable. The join is the product.

**Done when** you can load yesterday, filter to banking, and see a sorted list.

---

### Day 7 — automate, measure, write up

**Tasks**

- [ ] EventBridge rule at 6pm ET starts the instance; user-data runs the job and stops it.
- [ ] **Cost comparison.** Same 200 filings through a frontier API. Two numbers, one table. This is
      the headline claim and it needs evidence.
- [ ] **Quality comparison.** Same 50 filings through both, compared by hand. *"N% worse at 3% of
      the cost"* is far stronger than avoiding the question — and you will be asked it.
- [ ] Record: documents/hour, cache hit rate, cost per thousand documents, parse failure rate.
- [ ] Prove the isolation claim rather than asserting it: run extraction in a subnet with no
      internet gateway (ingestion is a separate step), or attach a VPC flow log showing zero
      external egress during the run. A screenshot of that beats a paragraph claiming it.

---

## MVP scope

| In | Out, and why |
|---|---|
| Yesterday's 8-Ks and Form 4s | Historical backfill — triples ingestion complexity |
| One extraction schema | Per-form schemas and routing — that is v2, and it needs v1 working |
| One GPU worker | Multi-worker cache-aware routing — real value, but another week |
| Table sorted by materiality | Charts, search, auth, alerts — none change what this demonstrates |
| Nightly refresh | Real-time streaming — filings are not real-time; the batch window is the point |

---

## Adapting it to your own data

Three files:

```
config/
  source.py      # EDGAR by default. Swap for S3, a filesystem, a database.
  schema.json    # what you extract
  prompt.txt     # the cached prefix - instructions and few-shot examples
```

Everything downstream — batching, constrained decoding, storage, the dashboard — is
domain-agnostic. The SEC pipeline is one instance of it.

---

## Running it

```bash
git clone https://github.com/<you>/inhouse
cd inhouse
cp .env.example .env          # AWS region, bucket, DB URL, SEC user-agent
make ingest DATE=2026-08-26   # fetch to S3, no GPU
make extract DATE=2026-08-26  # requires a running SGLang server
make serve                    # dashboard on :8080
```

---

## Limitations

- **Extraction quality is below a frontier model.** Quantified in `benchmarks/quality.md`.
- **8-K only.** 10-K and 10-Q exceed the context window and need section splitting.
- **SIC codes are coarse.** The taxonomy predates most of the tech industry, so software, fintech,
  and platform companies land in the same buckets. The model-generated `themes` field is the
  finer-grained complement.
- **Single worker.** Cache-aware routing across multiple GPUs is designed but not built.

## License

Apache 2.0.
