"""Form 4 (insider transaction) parsing.

A Form 4 is not authored as a document: the filer completes a web form and
EDGAR emits `ownership.xml`. All 425 Form 4s from 2026-08-27 contain exactly
one attachment and no rendered text version, so there is nothing to summarise
and no reason to involve a model -- the XML tag names are the schema.

    submission -> ownership.xml -> one row per transaction

The transaction code is the signal the dashboard hangs off:

    P  open-market purchase      S  open-market sale
    A  grant or award            M  option exercise
    F  shares withheld for tax   D  disposition to the issuer
    G  gift                      C  conversion

A large unscheduled `S` is interesting. Routine `A` grants are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from lxml import etree

from .parse import ParseError, TEXT_RE, primary_document

# Codes whose economics are a genuine open-market trade, as opposed to
# compensation mechanics. Used by the dashboard to separate signal from noise.
OPEN_MARKET_CODES = frozenset({"P", "S"})

CODE_MEANINGS = {
    "P": "purchase",
    "S": "sale",
    "A": "grant",
    "M": "option exercise",
    "F": "tax withholding",
    "D": "disposition to issuer",
    "G": "gift",
    "C": "conversion",
    "X": "option exercise",
    "J": "other",
}


@dataclass
class Transaction:
    """One reported transaction. Holdings are excluded -- see parse_form4."""

    issuer_cik: str
    issuer_name: str
    issuer_symbol: str | None
    insider_cik: str
    insider_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    officer_title: str | None
    security_title: str | None
    transaction_date: date | None
    code: str | None
    acquired_disposed: str | None      # "A" acquired / "D" disposed
    shares: float | None
    price_per_share: float | None
    shares_owned_after: float | None
    direct_ownership: bool | None      # D direct / I indirect
    derivative: bool = False
    footnotes: list[str] = field(default_factory=list)

    @property
    def value_usd(self) -> float | None:
        if self.shares is None or self.price_per_share is None:
            return None
        return round(self.shares * self.price_per_share, 2)

    @property
    def role(self) -> str:
        """Human-readable role, preferring the stated officer title."""
        if self.officer_title:
            return self.officer_title
        roles = []
        if self.is_director:
            roles.append("Director")
        if self.is_officer:
            roles.append("Officer")
        if self.is_ten_percent_owner:
            roles.append("10% Owner")
        return ", ".join(roles) or "Unknown"

    @property
    def is_open_market(self) -> bool:
        return self.code in OPEN_MARKET_CODES


@dataclass
class Form4:
    accession: str
    period: date | None
    issuer_cik: str
    issuer_name: str
    transactions: list[Transaction] = field(default_factory=list)
    holdings: int = 0                  # reported positions, not transactions
    footnotes: dict[str, str] = field(default_factory=dict)


# --- value helpers ---------------------------------------------------------
#
# Every amount in a Form 4 is wrapped: <transactionShares><value>86</value>.
# The wrapper carries footnote references, which is why the value is nested
# rather than being the element's own text.


def _value(node, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        return None
    inner = found.find("value")
    text = (inner if inner is not None else found).text
    return text.strip() if text and text.strip() else None


def _number(node, path: str) -> float | None:
    raw = _value(node, path)
    if raw is None:
        return None
    try:
        # Filers occasionally write "1,000" or "$12.50".
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _flag(node, path: str) -> bool:
    raw = _value(node, path)
    return raw in ("1", "true", "TRUE", "True")


def _date(node, path: str) -> date | None:
    raw = _value(node, path)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _footnote_ids(node) -> list[str]:
    return [
        ref.get("id")
        for ref in node.iter("footnoteId")
        if ref.get("id")
    ]


# --- parsing ---------------------------------------------------------------


def extract_xml(raw: str) -> str:
    """The ownership XML payload from inside the SGML envelope."""
    doc = primary_document(raw, "4")
    payload = TEXT_RE.search(doc)
    if not payload:
        raise ParseError("Form 4 has no <TEXT> payload")
    body = payload.group(1)

    start = body.find("<ownershipDocument")
    if start == -1:
        raise ParseError("no <ownershipDocument> in Form 4 payload")
    end = body.find("</ownershipDocument>", start)
    if end == -1:
        raise ParseError("unterminated <ownershipDocument>")
    return body[start:end + len("</ownershipDocument>")]


def parse_form4(raw: bytes | str, *, accession: str = "") -> Form4:
    """Parse one Form 4 submission into transactions.

    Holdings are counted but not returned as transactions. A
    `nonDerivativeHolding` reports a standing position -- it has no code, no
    date and no price -- so emitting one as a transaction would produce a row
    of nulls that looks like a failed parse.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    xml = extract_xml(raw)
    try:
        tree = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        raise ParseError(f"malformed ownership XML: {exc}") from exc

    issuer = tree.find("issuer")
    issuer_cik = (_value(issuer, "issuerCik") or "").zfill(10)
    issuer_name = _value(issuer, "issuerName") or ""
    issuer_symbol = _value(issuer, "issuerTradingSymbol")

    footnotes = {
        node.get("id"): " ".join((node.text or "").split())
        for node in tree.iter("footnote")
        if node.get("id")
    }

    # A filing can name several reporting owners (a fund and its manager, say).
    # The first is the subject; the rest are usually affiliated entities.
    owner = tree.find("reportingOwner")
    owner_id = owner.find("reportingOwnerId") if owner is not None else None
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    insider_cik = (_value(owner_id, "rptOwnerCik") or "").zfill(10)
    insider_name = _value(owner_id, "rptOwnerName") or ""

    common = dict(
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        issuer_symbol=issuer_symbol,
        insider_cik=insider_cik,
        insider_name=insider_name,
        is_director=_flag(rel, "isDirector"),
        is_officer=_flag(rel, "isOfficer"),
        is_ten_percent_owner=_flag(rel, "isTenPercentOwner"),
        officer_title=_value(rel, "officerTitle"),
    )

    transactions: list[Transaction] = []
    for tag, derivative in (
        ("nonDerivativeTransaction", False),
        ("derivativeTransaction", True),
    ):
        for node in tree.iter(tag):
            amounts = node.find("transactionAmounts")
            coding = node.find("transactionCoding")
            post = node.find("postTransactionAmounts")
            nature = node.find("ownershipNature")
            direct = _value(nature, "directOrIndirectOwnership")

            transactions.append(
                Transaction(
                    **common,
                    security_title=_value(node, "securityTitle"),
                    transaction_date=_date(node, "transactionDate"),
                    code=_value(coding, "transactionCode"),
                    acquired_disposed=_value(amounts, "transactionAcquiredDisposedCode"),
                    shares=_number(amounts, "transactionShares"),
                    price_per_share=_number(amounts, "transactionPricePerShare"),
                    shares_owned_after=_number(post, "sharesOwnedFollowingTransaction"),
                    direct_ownership=(direct == "D") if direct else None,
                    derivative=derivative,
                    footnotes=[footnotes[i] for i in _footnote_ids(node) if i in footnotes],
                )
            )

    holdings = sum(
        1
        for tag in ("nonDerivativeHolding", "derivativeHolding")
        for _ in tree.iter(tag)
    )

    return Form4(
        accession=accession,
        period=_date(tree, "periodOfReport"),
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        transactions=transactions,
        holdings=holdings,
        footnotes=footnotes,
    )
