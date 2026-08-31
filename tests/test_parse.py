"""Parser tests. Fixtures are minimal but shaped like the real submissions --
the awkward cases here (hidden XBRL, run-together blocks, "Item." with a stray
period) were all found by running against a real day of filings.
"""

import pytest

from inhouse.parse import (
    Filing,
    ParseError,
    find_items,
    html_to_text,
    parse_filing,
    primary_document,
    trim_to_items,
)


def submission(*, primary_html: str, extras: str = "", header_extra: str = "") -> str:
    return f"""<SEC-DOCUMENT>0000000000-26-000001.txt : 20260827
<SEC-HEADER>0000000000-26-000001.hdr.sgml : 20260827
<ACCEPTANCE-DATETIME>20260827113006
ACCESSION NUMBER:\t\t0000000000-26-000001
CONFORMED SUBMISSION TYPE:\t8-K
CONFORMED PERIOD OF REPORT:\t20260827
ITEM INFORMATION:\t\tResults of Operations and Financial Condition
{header_extra}
\tCOMPANY DATA:
\t\tSTANDARD INDUSTRIAL CLASSIFICATION:\tMEAT PACKING PLANTS [2011]
</SEC-HEADER>
<DOCUMENT>
<TYPE>8-K
<SEQUENCE>1
<FILENAME>primary.htm
<TEXT>
{primary_html}
</TEXT>
</DOCUMENT>
{extras}
</SEC-DOCUMENT>
"""


COVER = (
    "<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>"
    "<p>FORM 8-K CURRENT REPORT</p>"
    "<p>Written communications pursuant to Rule 425 &#9744;</p>"
)


# --- selecting the primary document ---------------------------------------


def test_primary_document_is_chosen_by_type_not_by_markup():
    """XBRL viewer files are HTML too -- TYPE is the only reliable selector."""
    raw = submission(
        primary_html="<html><body><p>Item 2.02 the real filing.</p></body></html>",
        extras=(
            "<DOCUMENT>\n<TYPE>XML\n<FILENAME>R1.htm\n<TEXT>\n"
            "<html><body><p>viewer artifact</p></body></html>\n</TEXT>\n</DOCUMENT>\n"
            "<DOCUMENT>\n<TYPE>GRAPHIC\n<FILENAME>logo.jpg\n<TEXT>\nbase64\n</TEXT>\n</DOCUMENT>"
        ),
    )
    filing = parse_filing(raw)
    assert "the real filing" in filing.text
    assert "viewer artifact" not in filing.text
    assert filing.attachments == 3
    assert filing.primary_filename == "primary.htm"


def test_missing_primary_document_raises():
    raw = "<SEC-HEADER>x</SEC-HEADER><DOCUMENT>\n<TYPE>EX-99.1\n<TEXT>\nx\n</TEXT>\n</DOCUMENT>"
    with pytest.raises(ParseError, match="no <TYPE>8-K"):
        parse_filing(raw)


# --- HTML to text ----------------------------------------------------------


def test_hidden_inline_xbrl_metadata_is_dropped():
    """Inline XBRL hides a metadata block that a tag-strip would put in the prompt."""
    html = (
        '<html><body>'
        '<div style="display:none"><ix:nonNumeric name="dei:EntityCentralIndexKey">'
        "0000048465</ix:nonNumeric>false 2026-08-27</div>"
        "<p>Item 8.01 Other Events. The company did a thing.</p>"
        "</body></html>"
    )
    text = html_to_text(html)
    assert "0000048465" not in text
    assert "The company did a thing." in text


def test_display_none_with_a_space_is_also_dropped():
    html = '<html><body><div style="display: none">hidden</div><p>Item 8.01 shown</p></body></html>'
    assert "hidden" not in html_to_text(html)


def test_adjacent_blocks_do_not_run_together():
    """text_content() concatenates siblings; 'Events.On August' is the failure."""
    html = "<html><body><p>Item 8.01 Other Events.</p><p>On August 27, 2026, the board met.</p></body></html>"
    assert "Events.On" not in html_to_text(html)
    assert "Events. On August" in html_to_text(html)


def test_table_cells_are_separated():
    html = "<html><body><table><tr><td>99.1</td><td>Press Release</td></tr></table></body></html>"
    assert "99.1 Press Release" in html_to_text(html)


def test_inline_xbrl_numbers_survive_as_text():
    html = ("<html><body><p>Revenue was "
            '<ix:nonFraction name="us-gaap:Revenues">3.1 billion</ix:nonFraction>'
            " this quarter. Item 2.02</p></body></html>")
    assert "3.1 billion" in html_to_text(html)


def test_script_and_style_are_removed():
    html = "<html><body><script>var x=1</script><style>p{color:red}</style><p>Item 8.01 body</p></body></html>"
    text = html_to_text(html)
    assert "var x" not in text and "color:red" not in text


def test_sgml_xbrl_wrapper_is_tolerated_present_or_absent():
    with_wrapper = "<XBRL>\n<html><body><p>Item 8.01 wrapped</p></body></html>\n</XBRL>"
    without = "<html><body><p>Item 8.01 bare</p></body></html>"
    assert "wrapped" in html_to_text(with_wrapper)
    assert "bare" in html_to_text(without)


# --- trimming --------------------------------------------------------------


def test_cover_page_is_trimmed_to_the_first_item():
    raw = submission(primary_html=f"<html><body>{COVER}<p>Item 2.02 Results. Real content.</p></body></html>")
    filing = parse_filing(raw)
    assert filing.text.startswith("Item 2.02")
    assert "Rule 425" not in filing.text


def test_item_with_a_stray_period_is_matched():
    """One filer in 181 writes 'Item. 8.01'. Missing it means shipping the cover page."""
    text = "COVER PAGE BOILERPLATE Item. 8.01 Other Events. The board declared a dividend."
    assert trim_to_items(text).startswith("Item. 8.01")
    assert find_items(text) == ["8.01"]


def test_text_without_any_item_is_returned_whole():
    """Better to send a full document than to truncate it to nothing."""
    text = "A filing that does not follow the item convention at all."
    assert trim_to_items(text) == text


def test_trim_can_be_disabled():
    raw = submission(primary_html=f"<html><body>{COVER}<p>Item 2.02 Results.</p></body></html>")
    assert "Rule 425" in parse_filing(raw, trim=False).text


def test_items_are_deduplicated_in_document_order():
    text = "Item 5.02 Departure. ... Item 9.01 Exhibits. ... see Item 5.02 above."
    assert find_items(text) == ["5.02", "9.01"]


# --- header facts ----------------------------------------------------------


def test_header_facts_are_extracted():
    filing = parse_filing(
        submission(primary_html="<html><body><p>Item 2.02 Results.</p></body></html>"),
        accession="0000000000-26-000001",
    )
    assert filing.sic == "2011"
    assert filing.period == "20260827"
    assert filing.item_descriptions == ["Results of Operations and Financial Condition"]
    assert filing.items == ["2.02"]


def test_item_descriptions_capture_every_header_line():
    raw = submission(
        primary_html="<html><body><p>Item 2.02 Results.</p></body></html>",
        header_extra="ITEM INFORMATION:\t\tFinancial Statements and Exhibits",
    )
    assert parse_filing(raw).item_descriptions == [
        "Results of Operations and Financial Condition",
        "Financial Statements and Exhibits",
    ]


# --- exhibits --------------------------------------------------------------


EXHIBIT = (
    "<DOCUMENT>\n<TYPE>EX-99.1\n<FILENAME>ex99.htm\n<TEXT>\n"
    "<html><body><p>Q3 revenue was $3.1 billion, up 4%.</p></body></html>\n"
    "</TEXT>\n</DOCUMENT>"
)


def test_exhibits_are_excluded_by_default():
    raw = submission(
        primary_html="<html><body><p>Item 2.02 furnished as Exhibit 99.1.</p></body></html>",
        extras=EXHIBIT,
    )
    filing = parse_filing(raw)
    assert filing.exhibits == []
    assert "3.1 billion" not in filing.text


def test_exhibits_are_available_on_request():
    """18% of 8-Ks are a bare pointer; the facts live in the exhibit."""
    raw = submission(
        primary_html="<html><body><p>Item 2.02 furnished as Exhibit 99.1.</p></body></html>",
        extras=EXHIBIT,
    )
    filing = parse_filing(raw, include_exhibits=True)
    assert len(filing.exhibits) == 1
    assert "$3.1 billion" in filing.exhibits[0]
    # The primary text is unchanged -- callers decide how to combine them.
    assert "3.1 billion" not in filing.text


# --- byte handling ---------------------------------------------------------


def test_accepts_bytes_and_reports_raw_size():
    raw = submission(primary_html="<html><body><p>Item 2.02 Results.</p></body></html>")
    filing = parse_filing(raw.encode("utf-8"))
    assert filing.raw_bytes == len(raw.encode("utf-8"))
    assert isinstance(filing, Filing)


def test_undecodable_bytes_do_not_crash():
    raw = submission(primary_html="<html><body><p>Item 2.02 caf\udce9</p></body></html>")
    filing = parse_filing(raw.encode("utf-8", errors="replace"))
    assert filing.text.startswith("Item 2.02")
