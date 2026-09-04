"""Concurrent submission to SGLang.

Continuous batching is the server's job: it packs concurrent requests into the
same GPU step, admitting and retiring them per step rather than per batch. What
this module builds is the queue that keeps it fed.

Why it matters, measured on this pipeline running sequentially:

    #running-req: 1     one sequence at a time
    token usage: 0.05   5% of KV cache in use
    gen throughput: 6.8 tok/s

Decode is memory-bandwidth-bound -- the T4 streams ~5GB of weights per step
whether it is generating one token or thirty-two. So a sequential loop pays the
full cost of a step to produce a single token. Concurrency is close to free
throughput until KV cache fills.

Note that per-request latency does NOT improve: each filing still waits through
its own decode steps. Only throughput does, which is the number that matters for
a nightly batch where nobody is waiting on an individual document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from .extract import (
    Extraction,
    ExtractionError,
    ExtractionRun,
    build_prefix,
    build_prompt,
)
from .parse import ParseError, parse_filing
from .sglang_client import DEFAULT_URL

log = logging.getLogger(__name__)


@dataclass
class BatchStats:
    """What day 4 asks to be recorded."""

    concurrency: int
    documents: int
    failures: int
    wall_s: float
    cache_hit_rate: float | None = None
    latencies: list[float] = field(default_factory=list)

    @property
    def docs_per_hour(self) -> float:
        return self.documents / self.wall_s * 3600 if self.wall_s else 0.0

    @property
    def mean_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def p90_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return sorted(self.latencies)[int(0.9 * len(self.latencies)) - 1]

    def line(self) -> str:
        hit = f"{self.cache_hit_rate:.1%}" if self.cache_hit_rate is not None else "n/a"
        return (
            f"  concurrency {self.concurrency:>3}  "
            f"{self.docs_per_hour:>8.0f} docs/hr  "
            f"wall {self.wall_s:>6.1f}s  "
            f"mean {self.mean_latency:>5.1f}s  "
            f"p90 {self.p90_latency:>5.1f}s  "
            f"cache {hit:>6}  "
            f"fail {self.failures}"
        )


class AsyncSGLangClient:
    """Async counterpart to SGLangClient, for many requests in flight."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        max_new_tokens: int = 700,
        temperature: float = 0.0,
        timeout: float = 600.0,
        retries: int = 2,
    ) -> None:
        self.url = url.rstrip("/")
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        # Generous: under load a request may queue behind many others before
        # the server admits it. A timeout here would look like a server failure
        # when it is really just backpressure.
        self.timeout = timeout
        self.retries = retries

    async def generate(self, client: httpx.AsyncClient, prompt: str, schema: dict) -> str:
        payload = {
            "text": prompt,
            "sampling_params": {
                "temperature": self.temperature,
                "max_new_tokens": self.max_new_tokens,
                "json_schema": json.dumps(schema),
            },
        }

        last: str = "unknown"
        for attempt in range(1, self.retries + 2):
            try:
                resp = await client.post(f"{self.url}/generate", json=payload)
                resp.raise_for_status()
                return resp.json()["text"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                # repr, not str: httpx exceptions frequently have an empty
                # str(), which turns a real error into a blank message.
                last = f"{type(exc).__name__}: {exc}".rstrip(": ")
                if isinstance(exc, httpx.HTTPStatusError):
                    last += f" -- body: {exc.response.text[:200]}"
                if attempt <= self.retries:
                    await asyncio.sleep(2.0 ** (attempt - 1))

        raise ExtractionError(
            f"SGLang request failed after {self.retries + 1} attempts: {last}"
        )

    async def flush_cache(self, client: httpx.AsyncClient) -> bool:
        """Clear SGLang's radix (prefix) cache.

        Needed to compare concurrency levels honestly: after a level has run the
        documents, their whole prompts sit in the tree, so the next level reads
        that work back instead of computing it. Flushing between levels puts
        each on the same footing as a real night, where every filing is new and
        only the shared prefix is a hit.
        """
        try:
            resp = await client.post(f"{self.url}/flush_cache")
            return resp.status_code < 400
        except httpx.HTTPError:
            return False

    async def cache_hit_rate(self, client: httpx.AsyncClient) -> float | None:
        """Read the prefix cache hit rate from /metrics.

        Read rather than inferred: the whole argument for a long shared prefix
        is that it is cached, and this is the number that shows whether it is.
        """
        try:
            body = (await client.get(f"{self.url}/metrics")).text
        except httpx.HTTPError:
            return None
        # `sglang:cache_hit_rate` is an instantaneous gauge and reads 0 while
        # the server is idle, which is exactly when a run has just finished. The
        # cumulative counters are what a batch job wants: how much of everything
        # sent was served from the radix tree.
        #
        # Prometheus lines are `name{label="v",...} value`, so the labels have to
        # come off before the name can be matched.
        cached = prompt = None
        for line in body.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            name, _, value = line.rpartition(" ")
            name = name.split("{", 1)[0].strip()
            try:
                number = float(value)
            except ValueError:
                continue
            if name == "sglang:cached_tokens_total":
                cached = (cached or 0.0) + number     # summed across cache sources
            elif name == "sglang:prompt_tokens_total":
                prompt = number

        if cached is not None and prompt:
            return cached / prompt
        return None


async def extract_day_async(
    day: str,
    rows: list[dict],
    load_document,
    client: AsyncSGLangClient,
    schema: dict,
    *,
    concurrency: int = 32,
    model: str = "unknown",
) -> tuple[ExtractionRun, BatchStats]:
    """Run a day's filings concurrently, bounded by a semaphore.

    The semaphore caps requests in flight. SGLang queues beyond what its KV
    cache admits, so a value above the knee costs nothing but does not help --
    which is why day 4 sweeps for it rather than guessing.
    """
    prefix = build_prefix()
    run = ExtractionRun(day=day)
    sem = asyncio.Semaphore(concurrency)

    # Parsing is CPU work on this machine and independent of the GPU, so it is
    # done up front rather than inside the semaphore -- otherwise a slow parse
    # would hold a concurrency slot that the GPU could be using.
    filings = []
    for row in rows:
        try:
            filings.append(parse_filing(load_document(row["s3_key"]), accession=row["accession"]))
        except ParseError as exc:
            log.error("parse failed for %s: %s", row["accession"], exc)
            run.failures.append((row["accession"], str(exc)))

    async def one(http: httpx.AsyncClient, filing) -> None:
        prompt, truncated = build_prompt(filing, prefix)
        async with sem:
            started = time.monotonic()
            try:
                raw = await client.generate(http, prompt, schema)
            except ExtractionError as exc:
                log.error("extraction failed for %s: %s", filing.accession, exc)
                run.failures.append((filing.accession, str(exc)))
                return
            latency = time.monotonic() - started

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            run.failures.append((filing.accession, f"non-JSON output: {exc}"))
            return

        missing = set(schema.get("required", [])) - data.keys()
        if missing:
            run.failures.append((filing.accession, f"missing fields {sorted(missing)}"))
            return

        run.results.append(
            Extraction(
                accession=filing.accession,
                data=data,
                model=model,
                extracted_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                prompt_chars=len(prompt),
                latency_s=latency,
                truncated=truncated,
                primary_document=filing.primary_filename,
            )
        )

    limits = httpx.Limits(max_connections=concurrency + 8)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=client.timeout, limits=limits) as http:
        await asyncio.gather(*(one(http, f) for f in filings))
        wall = time.monotonic() - started
        hit_rate = await client.cache_hit_rate(http)

    stats = BatchStats(
        concurrency=concurrency,
        documents=len(run.results),
        failures=len(run.failures),
        wall_s=wall,
        cache_hit_rate=hit_rate,
        latencies=[r.latency_s for r in run.results],
    )
    return run, stats


async def sweep(
    day: str,
    rows: list[dict],
    load_document,
    client: AsyncSGLangClient,
    schema: dict,
    *,
    levels: tuple[int, ...] = (8, 16, 32, 64),
    model: str = "unknown",
    flush: bool = True,
) -> list[BatchStats]:
    """Run the same documents at several concurrency levels and report each.

    The knee is where docs/hour stops improving: past it, extra requests queue
    inside the server instead of running, so throughput flattens while latency
    keeps climbing.

    The radix cache is flushed before each level. Without that the comparison is
    not a comparison: after a level has run the documents their whole prompts
    sit in the cache, so the first level pays full prefill and every later one
    reads that work back. Measured on 40 filings, that alone made concurrency 8
    look 3.7x worse than 16.

    Flushing also makes the numbers representative rather than optimistic: a
    real night's filings are each seen once, so only the shared prefix is a hit,
    which is exactly the state a flushed cache reproduces.
    """
    out = []
    for level in levels:
        if flush:
            async with httpx.AsyncClient(timeout=30.0) as http:
                if not await client.flush_cache(http):
                    log.warning(
                        "could not flush cache before concurrency %d -- this "
                        "level starts warmer than the previous one, so the "
                        "comparison is not sound", level,
                    )
        _, stats = await extract_day_async(
            day, rows, load_document, client, schema,
            concurrency=level, model=model,
        )
        log.info("%s", stats.line())
        out.append(stats)
    return out
