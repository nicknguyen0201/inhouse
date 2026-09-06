"""Render the dashboard to static HTML for GitHub Pages.

The data changes once a night, so serving it dynamically buys nothing. What it
would cost is a database credential on a public host and something to keep
running. A static build has neither: the pages are generated in CI, where the
credential already lives and never leaves, and GitHub serves the result.

    python -m inhouse.build --out site

One page per (day x sector) combination. That sounds like a lot and is not: a
day has ~22 sectors present, and each page is a few tens of kilobytes. The
alternative -- shipping the rows as JSON and filtering in the browser -- means
the page cannot be read without JavaScript and cannot be linked to a filtered
view, both of which a static site gets for free.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date
from pathlib import Path

from .config import _load_dotenv
from .db import connect
from .render import (
    DAYS_SQL,
    ROWS_SQL,
    SECTORS_SQL,
    _body,
    _filters,
    _header,
    _page,
    group_filings,
    rows_params,
    rows_to_dicts,
)

MATERIALITIES = ("", "high", "medium", "low")


def page_name(day: date, sector: str, materiality: str, insider: str = "") -> str:
    """Stable, linkable filenames.

    `index.html` is the unfiltered latest day; everything else is explicit, so a
    filtered view can be bookmarked and shared -- which a client-side filter
    would not allow.
    """
    parts = [f"{day:%Y-%m-%d}"]
    if sector:
        parts.append(sector.lower().replace(" & ", "-").replace(" ", "-"))
    if materiality:
        parts.append(materiality)
    if insider:
        parts.append("insider")
    return "-".join(parts) + ".html"


def build(conn, out: Path, days_limit: int = 5) -> int:
    """Write the site. Returns the number of pages written."""
    out.mkdir(parents=True, exist_ok=True)
    written = 0

    with conn.cursor() as cur:
        cur.execute(DAYS_SQL)
        # (date, extraction count) -- the count is shown beside each edition so
        # a thin day reads as thin rather than as a quiet news day.
        all_days = cur.fetchall()

    if not all_days:
        print("no filings in the database — nothing to build", file=sys.stderr)
        return 0

    # Only recent days: the archive grows without bound and nobody browses to
    # last March from a dropdown.
    days = all_days[:days_limit]

    for day, _count in days:
        with conn.cursor() as cur:
            cur.execute(SECTORS_SQL, (day,))
            sectors = cur.fetchall()

        # The unfiltered page, then one per sector, then one per materiality.
        # Cross-producting sector x materiality would be ~90 pages a day for
        # combinations nobody asks for.
        # (sector, materiality, insider-only). The last is its own page rather
        # than a cross-product: it is the join the project exists for, so it is
        # worth a URL, but crossing it with 22 sectors would be pages nobody
        # asks for.
        combos = (
            [("", "", "")]
            + [(s, "", "") for s, _ in sectors]
            + [("", m, "") for m in MATERIALITIES if m]
            + [("", "", "insider")]
        )

        for sector, materiality, insider in combos:
            with conn.cursor() as cur:
                cur.execute(ROWS_SQL, rows_params(day, sector, materiality, insider))
                rows = rows_to_dicts(cur, cur.fetchall())

            filings = group_filings(rows)
            n_txns = sum(1 for f in filings.values() if f["txns"])
            html = _page(
                _header(day, len(filings), n_txns,
                        insider_href=page_name(day, "", "", "insider"))
                + _filters(day, days, sectors, sector, materiality, static=True,
                           insider=insider)
                + _body(filings),
                f"inhouse — {day}",
            )
            (out / page_name(day, sector, materiality, insider)).write_text(
                html, encoding="utf-8")
            written += 1

    # The newest day, unfiltered, is the front page.
    shutil.copy(out / page_name(days[0][0], "", ""), out / "index.html")
    written += 1

    # Tell Pages not to run Jekyll over this -- it would otherwise ignore any
    # file starting with an underscore and rewrite things it does not need to.
    (out / ".nojekyll").write_text("")

    print(f"wrote {written} pages for {len(days)} day(s) to {out}/")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="site", help="output directory (default: site)")
    ap.add_argument("--days", type=int, default=5, help="how many recent days to build")
    ap.add_argument("--dsn", help="Postgres DSN (default: $DATABASE_URL)")
    args = ap.parse_args(argv)

    _load_dotenv(Path(".env"))
    dsn = args.dsn or os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    conn = connect(dsn)
    try:
        return 0 if build(conn, Path(args.out), args.days) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
