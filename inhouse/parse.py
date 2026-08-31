"""Reduce a raw EDGAR submission to the text worth sending to a model.

An 8-K arrives as an SGML envelope around a dozen-odd attachments: the filing
itself, exhibits, XBRL taxonomy files, an embedded logo. On 2026-08-27 the
median submission was 13 attachments and the primary document was ~2% of the
bytes. Everything here is about finding that 2% and turning it into clean text.

    raw bytes -> primary <DOCUMENT> -> HTML -> text -> trimmed to the Items

Nothing in this module interprets the filing. It decides what text the model
sees; deciding what the text *means* is the model's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import lxml.html

# --- SGML envelope ---------------------------------------------------------

DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S)
TYPE_RE = re.compile(r"^<TYPE>(.+?)\s*$", re.M)
FILENAME_RE = re.compile(r"^<FILENAME>(.+?)\s*$", re.M)
TEXT_RE = re.compile(r"<TEXT>(.*)", re.S)
HEADER_END = "</SEC-HEADER>"

ITEM_INFORMATION_RE = re.compile(r"^ITEM INFORMATION:\s*(.+)$", re.M)
HEADER_SIC_RE = re.compile(r"STANDARD INDUSTRIAL CLASSIFICATION:.*?\[(\d{4})\]")
PERIOD_RE = re.compile(r"^CONFORMED PERIOD OF REPORT:\s*(\d{8})$", re.M)

# An 8-K's substance starts at its first numbered item. Everything above is the
# cover page -- address block, telephone number, and the four Rule 425 / 14a-12
# checkboxes -- which is near-identical across filers and runs ~2,100 characters
# (median, measured over 181 filings). It is pure prompt tax: it says nothing
# about the event and pushes the real content away from the start.
#
# "Item. 8.01" with a stray period is rare but real, hence the optional dot.
ITEM_RE = re.compile(r"Item\.?\s+(\d\.\d\d)", re.I)

# Blocks whose text should never run together with their neighbours.
_BLOCK_TAGS = (
    "p", "div", "br", "tr", "td", "th", "li", "table",
    "h1", "h2", "h3", "h4", "h5", "h6",
)


class ParseError(Exception):
    """The submission did not contain a usable primary document."""


@dataclass
class Filing:
    """A submission reduced to what downstream stages need."""

    accession: str
    form: str
    text: str                       # cleaned narrative, ready for a prompt
    items: list[str] = field(default_factory=list)      # "5.02" from the body
    item_descriptions: list[str] = field(default_factory=list)  # SEC's own labels
    sic: str | None = None
    period: str | None = None
    primary_filename: str | None = None
    attachments: int = 0
    raw_bytes: int = 0
    exhibits: list[str] = field(default_factory=list)   # EX-99 text, if included

    @property
    def chars(self) -> int:
        return len(self.text)


# --- envelope splitting ----------------------------------------------------


def split_documents(raw: str) -> list[str]:
    """Every <DOCUMENT> block in the submission, in order."""
    return DOCUMENT_RE.findall(raw)


def primary_document(raw: str, form: str) -> str:
    """The attachment whose <TYPE> is the form itself.

    Selection is by TYPE, not by looking for HTML: the XBRL viewer files
    (R1.htm, report.css) are also HTML, and exhibits often carry the substance
    in a second HTML document. Verified against all 181 8-Ks in 2026-08-27 --
    every one has exactly one attachment of its own form type.
    """
    for doc in split_documents(raw):
        match = TYPE_RE.search(doc)
        if match and match.group(1).strip().upper() == form.upper():
            return doc
    raise ParseError(f"no <TYPE>{form} attachment in submission")


def header(raw: str) -> str:
    end = raw.find(HEADER_END)
    return raw[:end] if end != -1 else raw[:4000]


# --- HTML to text ----------------------------------------------------------


def html_to_text(payload: str) -> str:
    """Extract readable text from a filing's HTML payload.

    Three things this handles that a tag-strip does not:

      - Inline XBRL hides a block of machine-readable metadata behind
        `display:none`. It is invisible to a reader but sits at the very top of
        the markup, so a naive strip opens every prompt with a run of CIKs and
        dates.
      - `.text_content()` concatenates adjacent elements with no separator, so
        "Other Events." + "On August 27" becomes "Other Events.On August 27".
        Block-level tags get an explicit space first.
      - `ix:` tags wrap real numbers mid-sentence; lxml keeps their text and
        drops the tag, which is the behaviour we want.
    """
    # Strip the SGML <XBRL>/<XML> wrapper if present -- it is not always there,
    # so the parse must not depend on its depth.
    payload = re.sub(r"(?i)</?(XBRL|XML)>", " ", payload)
    if not payload.strip():
        return ""

    try:
        tree = lxml.html.fromstring(payload)
    except (lxml.etree.ParserError, ValueError) as exc:
        raise ParseError(f"could not parse document HTML: {exc}") from exc

    for node in tree.xpath("//script | //style"):
        node.getparent().remove(node)

    # Hidden inline-XBRL metadata. Matching on the style attribute covers the
    # common spellings ("display:none", "display: none").
    for node in tree.xpath(
        "//*[contains(translate(@style,' ',''),'display:none')]"
    ):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)

    # Force a separator around block elements so text does not run together.
    for node in tree.iter():
        if isinstance(node.tag, str) and node.tag.lower() in _BLOCK_TAGS:
            node.tail = (node.tail or "") + " "
            if node.text:
                node.text = " " + node.text

    return normalise_whitespace(tree.text_content())


def normalise_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- trimming --------------------------------------------------------------


def trim_to_items(text: str) -> str:
    """Drop the cover page, keeping from the first numbered item onward.

    If no item heading is found the text is returned whole: a filing that does
    not follow the convention is better sent in full than truncated to nothing.
    """
    match = ITEM_RE.search(text)
    return text[match.start():].strip() if match else text


def find_items(text: str) -> list[str]:
    """Item codes appearing in the body, de-duplicated, in order."""
    seen, out = set(), []
    for match in ITEM_RE.finditer(text):
        code = match.group(1)
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


# --- entry point -----------------------------------------------------------


def exhibit_documents(raw: str, prefix: str = "EX-99") -> list[str]:
    """Attachments whose TYPE starts with `prefix`, in filing order."""
    out = []
    for doc in split_documents(raw):
        match = TYPE_RE.search(doc)
        if match and match.group(1).strip().upper().startswith(prefix.upper()):
            out.append(doc)
    return out


def parse_filing(
    raw: bytes | str,
    *,
    accession: str = "",
    form: str = "8-K",
    trim: bool = True,
    include_exhibits: bool = False,
) -> Filing:
    """Reduce one raw submission to a Filing.

    `trim` drops the cover page.

    `include_exhibits` pulls in EX-99 attachments. This matters more than it
    looks: 55% of 8-Ks reference an exhibit, and 18% are a bare pointer -- a
    couple of hundred words saying an earnings release "is furnished as Exhibit
    99 and is incorporated herein by reference." For those, the primary document
    contains no facts to extract, so an `amounts` field can only come back empty
    and a summary can only restate the item title.

    It is off by default because it is a real trade-off, not a free improvement:
    an earnings release is long and mostly tables, which is exactly the input
    that inflates GPU time and produces worse summaries. Decide it against
    measured extraction quality on day 3, not now.
    """
    if isinstance(raw, bytes):
        raw_bytes = len(raw)
        raw = raw.decode("utf-8", errors="replace")
    else:
        raw_bytes = len(raw.encode("utf-8", errors="replace"))

    head = header(raw)
    doc = primary_document(raw, form)

    payload_match = TEXT_RE.search(doc)
    if not payload_match:
        raise ParseError("primary document has no <TEXT> payload")

    text = html_to_text(payload_match.group(1))
    if trim:
        text = trim_to_items(text)

    exhibits: list[str] = []
    if include_exhibits:
        for doc in exhibit_documents(raw):
            payload = TEXT_RE.search(doc)
            if not payload:
                continue
            try:
                body = html_to_text(payload.group(1))
            except ParseError:
                continue
            if body:
                exhibits.append(body)

    filename = FILENAME_RE.search(doc)
    sic = HEADER_SIC_RE.search(head)
    period = PERIOD_RE.search(head)

    return Filing(
        accession=accession,
        form=form,
        text=text,
        items=find_items(text),
        # The SEC's own classification of the filing, straight from the header.
        # It costs nothing here and is a free label to score extraction against.
        item_descriptions=[m.strip() for m in ITEM_INFORMATION_RE.findall(head)],
        sic=sic.group(1) if sic else None,
        period=period.group(1) if period else None,
        primary_filename=filename.group(1) if filename else None,
        attachments=len(split_documents(raw)),
        raw_bytes=raw_bytes,
        exhibits=exhibits,
    )
