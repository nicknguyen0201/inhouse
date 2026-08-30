from datetime import date

import pytest

from inhouse.index import IndexParseError, parse_index

SAMPLE = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    August 26, 2026

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------
4           ACME HOLDINGS CORP /DE/                                       1234567     2026-08-26  edgar/data/1234567/0001234567-26-000124.xml
8-K         APPLE INC                                                     320193      2026-08-26  edgar/data/320193/0000320193-26-000123.txt
8-K/A       AMENDED FILER INC                                             999999      2026-08-26  edgar/data/999999/0000999999-26-000001.txt
10-K        SOME OTHER CORP                                               111111      2026-08-26  edgar/data/111111/0000111111-26-000002.txt
"""


def test_parses_all_entries_when_unfiltered():
    entries = parse_index(SAMPLE)
    assert len(entries) == 4


def test_filters_to_requested_form_types():
    entries = parse_index(SAMPLE, form_types=("8-K", "4"))
    assert [e.form for e in entries] == ["4", "8-K"]


def test_amendments_are_not_matched_by_base_form():
    """8-K/A restates an earlier filing and is out of scope for day 1."""
    forms = {e.form for e in parse_index(SAMPLE, form_types=("8-K", "4"))}
    assert "8-K/A" not in forms


def test_company_names_with_spaces_survive_fixed_width_columns():
    entry = next(e for e in parse_index(SAMPLE) if e.cik.endswith("1234567"))
    assert entry.company == "ACME HOLDINGS CORP /DE/"


def test_cik_is_zero_padded_to_ten_digits():
    entry = next(e for e in parse_index(SAMPLE, form_types=("8-K",)))
    assert entry.cik == "0000320193"


def test_accession_and_extension_come_from_the_filename():
    by_form = {e.form: e for e in parse_index(SAMPLE)}
    assert by_form["8-K"].accession == "0000320193-26-000123"
    assert by_form["8-K"].extension == "txt"
    # Form 4s are served as XML and must keep that extension.
    assert by_form["4"].extension == "xml"


def test_filed_date_is_parsed():
    entry = parse_index(SAMPLE, form_types=("8-K",))[0]
    assert entry.filed_date == date(2026, 8, 26)


def test_missing_header_raises():
    with pytest.raises(IndexParseError):
        parse_index("nothing resembling an index here\n")


def test_blank_and_malformed_lines_are_skipped():
    text = SAMPLE + "\n   \nGARBAGE LINE WITHOUT COLUMNS\n"
    assert len(parse_index(text)) == 4


# The real daily index, as EDGAR actually serves it: the header wraps onto a
# second line and dates are YYYYMMDD. Both differ from the quarterly indexes.
REAL_SAMPLE = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Aug 27, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/
 
 
Form Type   Company Name                                                  CIK
      Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------
4                Cloud Title Partners LLC                                      2030863     20260827    edgar/data/2030863/0001096906-26-001317.txt
8-K              Shasta Power Fund II, LLC                                     2071540     20260827    edgar/data/2071540/0002071540-26-000005.xml
1-A/A            Tejascore Techsystems Inc                                     2044000     20260827    edgar/data/2044000/0002044000-26-000003.txt
"""


def test_parses_wrapped_header_and_compact_dates():
    entries = parse_index(REAL_SAMPLE, form_types=("8-K", "4"))
    assert [e.form for e in entries] == ["4", "8-K"]
    assert entries[0].filed_date == date(2026, 8, 27)
    assert entries[0].company == "Cloud Title Partners LLC"
    assert entries[0].cik == "0002030863"
    assert entries[1].accession == "0002071540-26-000005"
