# inhouse

**Self-hosted batch document extraction. Runs on your own GPU, in your own network — nothing leaves.**

---

One ordinary Thursday on the SEC's wire — 27 August 2026 — produced 181 8-K filings.

|  | to read them yourself | after this pipeline |
|---|---|---|
| Words | **125,329** | **7,510** |
| At 200 wpm | **10.4 hours** | **38 minutes** |
| Per filing | 692 words | 41 words |

Sixteen times less to read, and the 8-K/Form-4 correlations already attached — an executive who
sold shares in the days before their company filed something material, which neither document shows
on its own.

That is one public corpus, chosen because you can check the output against sec.gov yourself. The
point is the shape: **a few hundred documents every night, each reduced to a row you can sort.**

<details>
<summary><b>One filing, before and after</b> — the median document of that day, 405 words</summary>

Arthur J. Gallagher & Co., filed 27 August. The submission on EDGAR is 138 KB of SGML wrapping
eleven attachments; this is what remains after the envelope, the XBRL and the exhibits are stripped:

> **Item 5.02 Departure of Directors or Certain Officers; Election of Directors; Appointment of
> Certain Officers; Compensatory Arrangements of Certain Officers.** On August 24, 2026, Richard C.
> Cary, age 63, Controller and Chief Accounting Officer of Arthur J. Gallagher & Co. (the
> "Company"), notified the Company that he plans to retire in 2028 and step down from his role as
> Controller and Chief Accounting Officer (and as the Company's principal accounting officer)
> effective September 30, 2026. Mr. Cary has served as the Company's Controller since 1997 and as
> its Chief Accounting Officer since 2001. He will remain employed by the Company as Corporate Vice
> President - Accounting and will support the transition through his expected retirement in 2028.
> Mr. Cary's planned retirement is not due to any disagreement with the Company on any matter
> relating to the Company's financial statements, internal control over financial reporting,
> operations, policies or practices. In conjunction with this transition and consistent with the
> Company's succession planning, Kyle G. Koreyva, age 42, will succeed Mr. Cary as Controller and
> Chief Accounting Officer (and as the Company's principal accounting officer), effective October
> 1, 2026. Mr. Koreyva joined the Company as part of the Company's acquisition of AssuredPartners
> in August 2025. He has held progressively senior accounting and finance roles over the past 20
> years, most recently serving as the Company's Vice President, Accounting and prior to the
> acquisition as AssuredPartners' Chief Accounting Officer starting in June 2024. Prior to joining
> AssuredPartners, Mr. Koreyva served as Vice President, Finance and Divisional Chief Financial
> Officer of Westchester, a division of Chubb, from April 2020 to June 2024. Before then, Mr.
> Koreyva spent the first 14 years of his career with PricewaterhouseCoopers as an auditor in its
> assurance practice, including three years in its national office. There are no changes to the
> compensation arrangements for Mr. Koreyva in connection with his assuming the role of Controller
> and Chief Accounting Officer. Mr. Koreyva has no family relationship with any director or
> executive officer of the Company, and there are no transactions involving Mr. Koreyva that
> require disclosure under Item 404(a) of Regulation S-K. SIGNATURES Pursuant to the requirements
> of the Securities Exchange Act of 1934, the registrant has duly caused this report to be signed
> on its behalf by the undersigned hereunto duly authorized...

**What the pipeline stores — 56 words:**

> Richard C. Cary, Arthur J. Gallagher & Co.'s Controller and Chief Accounting Officer, plans to
> retire in 2028 and will step down from his roles effective September 30, 2026. Kyle G. Koreyva,
> who joined the company as part of its acquisition of AssuredPartners, will succeed him as
> Controller and Chief Accounting Officer starting October 1, 2026.

```
event_type   executive_change
direction    departure
materiality  medium
sector       Insurance
```

Seven times shorter, and sortable. Note what the summary drops: Koreyva's fourteen years at
PwC, the Regulation S-K boilerplate, the signature block. Note what it keeps: both names, both
dates, and the fact that this is a succession rather than a resignation — which is the difference
between a routine filing and one worth opening.

</details>

---

## Point it at your own documents

The reason to run this rather than call an API is that **the documents never leave your network.**
Extraction makes no outbound call — the model runs on hardware you control, reads from storage you
control, and writes to a database you control. There is no vendor to trust with the contents, no
terms of service to re-read when they change, and nothing to explain to a compliance team.

That matters for the corpora that are actually worth extracting:

- **Contracts and agreements** — the counterparty's confidentiality clause probably forbids sending
  them to a third party at all
- **Medical records, claims, case files** — regulated, and the regulation does not care that the
  API promises not to train on them
- **Internal documents** — incident reports, board minutes, customer correspondence
- **Anything under NDA**, where "we used a hosted service" is the wrong answer to a question you
  will eventually be asked

The demo uses public filings precisely so the quality is checkable. Swap three files —
`config/source.py`, `config/schema.json`, `config/prompt.txt` — and the rest of the pipeline does
not know or care what the documents are.

> For a single document, a frontier API is better and cheaper. This is for the case where you need
> hundreds processed identically every night, with guaranteed-valid output, on data that has to
> stay put.

Roughly **$7/month** in GPU and storage for a few hundred documents nightly on a single T4.

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
                                    static HTML, rebuilt nightly
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
started nightly, ~22 min . . . . . .  ~$6/month
```

Measured, not estimated: the GPU is needed for 22 minutes of a 90-minute pipeline, so the workflow
starts it after ingestion and the instance powers itself off when done. Boot plus model load is
three minutes.

The line that makes this true is one `if: always()` on the stop step, plus `shutdown -h now` as the
last line of the script the instance runs. A pipeline that only *starts* an instance is a pipeline
that bills $380/month the first time something fails.

---

## How it works

Measured on 2026-08-27: 1,051 index entries, 606 documents, 181 8-Ks, 425 Form 4s.

### Ingestion

The daily index is fetched, filtered to `8-K` and `4`, and every document stored raw. Nothing is
parsed at this stage — the extraction schema will change, and re-parsing local copies is free while
re-fetching from EDGAR costs an hour under the rate limit.

Three things about EDGAR that a reasonable guess gets wrong, each found by running against it:

- **The index lists a submission once per party.** A Form 4 appears under the issuer's CIK and again
  under the insider's, sharing one accession — 425 of 606 documents on a sample day. Fetching per
  index row downloads 40% of the corpus twice; the issuer↔insider pairing is also what makes the
  day-5 join possible without re-parsing XML.
- **Paths are relative to `/Archives/`**, and a missing index returns **403, not 404** —
  indistinguishable from throttling.
- **The filing date is not the acceptance time.** EDGAR's cutoff is 17:30 ET, so a submission
  accepted at 21:05 on Tuesday is *filed* Wednesday and appears in Wednesday's index. Grouping by
  acceptance invents days that EDGAR does not recognise.

### Parsing

**8-K** arrives as an SGML envelope around a dozen-odd attachments — the filing, its exhibits, XBRL
taxonomy files, an embedded logo. The primary document is selected by `<TYPE>`, not by looking for
HTML: the XBRL viewer artefacts are HTML too. Median 1.7 MB becomes ~2,600 characters, a **122×
reduction**, and 181/181 parse.

Two things that only show up on real filings: inline XBRL hides a metadata block behind
`display:none`, which a tag-strip puts at the top of every prompt; and `lxml`'s `text_content()`
concatenates adjacent blocks, turning `Other Events.` + `On August 27` into `Events.On August 27`.

**Form 4** is `ownership.xml` and nothing else — the filer completes a web form and EDGAR emits the
XML, so there is no rendered text version and nothing for a model to do. 425/425 parse into 1,135
transactions with no missing codes, shares, dates or names.

### Extraction

The schema is fitted to the corpus rather than guessed. A draft enum written before reading any
filings covered 51% of a day; the current one leaves 28% in `other`. `dividend` and `buyback` exist
because the model kept calling them `earnings`, which they are not.

`extractions` is deliberately thin. Everything the SEC already states — item codes, SIC, filer, date
— lives on `filings` where it is exact. Two fields were dropped after measuring them: `amounts` was
populated on 4 of 20 filings, because 65% of 8-Ks state their figures in an exhibit the model never
sees; `themes` produced 36 distinct values across 48 extractions, which grouped nothing.

Accuracy is **84%** on `event_type`, scored against the SEC's own `ITEM INFORMATION` labels — a
free evaluation set that comes with every filing.

### Throughput

Measured on a T4, cache flushed between levels:

| concurrency | docs/hour | mean latency |
|---|---|---|
| 1 | 135 | 27s |
| 8 | 315 | 88s |
| 16 | 354 | 155s |
| 32 | 380 | 285s |
| 64 | 381 | 353s |

The plateau at 32 is memory bandwidth, not concurrency: decode streams ~5 GB of weights per step
whether it produces one token or thirty-two. A full day of 181 filings runs at **439–547 docs/hour**
with a **60% prefix cache hit rate**, in about 20 minutes.

Per-request latency gets worse as concurrency rises and that is fine — nobody waits on filing #94.

### The join

```sql
FROM extractions e
JOIN filings f USING (accession)
LEFT JOIN insider_txns i
       ON i.cik = f.cik
      AND i.txn_date BETWEEN f.filed_at::date - INTERVAL '5 days' AND f.filed_at::date
```

Nine of 181 filings paired on a sample day. Two of the three largest turned out to be noise once
their footnotes were read — one a Rule 10b5-1 plan adopted months earlier, one an estate settling a
deceased founder's shares. **88% of transactions carry a footnote**, and the dashboard shows them,
because the join finds candidates and the footnote says which are real.

### Automation

Three jobs, ordered by what the GPU costs:

```
ingest    ~60 min   GPU stopped    rate-limited at 8 req/s
extract   ~22 min   GPU RUNNING    the instance runs this on itself
load       ~2 min   GPU stopped
```

Extraction runs *on* the instance rather than being driven over SSH: a CI runner has no stable
address, so admitting one would mean opening the port broadly, and SGLang has no authentication.
The box boots, reads its date from an instance tag, extracts, uploads its log, and powers itself
off. The workflow polls S3 for a completion marker.

AWS credentials are OIDC — GitHub mints a token per run proving which repository is executing, and
nothing long-lived is stored.

---

## MVP scope

| In | Out, and why |
|---|---|
| Yesterday's 8-Ks and Form 4s | Historical backfill — triples ingestion complexity |
| One extraction schema | Per-form schemas and routing — that is v2, and it needs v1 working |
| One GPU worker | Multi-worker cache-aware routing — real value, but another week |
| Table sorted by materiality, filtered by sector | Charts, search, auth, alerts — none change what this demonstrates |
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
pip install -e ".[dev]"
cp .env.example .env          # SEC user-agent, storage URI, database URL

python -m inhouse ingest  --date 2026-08-27              # fetch to S3, no GPU
python -m inhouse extract --date 2026-08-27 --concurrency 32   # needs SGLang
python -m inhouse load    --date 2026-08-27 --schema     # into Postgres
python -m uvicorn inhouse.web:app --port 8080            # dashboard
```

`extract` also takes `--sweep 8,16,32,64`, which runs the same documents at each concurrency level
and reports docs/hour, flushing the server's prefix cache between levels so the numbers are
comparable.

Deployment — the nightly workflow, the systemd units, and the IAM policies — is in
[docs/deploy.md](docs/deploy.md).

---

## Limitations

- **Extraction quality is below a frontier model, and not yet quantified against one.** 84% on
  `event_type` against the SEC's own item labels is the internal number; the comparison that
  matters — the same 50 filings through both — has not been run.
- **The cost claim is unmeasured.** ~$6/month of GPU against a frontier API's token cost for the
  same 200 filings is the headline, and it needs two real numbers rather than an estimate.
- **8-K only.** Not for the reason expected: the corpus maxes at 35k characters, well inside the
  context window. 10-K and 10-Q are the ones that would need section splitting.
- **65% of 8-Ks put their figures in an exhibit** the model never reads, so a summary of an earnings
  release says an earnings release exists. `facts_in_exhibit` flags this rather than hiding it.
  Feeding exhibits in is a measured trade — they are long and mostly tables — and has not been made.
- **SIC codes are coarse.** The taxonomy predates most of the tech industry, so software, fintech
  and platform companies land in the same buckets. A sector table maps SIC ranges to names the
  dashboard filters on; distinguishing *within* a bucket is still unsolved.
- **`materiality` is a sort key, not a measurement.** 75% of a sample day came back `medium`.
- **Single worker.** Cache-aware routing across multiple GPUs is designed but not built.

## License

Apache 2.0.
