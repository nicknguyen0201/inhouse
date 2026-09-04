"""Rendering and queries, shared by the live server and the static build.

The dashboard exists in two forms and neither is a copy of the other:

  web.py     a FastAPI app querying Postgres per request -- what you run
             locally while changing the layout
  build.py   the same functions run once in CI against the same database,
             emitting HTML for GitHub Pages

They share this module so the two cannot drift. The static build is the one
that ships: the data changes once a night, so serving it dynamically buys
nothing and would put a database credential on a public host.
"""

from __future__ import annotations

from datetime import date, timedelta
import re
from html import escape

ROWS_SQL = """
SELECT
    d.accession, d.company, d.sic, d.sic_description,
    COALESCE(s.sector, 'Unclassified')   AS sector,
    COALESCE(s.division, 'Unclassified') AS division,
    d.filed_at, d.source_url,
    d.event_type, d.direction, d.summary, d.materiality,
    d.facts_in_exhibit, d.primary_document,
    -- The Form 4's own accession and issuer CIK, so a transaction can link to
    -- the filing that reported it. `accession` above is the 8-K's.
    d.txn_accession, d.txn_cik,
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

# Days that have been extracted, with how much is on each.
#
# The join to `extractions` is the filter: `filings` gains rows the moment a day
# is ingested, and a day that was fetched but never extracted renders an empty
# page. Beyond that, no threshold -- an earlier version dropped days below a
# fraction of the median, which was arithmetic in place of a label. Showing the
# count says the same thing without deciding for the reader.
DAYS_SQL = """
SELECT f.filed_at::date AS day, count(*) AS n
FROM filings f
JOIN extractions e ON e.accession = f.accession
WHERE f.form_type = '8-K'
GROUP BY 1
ORDER BY 1 DESC
LIMIT 30
"""

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

.filters { padding:11px 24px; border-bottom:1px solid var(--rule);
           background:var(--wash); }
.navrow { display:flex; gap:9px; flex-wrap:wrap; align-items:baseline;
          margin-bottom:5px; }
.navrow:last-child { margin-bottom:0; }
.navlabel { color:var(--dim); min-width:74px; }
.nav-link { color:var(--dim); text-decoration:none; font-size:14px;
            border-bottom:1px solid transparent; }
.nav-link:hover { color:var(--ink); border-bottom-color:var(--rule); }
.nav-link.active { color:var(--ink); font-weight:600;
                   border-bottom:1px solid var(--ink); }
.nav-select { font:inherit; font-size:14px; padding:3px 6px; border:0;
              border-bottom:1px solid var(--ink); background:transparent;
              color:var(--ink); border-radius:0; max-width:260px; }
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
.txn-link { color:inherit; text-decoration:none;
            border-bottom:1px solid var(--rule); }
.txn-link:hover { border-bottom-color:currentColor; }
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


def filing_url(source_url: str | None, primary_document: str | None = None) -> str | None:
    """EDGAR's index page for the submission a summary describes.

    The stored source_url points at the raw SGML -- the exact bytes the model
    read, which is honest and unreadable: 1.7MB wrapping thirteen attachments.
    The index page lists those attachments as formatted documents:

        raw    /Archives/edgar/data/1308547/0001193125-26-369707.txt
        index  /Archives/edgar/data/1308547/0001193125-26-369707-index.htm

    Deliberately the index rather than EDGAR's inline-XBRL viewer, which would
    open the 8-K body directly. 65% of filings state their figures in an exhibit
    rather than the body, so a reader checking "released second quarter results"
    needs Exhibit 99, and only the index page offers it. One extra click buys
    the whole submission.

    `primary_document` is accepted and unused: it is what a viewer link would
    need, and the signature records that the choice was made rather than missed.
    """
    if not source_url:
        return None
    match = re.search(r"/edgar/data/(\d+)/([\d-]+)\.txt$", source_url)
    if not match:
        return source_url          # unrecognised shape: link what we have
    cik, accession = match.groups()
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}-index.htm"


def form4_url(cik: str | None, accession: str | None) -> str | None:
    """EDGAR's rendered Form 4 for a transaction.

    Unlike an 8-K, nothing needs storing: every Form 4 in the corpus has exactly
    one attachment named ownership.xml, because the filer completes a web form
    and EDGAR emits the XML itself. The viewer renders that XML as the familiar
    tabular Form 4.

    The link matters more here than for an 8-K. A transaction row says "sold
    289,624 shares"; the filing says those shares belonged to a deceased
    founder's estate. 88% of transactions carry a footnote that changes how the
    row should be read, and the dashboard shows at most two.
    """
    if not cik or not accession:
        return None
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}-index.htm"
    )


def _company_link(company: str, url: str | None, primary_document: str | None = None) -> str:
    """The headline links to the filing it summarises.

    Every summary here is a model's reading of a document, and it can be wrong.
    One click to the original is the difference between a claim and a citation.
    """
    name = escape(company)
    target = filing_url(url, primary_document)
    if not target:
        return name
    return (
        f"<a class=co-link href='{escape(target, quote=True)}' "
        f"target=_blank rel='noopener noreferrer' "
        f"title='Open this filing on sec.gov'>{name}</a>"
    )


def _page(body: str, title: str = "inhouse") -> str:
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=color-scheme content='dark light'>"
        f"<title>{escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )




def rows_to_dicts(cursor, rows) -> list[dict]:
    """Name the columns, using the cursor's own description.

    Positional unpacking made every schema change a landmine: adding a column to
    the view shifted every index after it, and the failure was a ValueError
    about tuple length rather than anything naming the column. The view is the
    contract; this reads it from the database instead of restating it here.
    """
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, r)) for r in rows]


def group_filings(rows: list[dict]) -> dict:
    """Collapse the join's fan-out back to one entry per filing.

    An 8-K matching three insider transactions arrives as three rows; the page
    shows one story with three transactions beneath it.
    """
    filings: dict[str, dict] = {}
    for r in rows:
        f = filings.setdefault(r["accession"], {"meta": r, "txns": []})
        if r.get("insider"):
            f["txns"].append(r)
    return filings


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


def _filters(day, days, sectors, sector, materiality, static: bool = False) -> str:
    """The controls.

    Two forms of the same thing. The live server posts a GET form; the static
    build cannot, so it emits links to pre-rendered pages. Links are arguably
    better: every filtered view is bookmarkable and works without JavaScript,
    which a client-side filter would not be.
    """
    if static:
        return _static_filters(day, days, sectors, sector, materiality)

    day_opts = "".join(
        f"<option value='{d}'{' selected' if d == day else ''}>{d} ({n})</option>"
        for d, n in days
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


def _static_filters(day, days, sectors, sector, materiality) -> str:
    from .build import page_name

    def link(label, target_day, target_sector, target_mat, active):
        href = page_name(target_day, target_sector, target_mat)
        cls = "nav-link active" if active else "nav-link"
        return f"<a class='{cls}' href='{href}'>{escape(label)}</a>"

    rows = []

    # Every day on one row, current one marked -- the same shape as the Section
    # and Materiality rows below it. The previous version stated the current day
    # as a sentence and hung "Earlier:" off it, which read as prose rather than
    # as a control and did not match the two rows underneath.
    if len(days) > 1:
        items = [
            link(f"{d:%-d %b} ({n})", d, "", "", d == day) for d, n in days
        ]
        rows.append(
            "<div class='navrow' data-collapsible='Edition'>"
            "<span class='navlabel rubric'>Edition</span>"
            + "".join(items) + "</div>"
        )

    # A list, not a dropdown. Sectors are a bounded set -- 26 in the hierarchy,
    # around 22 present on a given day -- so this row does not grow the way
    # editions do, and the counts beside each are worth seeing at a glance
    # rather than hidden behind a control that has to be opened.
    items = [link("All", day, "", materiality, not sector)]
    items += [
        link(f"{s} ({n})", day, s, "", s == sector)
        for s, n in sectors
    ]
    rows.append(
        "<div class=navrow><span class='navlabel rubric'>Section</span>"
        + "".join(items) + "</div>"
    )

    items = [link("Any", day, sector, "", not materiality)]
    items += [
        link(m.title(), day, "", m, m == materiality)
        for m in ("high", "medium", "low")
    ]
    rows.append(
        "<div class=navrow><span class='navlabel rubric'>Materiality</span>"
        + "".join(items) + "</div>"
    )

    return "<nav class=filters>" + "".join(rows) + "</nav>" + COLLAPSE_JS


# Phrases that change what a transaction means. Matched on the footnote text
# because the structured <aff10b5One> flag is set by some filers and not others.
SCHEDULED = ("10b5-1", "10b5‑1")
AVERAGED = ("weighted average",)


# Progressive enhancement. Each marked row ships as links and becomes a select
# on load; if the script does not run, the links are still there and still work,
# and every view keeps its own URL either way.
#
# Only Edition is marked. It is the row that grows without bound -- one entry
# per night the pipeline runs -- and a date picker is expected to be a picker.
# Section and Materiality are bounded sets whose counts are worth seeing.
COLLAPSE_JS = """<script>
(function () {
  document.querySelectorAll('[data-collapsible]').forEach(function (row) {
    var links = row.querySelectorAll('a.nav-link');
    if (!links.length) return;

    var select = document.createElement('select');
    select.className = 'nav-select';
    select.setAttribute('aria-label', row.dataset.collapsible);

    links.forEach(function (a) {
      var opt = document.createElement('option');
      opt.value = a.getAttribute('href');
      opt.textContent = a.textContent;
      if (a.classList.contains('active')) opt.selected = true;
      select.appendChild(opt);
    });

    select.addEventListener('change', function () {
      window.location.href = select.value;
    });

    // Only now that the replacement exists: a failure above leaves the links.
    links.forEach(function (a) { a.remove(); });
    row.appendChild(select);
  });
})();
</script>"""


def _txn_line(r) -> str:
    code = r["code"]
    shares, value = r["shares"], r["txn_value_usd"]
    insider, role, txn_date = r["insider"], r["role"], r["txn_date"]
    footnotes = r["footnotes"]
    link = form4_url(r.get("txn_cik"), r.get("txn_accession"))

    verb = "sold" if code == "S" else "bought"
    cls = "sale" if code == "S" else "buy"
    who = escape(insider or "")
    if link:
        who = (f"<a class=txn-link href='{escape(link, quote=True)}' "
               f"target=_blank rel='noopener noreferrer' "
               f"title='Open this Form 4 on sec.gov'>{who}</a>")
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
        m = f["meta"]
        company, sic, sector = m["company"], m["sic"], m["sector"]
        event, direction = m["event_type"], m["direction"]
        summary, materiality = m["summary"], m["materiality"]
        in_exhibit, source_url = m["facts_in_exhibit"], m["source_url"]
        primary_document = m.get("primary_document")

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
            f"<h2 class=co>{_company_link(company, source_url, primary_document)}</h2>"
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


