"""Rendering helpers. No database: these are pure functions over row tuples."""

from inhouse.render import filing_url, form4_url


def test_filing_url_points_at_the_submission_index():
    """The index page, not the inline-XBRL viewer: 65% of filings state their
    figures in an exhibit rather than the 8-K body, and only the index lists
    the exhibits."""
    assert filing_url(
        "https://www.sec.gov/Archives/edgar/data/1308547/0001193125-26-369707.txt"
    ) == (
        "https://www.sec.gov/Archives/edgar/data/1308547/"
        "0001193125-26-369707-index.htm"
    )


def test_unrecognised_urls_are_passed_through_rather_than_dropped():
    """Better to link the raw document than to render no link at all."""
    assert filing_url("https://example.com/x.txt") == "https://example.com/x.txt"
    assert filing_url(None) is None
    assert filing_url("") is None


def test_form4_url_needs_no_stored_filename():
    """Every Form 4 has one attachment, ownership.xml, because EDGAR generates
    it from a web form -- so unlike an 8-K nothing has to be persisted."""
    assert form4_url("0001094517", "0000947871-26-000844") == (
        "https://www.sec.gov/Archives/edgar/data/1094517/"
        "0000947871-26-000844-index.htm"
    )


def test_form4_url_strips_the_ciks_leading_zeros():
    """CIKs are stored zero-padded to ten digits; EDGAR paths are not padded."""
    assert "/data/1094517/" in form4_url("0001094517", "0000947871-26-000844")


def test_form4_url_is_none_without_both_parts():
    assert form4_url(None, "0000947871-26-000844") is None
    assert form4_url("0001094517", None) is None
