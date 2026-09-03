"""Concurrency tests. No GPU and no network: httpx is stubbed."""

import asyncio
import json

import pytest

from inhouse.batch import AsyncSGLangClient, BatchStats, extract_day_async, sweep
from inhouse.extract import load_schema

SCHEMA = load_schema()

VALID = json.dumps({
    "event_type": "dividend",
    "direction": None,
    "summary": "A company declared a quarterly cash dividend payable next month.",
    "materiality": "low",
    "entities": ["Example Corp"],
    "amounts": [],
    "themes": ["capital return"],
    "facts_in_exhibit": False,
})

DOC = b"""<SEC-HEADER>x
</SEC-HEADER>
<DOCUMENT>
<TYPE>8-K
<TEXT>
<html><body><p>Item 8.01 Other Events. The board declared a dividend.</p></body></html>
</TEXT>
</DOCUMENT>"""


class FakeAsyncClient(AsyncSGLangClient):
    """Records concurrency so the semaphore can be asserted on."""

    def __init__(self, delay=0.01, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.flushes = 0
        self.prompts = []

    async def generate(self, http, prompt, schema):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls += 1
        self.prompts.append(prompt)
        try:
            await asyncio.sleep(self.delay)
            return VALID
        finally:
            self.in_flight -= 1

    async def cache_hit_rate(self, http):
        return 0.68

    async def flush_cache(self, http):
        self.flushes += 1
        return True


def rows(n):
    return [{"accession": f"acc-{i:04d}", "s3_key": f"raw/d/{i}.txt"} for i in range(n)]


def load(_key):
    return DOC


def run_async(**kw):
    client = kw.pop("client")
    return asyncio.run(
        extract_day_async("2026-08-27", kw.pop("rows"), load, client, SCHEMA, **kw)
    )


# --- concurrency -----------------------------------------------------------


def test_requests_run_concurrently():
    """The point of the module: many in flight, not one at a time."""
    client = FakeAsyncClient(delay=0.05)
    run, stats = run_async(rows=rows(20), client=client, concurrency=8)
    assert run.ok == 20
    assert client.peak > 1, "requests were serialised"


def test_semaphore_caps_requests_in_flight():
    client = FakeAsyncClient(delay=0.05)
    run_async(rows=rows(30), client=client, concurrency=4)
    assert client.peak <= 4


def test_concurrency_of_one_is_sequential():
    client = FakeAsyncClient(delay=0.01)
    run_async(rows=rows(10), client=client, concurrency=1)
    assert client.peak == 1


def test_every_document_is_submitted_exactly_once():
    client = FakeAsyncClient()
    run, _ = run_async(rows=rows(25), client=client, concurrency=8)
    assert client.calls == 25
    assert len({r.accession for r in run.results}) == 25


# --- the shared prefix -----------------------------------------------------


def test_prefix_stays_byte_identical_under_concurrency():
    """Prefix caching is the throughput argument; a varying prefix silently
    costs it while the pipeline still appears to work."""
    from inhouse.extract import build_prefix

    client = FakeAsyncClient()
    run_async(rows=rows(12), client=client, concurrency=6)
    prefix = build_prefix()
    assert len({p[: len(prefix)] for p in client.prompts}) == 1


# --- failure handling ------------------------------------------------------


class FlakyClient(FakeAsyncClient):
    def __init__(self, fail_on, **kw):
        super().__init__(**kw)
        self.fail_on = fail_on

    async def generate(self, http, prompt, schema):
        self.calls += 1
        if self.calls in self.fail_on:
            from inhouse.extract import ExtractionError

            raise ExtractionError("simulated server error")
        return VALID


def test_one_failure_does_not_sink_the_batch():
    client = FlakyClient(fail_on={3, 7})
    run, stats = run_async(rows=rows(10), client=client, concurrency=4)
    assert run.ok == 8
    assert len(run.failures) == 2 == stats.failures


def test_unparseable_document_is_reported_not_raised():
    client = FakeAsyncClient()
    bad = lambda key: b"<SEC-HEADER>no primary document</SEC-HEADER>"
    run, _ = asyncio.run(
        extract_day_async("2026-08-27", rows(3), bad, client, SCHEMA, concurrency=2)
    )
    assert run.ok == 0 and len(run.failures) == 3
    # Parsing happens before the semaphore, so no GPU time is wasted on it.
    assert client.calls == 0


def test_malformed_json_is_a_failure_not_a_crash():
    class Garbage(FakeAsyncClient):
        async def generate(self, http, prompt, schema):
            return "certainly! here is your JSON:"

    run, _ = run_async(rows=rows(4), client=Garbage(), concurrency=2)
    assert run.ok == 0 and len(run.failures) == 4


def test_missing_required_field_is_a_failure():
    class Partial(FakeAsyncClient):
        async def generate(self, http, prompt, schema):
            return json.dumps({"event_type": "other", "summary": "x" * 50})

    run, _ = run_async(rows=rows(3), client=Partial(), concurrency=2)
    assert run.ok == 0 and len(run.failures) == 3


# --- stats -----------------------------------------------------------------


def test_stats_record_what_day_four_asks_for():
    client = FakeAsyncClient(delay=0.02)
    _, stats = run_async(rows=rows(12), client=client, concurrency=4)
    assert stats.documents == 12
    assert stats.concurrency == 4
    assert stats.docs_per_hour > 0
    assert stats.mean_latency > 0
    assert stats.cache_hit_rate == 0.68
    assert "docs/hr" in stats.line()


def test_docs_per_hour_is_zero_without_wall_time():
    assert BatchStats(concurrency=1, documents=5, failures=0, wall_s=0).docs_per_hour == 0.0


def test_p90_latency_is_reported():
    stats = BatchStats(
        concurrency=8, documents=10, failures=0, wall_s=10.0,
        latencies=[float(i) for i in range(1, 11)],
    )
    assert stats.p90_latency == 9.0


# --- sweep -----------------------------------------------------------------


def test_sweep_runs_every_level_and_reports_each():
    client = FakeAsyncClient(delay=0.005)
    results = asyncio.run(
        sweep("2026-08-27", rows(8), load, client, SCHEMA, levels=(2, 4, 8),
              flush=False)
    )
    assert [s.concurrency for s in results] == [2, 4, 8]
    assert all(s.documents == 8 for s in results)
    # Every level processed the full set, so the sweep is comparable.
    assert client.calls == 24


def test_sweep_flushes_the_cache_before_each_level():
    """Otherwise the first level pays full prefill and every later one reads
    that work back out of the radix cache -- not a comparison."""
    client = FakeAsyncClient(delay=0.005)
    results = asyncio.run(
        sweep("2026-08-27", rows(8), load, client, SCHEMA, levels=(2, 4))
    )
    assert client.flushes == 2, "expected one flush per level"
    assert [s.concurrency for s in results] == [2, 4]
    # No warmup pass: every request is a measured one.
    assert client.calls == 16


def test_sweep_can_skip_flushing():
    client = FakeAsyncClient(delay=0.005)
    asyncio.run(sweep("2026-08-27", rows(4), load, client, SCHEMA,
                      levels=(2,), flush=False))
    assert client.flushes == 0


def test_sweep_continues_when_the_flush_endpoint_is_unavailable():
    """An older server without /flush_cache should warn, not abort the sweep."""
    class NoFlush(FakeAsyncClient):
        async def flush_cache(self, http):
            return False

    client = NoFlush(delay=0.005)
    results = asyncio.run(
        sweep("2026-08-27", rows(4), load, client, SCHEMA, levels=(2, 4))
    )
    assert [s.concurrency for s in results] == [2, 4]
