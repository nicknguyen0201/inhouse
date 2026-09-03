"""Command-line entry point: `python -m inhouse ingest --date YYYY-MM-DD`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from .config import Config, ConfigError
from .edgar import EdgarError
from .index import IndexParseError
from .ingest import ingest
from .sglang_client import DEFAULT_URL as SGLANG_URL
from .storage import open_storage


def _parse_date(value: str) -> date:
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM-DD or 'yesterday', got {value!r}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inhouse",
        description="Fetch a day of SEC filings into storage, with a manifest.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="fetch one day's 8-Ks and Form 4s")
    ing.add_argument(
        "--date", type=_parse_date, default="yesterday",
        help="filing date, YYYY-MM-DD (default: yesterday)",
    )
    ing.add_argument("--limit", type=int, help="stop after N filings (for smoke tests)")
    ing.add_argument("--force", action="store_true", help="re-download documents already stored")
    ing.add_argument("--storage", help="override STORAGE_URI, e.g. s3://bucket or ./data")
    ing.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    ext = sub.add_parser("extract", help="run a day's 8-Ks through the model")
    ext.add_argument(
        "--date", type=_parse_date, default="yesterday",
        help="filing date, YYYY-MM-DD (default: yesterday)",
    )
    ext.add_argument("--limit", type=int, help="stop after N filings")
    ext.add_argument("--storage", help="override STORAGE_URI")
    ext.add_argument(
        "--sglang-url", default=SGLANG_URL,
        help=f"SGLang server URL (default: {SGLANG_URL})",
    )
    ext.add_argument(
        "--concurrency", type=int, default=1,
        help="requests in flight (default 1 = sequential)",
    )
    ext.add_argument(
        "--sweep", metavar="N,N,...",
        help="sweep concurrency levels and report docs/hour, e.g. 8,16,32,64",
    )
    ext.add_argument(
        "--no-flush", action="store_true",
        help="do not clear SGLang's prefix cache between sweep levels (levels "
             "then differ in cache warmth, so results are not comparable)",
    )
    ext.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    ld = sub.add_parser("load", help="load a day into Postgres")
    ld.add_argument(
        "--date", type=_parse_date, default="yesterday",
        help="filing date, YYYY-MM-DD (default: yesterday)",
    )
    ld.add_argument("--storage", help="override STORAGE_URI")
    ld.add_argument("--dsn", help="Postgres DSN (default: $DATABASE_URL)")
    ld.add_argument(
        "--schema", action="store_true",
        help="apply sql/schema.sql before loading",
    )
    ld.add_argument(
        "--show", type=int, metavar="N",
        help="print the top N dashboard rows after loading",
    )
    ld.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    return parser


def _run_extract(args, config) -> int:
    from .extract import extract_day, load_schema, read_manifest, to_jsonl
    from .sglang_client import SGLangClient

    storage_uri = args.storage or config.storage_uri
    storage = open_storage(storage_uri)
    day = args.date

    client = SGLangClient(args.sglang_url)
    if not client.health():
        print(
            f"error: no SGLang server at {args.sglang_url}\n"
            f"Start one with:\n"
            f"  python -m sglang.launch_server --model-path Qwen/Qwen2.5-7B-Instruct-AWQ \\\n"
            f"    --host 0.0.0.0 --port 30000 --mem-fraction-static 0.85",
            file=sys.stderr,
        )
        return 2

    mkey = f"manifest/{day:%Y-%m-%d}.jsonl"
    if not storage.exists(mkey):
        print(f"error: no manifest at {storage.uri(mkey)} -- run `ingest` first", file=sys.stderr)
        return 2

    rows = read_manifest(storage.read(mkey).decode("utf-8"))
    print(f"extracting {len(rows)} 8-Ks from {day} via {args.sglang_url}")

    model = client.model_path()

    if args.sweep or args.concurrency > 1:
        return _run_extract_async(args, storage, rows, day, model, mkey)

    run = extract_day(
        f"{day:%Y-%m-%d}", rows, lambda key: storage.read(key), client,
        schema=load_schema(), model=model, limit=args.limit,
    )

    okey = f"extractions/{day:%Y-%m-%d}.jsonl"
    storage.put(okey, to_jsonl(run).encode("utf-8"))

    print()
    print(f"  extracted {run.ok}")
    print(f"  failed    {len(run.failures)}")
    print(f"  model     {model}")
    print(f"  output    {storage.uri(okey)}")
    hit = client.cache_hit_rate()
    if hit is not None:
        print(f"  cache hit {hit:.1%}")
    if run.results:
        avg = sum(r.latency_s for r in run.results) / len(run.results)
        print(f"  mean latency {avg:.2f}s")
    for acc, err in run.failures[:5]:
        print(f"    FAILED {acc}: {err}", file=sys.stderr)

    return 1 if run.failures else 0


def _run_extract_async(args, storage, rows, day, model, mkey) -> int:
    """Concurrent path: many requests in flight, optionally swept."""
    import asyncio

    from .batch import AsyncSGLangClient, extract_day_async, sweep
    from .extract import load_schema, to_jsonl

    schema = load_schema()
    client = AsyncSGLangClient(args.sglang_url)
    load = lambda key: storage.read(key)

    if args.limit is not None:
        rows = rows[: args.limit]

    if args.sweep:
        levels = tuple(int(x) for x in args.sweep.split(","))
        print(f"sweeping concurrency {levels} over {len(rows)} filings\n")
        results = asyncio.run(
            sweep(f"{day:%Y-%m-%d}", rows, load, client, schema,
                  levels=levels, model=model, flush=not args.no_flush)
        )
        print()
        for stats in results:
            print(stats.line())
        best = max(results, key=lambda s: s.docs_per_hour)
        print(f"\n  best: concurrency {best.concurrency} at {best.docs_per_hour:.0f} docs/hour")
        return 0

    run, stats = asyncio.run(
        extract_day_async(f"{day:%Y-%m-%d}", rows, load, client, schema,
                          concurrency=args.concurrency, model=model)
    )
    okey = f"extractions/{day:%Y-%m-%d}.jsonl"
    storage.put(okey, to_jsonl(run).encode("utf-8"))

    print()
    print(f"  extracted {run.ok}")
    print(f"  failed    {len(run.failures)}")
    print(f"  model     {model}")
    print(f"  output    {storage.uri(okey)}")
    print(stats.line())
    for acc, err in run.failures[:5]:
        print(f"    FAILED {acc}: {err}", file=sys.stderr)
    return 1 if run.failures else 0


def _run_load(args, config) -> int:
    import os

    from .db import apply_schema, connect, dashboard_rows, load_day

    dsn = args.dsn or os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print(
            "error: no database DSN. Set DATABASE_URL in .env or pass --dsn, e.g.\n"
            "  DATABASE_URL=postgresql://user:pass@host:5432/inhouse",
            file=sys.stderr,
        )
        return 2

    storage = open_storage(args.storage or config.storage_uri)
    day = args.date

    mkey = f"manifest/{day:%Y-%m-%d}.jsonl"
    if not storage.exists(mkey):
        print(f"error: no manifest at {storage.uri(mkey)} -- run `ingest` first", file=sys.stderr)
        return 2
    manifest = storage.read(mkey).decode("utf-8")

    ekey = f"extractions/{day:%Y-%m-%d}.jsonl"
    extractions = storage.read(ekey).decode("utf-8") if storage.exists(ekey) else ""
    if not extractions:
        print(f"note: no extractions at {storage.uri(ekey)} -- loading filings only")

    conn = connect(dsn)
    try:
        if args.schema:
            apply_schema(conn)
            print("  schema applied")
        counts = load_day(conn, manifest, extractions, lambda k: storage.read(k))
        print(f"  loaded {counts}")

        if args.show:
            rows = dashboard_rows(conn, f"{day:%Y-%m-%d}")
            print(f"\n  {len(rows)} dashboard rows for {day}\n")
            for r in rows[: args.show]:
                company, sic, _sicd, event, mat, summary = r[0], r[1], r[2], r[3], r[4], r[5]
                print(f"  {company[:38]:<40} SIC {sic or '----'}  [{mat.upper()}] {event}")
                print(f"      {summary[:110]}")
                if r[6]:
                    print(f"      Form 4 -- {r[6]} ({r[7] or 'insider'}) "
                          f"{r[8]} {r[9]:,.0f} shares on {r[11]}")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Retry chatter from urllib3 is noise at info level.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "extract":
        return _run_extract(args, config)
    if args.command == "load":
        return _run_load(args, config)

    day = args.date if isinstance(args.date, date) else _parse_date(args.date)
    storage_uri = args.storage or config.storage_uri
    storage = open_storage(storage_uri)

    print(f"ingesting {day} -> {storage_uri}")
    try:
        result = ingest(
            day, config, storage, limit=args.limit, force=args.force,
        )
    except (EdgarError, IndexParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted -- re-run the same command to resume", file=sys.stderr)
        return 130

    forms: dict[str, int] = {}
    for record in result.records:
        forms[record.form] = forms.get(record.form, 0) + 1

    print()
    print(f"  indexed   {result.indexed} index rows -> {result.documents} documents")
    print(f"  fetched   {result.fetched}")
    print(f"  skipped   {result.skipped} (already stored)")
    print(f"  failed    {result.failed}")
    print(f"  manifest  {result.manifest_key} ({result.stored} rows)")
    if forms:
        print("  by form   " + ", ".join(f"{k}: {v}" for k, v in sorted(forms.items())))
    with_sic = sum(1 for r in result.records if r.sic)
    if result.records:
        print(f"  with SIC  {with_sic}/{result.stored}")

    # Any failure is a non-zero exit, so a nightly cron notices.
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
