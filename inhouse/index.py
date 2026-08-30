"""Parser for EDGAR's daily form index.

The file looks like this:

    Form Type   Company Name                       CIK
          Date Filed  File Name
    ---------------------------------------------------------------------------
    8-K         APPLE INC                        320193      20260826    edgar/data/320193/0000320193-26-000123.txt

Two things about the real files that a reasonable guess gets wrong:

  - The header wraps onto a second line, so "Date Filed" and "File Name" are not
    on the same line as "Form Type". Column offsets therefore have to come from
    the dashed separator, not the header text.
  - Dates are YYYYMMDD here, even though the quarterly indexes use YYYY-MM-DD.
    Both are accepted below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

HEADER_RE = re.compile(r"^Form Type\s+Company Name\s+CIK", re.I)
SEPARATOR_RE = re.compile(r"^-{5,}\s*$")

# A data row, anchored on the fields that cannot contain spaces. The company
# name is whatever lies between the form type and the CIK.
ROW_RE = re.compile(
    r"^(?P<form>\S+)\s{2,}"
    r"(?P<company>.+?)\s{2,}"
    r"(?P<cik>\d{1,10})\s+"
    r"(?P<filed>\d{4}-?\d{2}-?\d{2})\s+"
    r"(?P<path>\S+)\s*$"
)


@dataclass(frozen=True)
class IndexEntry:
    form: str
    company: str
    cik: str          # zero-padded to 10 digits
    filed_date: date
    path: str         # EDGAR archive path, e.g. edgar/data/320193/0000320193-26-000123.txt

    @property
    def accession(self) -> str:
        """The accession number, taken from the filename: 0000320193-26-000123."""
        stem = self.path.rsplit("/", 1)[-1]
        return stem.rsplit(".", 1)[0]

    @property
    def extension(self) -> str:
        """Extension of the served document, so raw files keep their real type."""
        stem = self.path.rsplit("/", 1)[-1]
        return stem.rsplit(".", 1)[-1].lower() if "." in stem else "txt"


class IndexParseError(Exception):
    """The index file was not in the expected format."""


def _parse_filed(value: str) -> date | None:
    """Daily indexes use YYYYMMDD; quarterly ones use YYYY-MM-DD. Accept both."""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_index(text: str, *, form_types: tuple[str, ...] = ()) -> list[IndexEntry]:
    """Parse a daily index, optionally filtering to specific form types.

    Rows are matched by regex anchored on the three fields that cannot contain
    spaces -- CIK, date, and path. Whatever sits between the form type and the
    CIK is the company name, spaces and all.

    Fixed-width column offsets are deliberately not used: the daily files pad
    columns differently from the quarterly ones, and the header wraps, so
    offsets derived from either the header or the separator bar disagree with
    the data rows.

    Form matching is exact: "4" does not match "4/A" (an amendment) and "8-K"
    does not match "8-K/A". Amendments restate an earlier filing and belong in a
    later version of this pipeline, not day 1.
    """
    lines = text.splitlines()

    header_idx = None
    for i, line in enumerate(lines[:60]):
        if HEADER_RE.match(line):
            header_idx = i
            break
    if header_idx is None:
        raise IndexParseError("no 'Form Type ... CIK' header found in index")

    # The header wraps in the daily files, so the separator is one or two lines
    # down. Scan forward a little rather than assuming it is adjacent.
    sep_idx = None
    for i in range(header_idx + 1, min(header_idx + 5, len(lines))):
        if SEPARATOR_RE.match(lines[i]):
            sep_idx = i
            break
    if sep_idx is None:
        raise IndexParseError("expected a dashed separator line beneath the header")

    wanted = {f.upper() for f in form_types}
    entries: list[IndexEntry] = []

    for line in lines[sep_idx + 1:]:
        if not line.strip():
            continue
        match = ROW_RE.match(line)
        if not match:
            continue

        form, company, cik, filed, path = (
            match.group("form"), match.group("company").strip(),
            match.group("cik"), match.group("filed"), match.group("path").strip(),
        )
        if wanted and form.upper() not in wanted:
            continue

        filed_date = _parse_filed(filed)
        if filed_date is None:
            continue

        entries.append(
            IndexEntry(
                form=form,
                company=company,
                cik=cik.zfill(10),
                filed_date=filed_date,
                path=path,
            )
        )

    return entries
