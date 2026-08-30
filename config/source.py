"""The document source. EDGAR by default -- swap this to point at your own corpus.

Everything downstream of `discover()` and `fetch()` is domain-agnostic: storage
layout, the manifest, and (from day 2 on) extraction and the dashboard. To run
this pipeline against a different corpus, reimplement these two functions and
leave the rest alone.

    discover(day) -> list[SourceDocument]   what exists for a date
    fetch(doc)    -> bytes                  the raw document, unmodified
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from inhouse.config import Config
from inhouse.edgar import EdgarClient
from inhouse.index import parse_index

# The forms this pipeline handles. 8-K is narrative and needs a model; Form 4 is
# already-structured XML and does not. See the README for why they differ.
FORM_TYPES = ("8-K", "4")


@dataclass(frozen=True)
class SourceDocument:
    doc_id: str       # unique and stable; becomes the storage filename
    group_id: str     # the entity this belongs to -- CIK here
    title: str
    kind: str         # form type here
    day: date
    locator: str      # whatever fetch() needs; an archive path here
    extension: str


def discover(day: date, config: Config, client: EdgarClient) -> list[SourceDocument]:
    entries = parse_index(client.daily_index(day), form_types=config.form_types)
    return [
        SourceDocument(
            doc_id=e.accession,
            group_id=e.cik,
            title=e.company,
            kind=e.form,
            day=e.filed_date,
            locator=e.path,
            extension=e.extension,
        )
        for e in entries
    ]


def fetch(doc: SourceDocument, client: EdgarClient) -> bytes:
    """Return the document exactly as served. Do not normalise or parse here."""
    return client.document(doc.locator)
