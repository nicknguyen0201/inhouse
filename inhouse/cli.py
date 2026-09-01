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
    ext.add_argument("-v", "--verbose", action="store_true", help="debug logging")

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
