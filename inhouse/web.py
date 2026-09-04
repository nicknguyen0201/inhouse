"""The dashboard as a live server.

Used for development: edit render.py, reload, see the change. What ships to
GitHub Pages is the static build in build.py, which calls the same functions.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

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
    _resolve_day,
    group_filings,
    rows_to_dicts,
)

app = FastAPI(title="inhouse", docs_url=None, redoc_url=None)


def _dsn() -> str:
    """Read DATABASE_URL, falling back to .env as the CLI does.

    uvicorn does not load .env, so without this the dashboard would need the
    variable exported while every other command reads it from the file.
    """
    _load_dotenv(Path(".env"))
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Put it in .env or export it:\n"
            "  DATABASE_URL=postgresql://user:pass@host:6543/postgres"
        )
    return dsn


@app.get("/", response_class=HTMLResponse)
def dashboard(
    day: str = Query(""),
    sector: str = Query(""),
    materiality: str = Query(""),
) -> str:
    conn = connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(DAYS_SQL)
            days = cur.fetchall()
            chosen = _resolve_day(day, [d for d, _ in days])

            cur.execute(SECTORS_SQL, (chosen,))
            sectors = cur.fetchall()

            cur.execute(ROWS_SQL, (chosen, sector, sector, materiality, materiality))
            rows = cur.fetchall()
    finally:
        conn.close()

    filings = group_filings(rows)
    n_txns = sum(1 for f in filings.values() if f["txns"])
    return _page(
        _header(chosen, len(filings), n_txns)
        + _filters(chosen, days, sectors, sector, materiality)
        + _body(filings),
        f"inhouse — {chosen}",
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True}
