-- inhouse: day 5 schema.
--
-- Three tables, one join. The join is the product: an 8-K saying the CFO left
-- is a filing; the same 8-K with the CEO having sold 40,000 shares three days
-- earlier is a story.
--
--   filings       what was filed, by whom, when      (from the manifest)
--   extractions   what the model made of an 8-K      (from extract)
--   insider_txns  what an insider actually did       (from form4)

CREATE TABLE IF NOT EXISTS filings (
    accession        text        NOT NULL,
    -- One submission is listed once per PARTY to it: a Form 4 appears under the
    -- issuer's CIK and again under the insider's, sharing one accession. 425 of
    -- 606 documents on 2026-08-27 did. So the key is (accession, cik), not
    -- accession alone -- and keeping both rows is what makes the day-5 join
    -- possible without re-parsing Form 4 XML.
    cik              char(10)    NOT NULL,
    company          text        NOT NULL,
    form_type        text        NOT NULL,
    filed_at         timestamp   NOT NULL,
    -- Individual filers (the people behind Form 4s) have no SIC at all, so this
    -- is null for roughly 44% of rows. Not a failure -- the dashboard reaches
    -- the issuer's SIC through the CIK relationship.
    sic              char(4),
    sic_description  text,
    s3_key           text        NOT NULL,
    source_url       text,
    PRIMARY KEY (accession, cik)
);

CREATE INDEX IF NOT EXISTS filings_day_idx    ON filings ((filed_at::date));
CREATE INDEX IF NOT EXISTS filings_cik_idx    ON filings (cik, filed_at);
CREATE INDEX IF NOT EXISTS filings_sic_idx    ON filings (substr(sic, 1, 2));
CREATE INDEX IF NOT EXISTS filings_accession_idx ON filings (accession);


-- What the model made of an 8-K.
--
-- Deliberately thin. Everything the SEC already states -- the item codes, the
-- SIC, the filer, the date -- lives on `filings` where it is exact. This table
-- holds only what is locked in prose and cannot be looked up, because every
-- column here is a guess that can be wrong and that the reader has to verify
-- against the filing.
--
-- Two fields were dropped after measuring them on real output:
--
--   amounts   populated on 4 of 20 filings. 65% of 8-Ks state their figures in
--             an exhibit the model never sees, so the column was empty far more
--             often than not, and a sparse numeric field invites being summed.
--   themes    36 distinct values across 48 extractions -- nearly one per
--             filing. Free text drifts exactly as the README warns event_type
--             would, so it grouped nothing and could not be filtered on. Sector
--             comes from SIC, which is exact.
--
-- What remains is the summary a human reads, a coarse sort key, and the
-- classification that constrained decoding actually makes reliable.
CREATE TABLE IF NOT EXISTS extractions (
    accession        text        PRIMARY KEY,
    -- Constrained decoding makes this one of a fixed set rather than free text,
    -- which is what stops "CFO departure" / "officer resignation" drift.
    event_type       text        NOT NULL,
    -- Item 5.02 bundles arrivals and departures, and the difference is the
    -- whole point of the filing. Null for every other event_type.
    direction        text,
    -- The reason the GPU is here: two sentences of prose that a person reads
    -- instead of opening the filing.
    summary          text        NOT NULL,
    -- A sort key, not a fact. 75% of a sample day came back 'medium', so treat
    -- it as "which few are worth looking at first" rather than a measurement.
    materiality      text        NOT NULL,
    -- True when the substance is in an exhibit rather than the filing body --
    -- 65% of a sample day. This is what tells a reader the summary is thin
    -- because the filing was, not because extraction failed.
    facts_in_exhibit boolean     NOT NULL DEFAULT false,
    -- The document was longer than the prompt budget, so the extraction saw
    -- only its head.
    truncated        boolean     NOT NULL DEFAULT false,
    -- The filename of the document inside the submission, e.g. hrl-20260827.htm.
    -- EDGAR's inline-XBRL viewer needs it to open the filing itself rather than
    -- an index of the submission's thirteen attachments, and it is arbitrary
    -- per filer -- there is no way to derive it from the accession.
    primary_document text,
    -- Provenance. The schema will change -- it already has once -- and you need
    -- to know which rows came from which version rather than re-running
    -- everything to find out.
    model            text        NOT NULL,
    extracted_at     timestamp   NOT NULL,
    latency_s        numeric(8,3),

    CONSTRAINT extractions_materiality_ck
        CHECK (materiality IN ('high', 'medium', 'low')),
    CONSTRAINT extractions_direction_ck
        CHECK (direction IS NULL OR direction IN ('departure', 'appointment', 'both'))
);

-- Materiality sorts the dashboard, so it must sort by consequence rather than
-- alphabetically ('high' < 'low' < 'medium' as text, which is nonsense).
CREATE INDEX IF NOT EXISTS extractions_materiality_idx ON extractions (
    (CASE materiality WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END)
);
CREATE INDEX IF NOT EXISTS extractions_event_idx ON extractions (event_type);


CREATE TABLE IF NOT EXISTS insider_txns (
    accession        text        NOT NULL,
    -- Position within the filing, from the XML's document order. Together with
    -- accession this is a real natural key: no surrogate id, no nullable
    -- columns, and it distinguishes two identical transactions in one filing.
    txn_index        int         NOT NULL,
    -- The ISSUER's CIK, which is what the join keys on -- the insider's own CIK
    -- carries no SIC and is not what a sector filter looks up.
    cik              char(10)    NOT NULL,
    issuer_name      text        NOT NULL,
    issuer_symbol    text,
    insider_cik      char(10)    NOT NULL,
    insider          text        NOT NULL,
    role             text,
    is_director      boolean     NOT NULL DEFAULT false,
    is_officer       boolean     NOT NULL DEFAULT false,
    is_ten_pct       boolean     NOT NULL DEFAULT false,
    security_title   text,
    -- P purchase, S sale, A grant, M option exercise, F tax withholding,
    -- D disposition to issuer, G gift, C conversion. A large unscheduled S is
    -- the signal; routine A grants are not.
    code             char(1),
    acquired_disposed char(1),
    -- numeric, not float: 26,000,000 shares at 4 decimal places appears in real
    -- filings, and float would quietly lose the tail on a value_usd product.
    shares           numeric(18,4),
    price_usd        numeric(14,4),
    shares_after     numeric(18,4),
    direct_ownership boolean,
    -- Options and RSUs. Kept rather than dropped: an exercise is a real event,
    -- but the dashboard's headline signal is non-derivative P/S.
    derivative       boolean     NOT NULL DEFAULT false,
    txn_date         date        NOT NULL,
    -- 88% of transactions reference a footnote, and footnotes change meaning:
    -- one price is a yen conversion, another marks shares held in trust.
    footnotes        jsonb       NOT NULL DEFAULT '[]',

    -- A previous version keyed on (accession, insider_cik, txn_date, code,
    -- shares, price_usd, derivative). It silently failed: NULL is never equal
    -- to NULL in SQL, so the 447 rows with no price -- grants and dispositions
    -- to the issuer -- were treated as distinct on every reload and inserted
    -- again. 1,135 transactions had become 1,405, and 447 of them collapsed to
    -- 140 once price was ignored.
    PRIMARY KEY (accession, txn_index)
);

CREATE INDEX IF NOT EXISTS insider_txns_join_idx ON insider_txns (cik, txn_date);
CREATE INDEX IF NOT EXISTS insider_txns_code_idx ON insider_txns (code)
    WHERE code IN ('P', 'S');


-- The dashboard's query: yesterday's 8-Ks, most material first, with any
-- insider transaction by the same issuer in the preceding week attached.
--
-- LEFT JOIN because most 8-Ks have no matching insider activity -- the ones
-- that do are the point, but the others still belong in the table.
CREATE OR REPLACE VIEW daily_dashboard AS
SELECT
    f.accession,
    f.cik,
    f.company,
    f.sic,
    f.sic_description,
    f.filed_at,
    -- So the dashboard can link a headline back to the filing it summarises.
    -- Every summary on the page is a model's reading of a document, and the
    -- reader has to be one click from checking it.
    f.source_url,
    e.event_type,
    e.direction,
    e.summary,
    e.materiality,
    e.facts_in_exhibit,
    -- Lets the dashboard deep-link to the filing itself rather than to an index
    -- of the submission's attachments.
    e.primary_document,
    -- The Form 4's own accession and the issuer's CIK, so the dashboard can
    -- link the transaction to the filing that reported it. `accession` above is
    -- the 8-K's; these two identify a different document entirely.
    i.accession  AS txn_accession,
    i.cik        AS txn_cik,
    i.insider,
    i.role,
    i.code,
    i.shares,
    i.price_usd,
    i.txn_date,
    (i.shares * i.price_usd)  AS txn_value_usd,
    -- Surfaced rather than interpreted. 88% of transactions carry a footnote
    -- and two patterns change how the row should be read:
    --
    --   "weighted average price"  the price is an average across several
    --                             trades, so txn_value_usd is approximate
    --   "Rule 10b5-1 trading plan"  the sale was scheduled months in advance,
    --                             so its timing says nothing about what the
    --                             insider knew this week
    --
    -- That second one matters more than anything else in this view: an
    -- unscheduled sale before an 8-K is the story, a pre-scheduled one is
    -- noise, and the transaction code alone cannot tell them apart.
    i.footnotes
FROM extractions e
JOIN filings f
      ON f.accession = e.accession
     AND f.form_type = '8-K'
LEFT JOIN insider_txns i
      ON i.cik = f.cik
     AND i.txn_date BETWEEN f.filed_at::date - INTERVAL '5 days' AND f.filed_at::date
     -- Compensation mechanics are noise; an open-market trade is the signal.
     AND i.code IN ('P', 'S')
     AND NOT i.derivative;


-- ---------------------------------------------------------------------------
-- Sector hierarchy.
--
-- The dashboard serves analysts who each cover one sector, so "show me banking"
-- has to be a selectable thing rather than a prefix the user is expected to
-- know. One day of filings spans 150 distinct SIC codes in 48 major groups --
-- far too many for a dropdown, and the raw codes mean nothing to a reader.
--
-- This is the one place a lookup table earns its place. Elsewhere the schema
-- denormalises (sic_description sits on `filings`) because those strings are
-- only ever printed. A hierarchy is different: the query traverses it, so it
-- has to be data.
--
-- Ranges rather than a row per SIC code: the taxonomy is defined as contiguous
-- blocks, and 150 codes appear on a single day against ~1,000 in existence.
-- Enumerating every code would mean maintaining a list that is mostly absent
-- from any given night's filings.

CREATE TABLE IF NOT EXISTS sic_sectors (
    sector       text     PRIMARY KEY,   -- what the user picks
    sic_from     char(4)  NOT NULL,      -- inclusive
    sic_to       char(4)  NOT NULL,      -- inclusive
    -- SIC divisions are the coarsest official level (A-J). Kept so the UI can
    -- offer two depths -- "Finance" or specifically "Banking" -- without a
    -- second table.
    division     text     NOT NULL,
    sort_order   int      NOT NULL DEFAULT 100,

    CONSTRAINT sic_sectors_range_ck CHECK (sic_from <= sic_to)
);

INSERT INTO sic_sectors (sector, sic_from, sic_to, division, sort_order) VALUES
    ('Agriculture',              '0100', '0999', 'Agriculture',    10),
    ('Mining & Energy',          '1000', '1499', 'Mining',         20),
    ('Construction',             '1500', '1799', 'Construction',   30),
    ('Food & Beverage',          '2000', '2199', 'Manufacturing',  40),
    ('Chemicals & Pharma',       '2800', '2899', 'Manufacturing',  50),
    ('Industrial Manufacturing', '2200', '2799', 'Manufacturing',  60),
    ('Energy & Materials',       '2900', '3499', 'Manufacturing',  70),
    ('Technology Hardware',      '3500', '3699', 'Manufacturing',  80),
    ('Medical Devices',          '3800', '3899', 'Manufacturing',  90),
    ('Other Manufacturing',      '3700', '3799', 'Manufacturing', 100),
    ('Misc Manufacturing',       '3900', '3999', 'Manufacturing', 110),
    ('Transport & Utilities',    '4000', '4999', 'Transportation',120),
    ('Wholesale',                '5000', '5199', 'Wholesale',     130),
    ('Retail',                   '5200', '5999', 'Retail',        140),
    ('Banking',                  '6000', '6199', 'Finance',       150),
    ('Insurance',                '6300', '6499', 'Finance',       160),
    ('Real Estate',              '6500', '6599', 'Finance',       170),
    ('Investment & Holding',     '6700', '6799', 'Finance',       180),
    ('Financial Services',       '6200', '6299', 'Finance',       190),
    ('Hotels & Leisure',         '7000', '7099', 'Services',      200),
    ('Software & IT Services',   '7370', '7379', 'Services',      210),
    ('Business Services',        '7200', '7369', 'Services',      220),
    ('Healthcare Services',      '8000', '8099', 'Services',      230),
    ('Other Services',           '7380', '7999', 'Services',      240),
    ('Professional Services',    '8100', '8999', 'Services',      250),
    ('Public Administration',    '9100', '9999', 'Public Admin',  260)
ON CONFLICT (sector) DO NOTHING;

-- Range lookup: `WHERE :sic BETWEEN sic_from AND sic_to`.
CREATE INDEX IF NOT EXISTS sic_sectors_range_idx ON sic_sectors (sic_from, sic_to);


-- Resolves a filing's SIC to its sector. A LEFT JOIN and a nullable result on
-- purpose: 44% of manifest rows are individual Form 4 filers with no SIC at
-- all, and a code outside every range should surface as "Unclassified" rather
-- than vanishing from the dashboard.
CREATE OR REPLACE VIEW filing_sectors AS
SELECT
    f.accession,
    f.cik,
    f.sic,
    f.sic_description,
    COALESCE(s.sector,   'Unclassified') AS sector,
    COALESCE(s.division, 'Unclassified') AS division,
    s.sort_order
FROM filings f
LEFT JOIN sic_sectors s
       ON f.sic IS NOT NULL
      AND f.sic BETWEEN s.sic_from AND s.sic_to;
