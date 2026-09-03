"""The dashboard: one page, server-rendered.

A table of a day's 8-Ks sorted by materiality, filterable by sector. No SPA --
the page is a sorted table and a couple of dropdowns, which is a day of React
that would add nothing.

The row that matters is the joined one:

    ACME CORP                                          Banking
      8-K   CFO departure, effective immediately        [HIGH]
      Form 4 - CEO sold 40,000 shares three days prior

Neither filing alone is remarkable. Finding the pair means correlating two form
types on CIK inside a date window, which is what the SQL below does.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from html import escape

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from .config import _load_dotenv
from .db import connect

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


# One query per page. The join windows insider transactions against the filing
# date, and footnotes come along because they routinely change what a
# transaction means -- a $11.5m sale turned out to be an estate settling a
# deceased founder's shares, and a Rule 10b5-1 plan means the timing was set
# months ago.
ROWS_SQL = """
SELECT
    d.accession, d.company, d.sic, d.sic_description,
    COALESCE(s.sector, 'Unclassified')   AS sector,
    COALESCE(s.division, 'Unclassified') AS division,
    d.filed_at, d.source_url,
    d.event_type, d.direction, d.summary, d.materiality,
    d.facts_in_exhibit,
    d.insider, d.role, d.code, d.shares, d.price_usd, d.txn_date,
    d.txn_value_usd, d.footnotes
FROM daily_dashboard d
LEFT JOIN sic_sectors s ON d.sic BETWEEN s.sic_from AND s.sic_to
WHERE d.filed_at::date = %s
  AND (%s = '' OR COALESCE(s.sector, 'Unclassified') = %s)
  AND (%s = '' OR d.materiality = %s)
ORDER BY
    CASE d.materiality WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
    d.txn_value_usd DESC NULLS LAST,
    d.company
"""

DAYS_SQL = "SELECT DISTINCT filed_at::date FROM filings ORDER BY 1 DESC LIMIT 30"

SECTORS_SQL = """
SELECT COALESCE(s.sector, 'Unclassified') AS sector, count(*) AS n
FROM daily_dashboard d
LEFT JOIN sic_sectors s ON d.sic BETWEEN s.sic_from AND s.sic_to
WHERE d.filed_at::date = %s
GROUP BY 1 ORDER BY 2 DESC, 1
"""

CSS = """
/* A newspaper, not an app. The conventions are borrowed on purpose: a serif
   face for anything a person reads as prose, a condensed sans in small caps
   for the apparatus around it (rubrics, bylines, labels), rules instead of
   boxes, and a measure narrow enough to read without tracking back.

   The one place it departs from print is materiality: a paper would signal
   importance with position and headline size. Here it is a coloured rubric,
   because the sort order is the position and the reader needs to see why. */
:root {
  --paper:#fbfaf7; --ink:#12100e; --dim:#6a6660; --rule:#d8d4cc;
  --hair:#ebe8e2; --high:#8c1c13; --med:#7a5c12; --accent:#1c3f5f;
  --buy:#1a5c38; --wash:#f3f1ec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper:#14140f; --ink:#eae7df; --dim:#8f8b82; --rule:#33322c;
    --hair:#26251f; --high:#e0847a; --med:#d6b062; --accent:#8fb6d8;
    --buy:#79c795; --wash:#1b1a15;
  }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
       font:17px/1.5 "Iowan Old Style", "Palatino Linotype", Palatino,
            Georgia, "Times New Roman", serif;
       -webkit-font-smoothing:antialiased; }

/* Small caps rubrics: the labels, sector names and kickers. */
.rubric { font-family:-apple-system, "Helvetica Neue", Arial, sans-serif;
          font-size:10.5px; font-weight:640; letter-spacing:.11em;
          text-transform:uppercase; }

/* --- Masthead ---------------------------------------------------------- */
.masthead { border-bottom:3px double var(--ink); padding:26px 24px 14px;
            text-align:center; }
.masthead h1 { margin:0; font-size:44px; font-weight:500; text-transform:uppercase;
               /* Caps need looser tracking than lowercase or they crowd --
                  the same reason a nameplate is letterspaced in print. */
               letter-spacing:.16em; text-indent:.16em;
               font-family:"Playfair Display","Iowan Old Style", Georgia, serif; }
.dateline { display:flex; justify-content:center; gap:14px; flex-wrap:wrap;
            margin-top:9px; padding-top:9px; border-top:1px solid var(--rule);
            color:var(--dim); }
.dateline span:not(:last-child)::after { content:" ·"; margin-left:14px;
                                         color:var(--rule); }

/* --- Controls, styled as a standing head rather than a form ------------ */
form { display:flex; gap:16px; flex-wrap:wrap; align-items:flex-end;
       padding:12px 24px; border-bottom:1px solid var(--rule);
       background:var(--wash); }
label { display:block; color:var(--dim); margin-bottom:3px; }
select { font:inherit; font-size:14px; padding:4px 6px; border:0;
         border-bottom:1px solid var(--ink); background:transparent;
         color:var(--ink); border-radius:0; }
button { font-family:-apple-system, Arial, sans-serif; font-size:11px;
         font-weight:640; letter-spacing:.1em; text-transform:uppercase;
         padding:7px 16px; border:1px solid var(--ink); background:var(--ink);
         color:var(--paper); cursor:pointer; }
button:hover { background:transparent; color:var(--ink); }

main { max-width:760px; margin:0 auto; padding:22px 24px 70px; }
.count { color:var(--dim); padding-bottom:10px; margin-bottom:6px;
         border-bottom:1px solid var(--rule); }

/* --- A filing, set as a story ------------------------------------------ */
.filing { padding:17px 0; border-bottom:1px solid var(--hair); }
.filing:last-child { border-bottom:0; }
.kicker { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap;
          margin-bottom:5px; }
.kicker .sector { color:var(--dim); }
.tag-high, .tag-medium, .tag-low { letter-spacing:.11em; }
.tag-high { color:var(--high); }
.tag-medium { color:var(--med); }
.tag-low { color:var(--dim); }
.co { font-size:21px; font-weight:600; line-height:1.25; margin:0 0 4px;
      letter-spacing:-.005em; }
.summary { margin:0; }
.summary-label { color:var(--dim); margin-right:7px; }
.co-link { color:inherit; text-decoration:none;
           border-bottom:1px solid var(--rule); }
.co-link:hover { border-bottom-color:var(--accent); color:var(--accent); }
.summary::first-letter { font-size:1em; }
.exhibit { color:var(--dim); font-size:14px; font-style:italic; margin:5px 0 0; }

/* --- The insider transaction: an inset note, the way a paper sets a
       sidebar related to the main story. ---------------------------------- */
.txn { margin-top:11px; padding:9px 0 2px 13px;
       border-left:2px solid var(--accent); }
.txn-line { font-size:15px; margin-bottom:2px; }
.sale { color:var(--high); font-weight:600; }
.buy  { color:var(--buy); font-weight:600; }
.when { color:var(--dim); font-size:14px; }
.scheduled { color:var(--dim); margin-left:6px; }
.note { color:var(--dim); font-size:13.5px; line-height:1.45; margin:4px 0 0;
        font-style:italic; }
.empty { color:var(--dim); padding:50px 0; text-align:center; }
a { color:var(--accent); }
"""


def _money(v) -> str:
    if v is None:
        return ""
    v = float(v)
    if v >= 1_000_000:
        return f"${v/1_000_000:,.1f}M"
    return f"${v:,.0f}"


def _company_link(company: str, url: str | None) -> str:
    """The headline links to the filing it summarises.

    Every summary here is a model's reading of a document, and it can be wrong.
    One click to the original is the difference between a claim and a citation.
    """
    name = escape(company)
    if not url:
        return name
    return (
        f"<a class=co-link href='{escape(url, quote=True)}' "
        f"target=_blank rel='noopener noreferrer'>{name}</a>"
    )


def _page(body: str, title: str = "inhouse") -> str:
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=color-scheme content='dark light'>"
        f"<title>{escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


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
            days = [r[0] for r in cur.fetchall()]
            chosen = _resolve_day(day, days)

            cur.execute(SECTORS_SQL, (chosen,))
            sectors = cur.fetchall()

            cur.execute(ROWS_SQL, (chosen, sector, sector, materiality, materiality))
            rows = cur.fetchall()
    finally:
        conn.close()

    # One filing can appear on several rows -- one per matching insider
    # transaction -- so group them back together for display.
    filings: dict[str, dict] = {}
    for r in rows:
        acc = r[0]
        f = filings.setdefault(acc, {"meta": r, "txns": []})
        if r[13]:            # insider column, after source_url shifted the row
            f["txns"].append(r)

    n_txns = sum(1 for f in filings.values() if f["txns"])
    return _page(
        _header(chosen, len(filings), n_txns)
        + _filters(chosen, days, sectors, sector, materiality)
        + _body(filings),
        f"inhouse — {chosen}",
    )


def _resolve_day(requested: str, days: list[date]) -> date:
    if requested:
        try:
            return date.fromisoformat(requested)
        except ValueError:
            pass
    return days[0] if days else date.today() - timedelta(days=1)


def _header(day: date, n_filings: int, n_txns: int) -> str:
    """Masthead and dateline, in the order a paper prints them."""
    return (
        "<div class=masthead><h1>inhouse</h1>"
        "<div class='dateline rubric'>"
        f"<span>{day:%A, %B %-d, %Y}</span>"
        "<span>SEC Edgar</span>"
        f"<span>{n_filings} filings</span>"
        f"<span>{n_txns} with insider activity</span>"
        "</div></div>"
    )


def _filters(day, days, sectors, sector, materiality) -> str:
    day_opts = "".join(
        f"<option value='{d}'{' selected' if d == day else ''}>{d}</option>"
        for d in days
    )
    sec_opts = "<option value=''>All sectors</option>" + "".join(
        f"<option value='{escape(s)}'{' selected' if s == sector else ''}>"
        f"{escape(s)} ({n})</option>"
        for s, n in sectors
    )
    mat_opts = "<option value=''>Any materiality</option>" + "".join(
        f"<option value='{m}'{' selected' if m == materiality else ''}>{m}</option>"
        for m in ("high", "medium", "low")
    )
    return (
        "<form method=get>"
        f"<div><label class=rubric>Date</label><select name=day>{day_opts}</select></div>"
        f"<div><label class=rubric>Section</label><select name=sector>{sec_opts}</select></div>"
        f"<div><label class=rubric>Materiality</label><select name=materiality>{mat_opts}</select></div>"
        "<button type=submit>Apply</button></form>"
    )


# Phrases that change what a transaction means. Matched on the footnote text
# because the structured <aff10b5One> flag is set by some filers and not others.
SCHEDULED = ("10b5-1", "10b5‑1")
AVERAGED = ("weighted average",)


def _txn_line(r) -> str:
    (_, _, _, _, _, _, _, _, _, _, _, _, _,
     insider, role, code, shares, price, txn_date, value, footnotes) = r

    verb = "sold" if code == "S" else "bought"
    cls = "sale" if code == "S" else "buy"
    who = escape(insider or "")
    what = f"{float(shares):,.0f} shares" if shares is not None else "shares"
    amount = f" worth {_money(value)}" if value else ""
    role_txt = f", {escape(role)}," if role else ""

    notes = [str(n) for n in (footnotes or [])]
    joined = " ".join(notes).lower()
    flags = ""
    if any(p in joined for p in SCHEDULED):
        # The most consequential qualifier on the page. A sale scheduled months
        # in advance says nothing about what the insider knew this week, and
        # without this line the reader would take it as a signal.
        flags = "<span class='scheduled rubric'>Scheduled · Rule 10b5-1</span>"
    elif any(p in joined for p in AVERAGED):
        flags = "<span class='scheduled rubric'>Average price</span>"

    note_html = "".join(f"<p class=note>{escape(n)}</p>" for n in notes[:2])
    return (
        f"<p class=txn-line><span class={cls}>{who}</span>{role_txt} "
        f"{verb} {what}{amount} "
        f"<span class=when>on {txn_date:%B %-d}</span>{flags}</p>{note_html}"
    )


def _body(filings: dict) -> str:
    if not filings:
        return "<main><p class=empty>No filings for this day and section.</p></main>"

    with_txn = sum(1 for f in filings.values() if f["txns"])
    out = [
        "<main>",
        f"<p class='count rubric'>{len(filings)} filings · "
        f"{with_txn} with insider transactions in the preceding five days</p>",
    ]

    for f in filings.values():
        (_acc, company, sic, _sic_desc, sector, _div, _filed, source_url,
         event, direction, summary, materiality, in_exhibit) = f["meta"][:13]

        # The kicker carries the apparatus -- section, event type, importance --
        # so the headline itself can be just the company name.
        kicker = [f"<span class='sector rubric'>{escape(sector or '')}</span>"]
        if event:
            ev = escape(event.replace("_", " "))
            if direction:
                ev += f" · {escape(direction)}"
            kicker.append(f"<span class='sector rubric'>{ev}</span>")
        kicker.append(
            f"<span class='tag-{materiality} rubric'>{escape(materiality)}</span>"
        )

        out.append(
            "<article class=filing>"
            f"<div class=kicker>{''.join(kicker)}</div>"
            f"<h2 class=co>{_company_link(company, source_url)}</h2>"
            f"<p class=summary><span class='summary-label rubric'>Summary</span>"
            f"{escape(summary)}</p>"
        )
        if in_exhibit:
            out.append(
                "<p class=exhibit>Figures are in an attached exhibit rather "
                "than the filing itself.</p>"
            )
        if f["txns"]:
            out.append("<div class=txn>" + "".join(_txn_line(t) for t in f["txns"]) + "</div>")
        out.append("</article>")

    out.append("</main>")
    return "".join(out)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
