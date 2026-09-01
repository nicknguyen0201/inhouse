"""Form 4 parser tests.

The awkward cases here are all real: nested <value> wrappers, holdings mixed in
with transactions, derivative tables, footnote-qualified prices, and filers
whose numbers are simply wrong.
"""

import pytest

from inhouse.form4 import parse_form4
from inhouse.parse import ParseError


def submission(body: str) -> str:
    return f"""<SEC-DOCUMENT>0000000000-26-000001.txt : 20260827
<SEC-HEADER>x
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<SEQUENCE>1
<FILENAME>ownership.xml
<TEXT>
<XML>
<?xml version="1.0"?>
{body}
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""


OWNER = """
    <issuer>
        <issuerCik>0001094517</issuerCik>
        <issuerName>TOYOTA MOTOR CORP/</issuerName>
        <issuerTradingSymbol>TM</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0002119451</rptOwnerCik>
            <rptOwnerName>Ueda Tatsuro</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector>
            <isOfficer>1</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <officerTitle>Operating Officer</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
"""

TXN = """
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-08-25</value></transactionDate>
            <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>86</value></transactionShares>
                <transactionPricePerShare>
                    <value>19.53</value><footnoteId id="F1"/>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>32747</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
"""

HOLDING = """
        <nonDerivativeHolding>
            <securityTitle><value>Common Stock</value></securityTitle>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>21000</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeHolding>
"""

FOOTNOTES = """
    <footnotes>
        <footnote id="F1">The purchase was made in Japanese Yen and converted
        at Japanese Yen 1.00 = U.S. dollar .0063.</footnote>
    </footnotes>
"""


def toyota(extra: str = "") -> str:
    return submission(
        f"<ownershipDocument><periodOfReport>2026-08-25</periodOfReport>"
        f"{OWNER}<nonDerivativeTable>{TXN}{HOLDING}</nonDerivativeTable>"
        f"{FOOTNOTES}{extra}</ownershipDocument>"
    )


# --- the basic shape -------------------------------------------------------


def test_parses_issuer_insider_and_transaction():
    f = parse_form4(toyota(), accession="0000947871-26-000844")
    assert f.issuer_cik == "0001094517"
    assert f.issuer_name == "TOYOTA MOTOR CORP/"
    assert len(f.transactions) == 1

    t = f.transactions[0]
    assert t.insider_name == "Ueda Tatsuro"
    assert t.officer_title == "Operating Officer"
    assert t.role == "Operating Officer"
    assert t.code == "A"
    assert t.shares == 86.0
    assert t.price_per_share == 19.53
    assert t.shares_owned_after == 32747.0
    assert t.direct_ownership is False       # "I" -- held indirectly


def test_values_are_read_from_the_nested_value_element():
    """<transactionShares><value>86</value> -- not the element's own text."""
    t = parse_form4(toyota()).transactions[0]
    assert t.shares == 86.0 and isinstance(t.shares, float)


def test_holdings_are_counted_but_never_emitted_as_transactions():
    """A holding has no code, date or price; emitting one looks like a bad parse."""
    f = parse_form4(toyota())
    assert f.holdings == 1
    assert all(t.code is not None for t in f.transactions)


def test_value_usd_is_shares_times_price():
    assert parse_form4(toyota()).transactions[0].value_usd == round(86 * 19.53, 2)


def test_footnotes_are_attached_to_the_transaction_that_references_them():
    """The price is a yen conversion -- meaningless without its footnote."""
    t = parse_form4(toyota()).transactions[0]
    assert len(t.footnotes) == 1
    assert "Japanese Yen" in t.footnotes[0]


def test_period_and_transaction_dates_are_parsed():
    from datetime import date

    f = parse_form4(toyota())
    assert f.period == date(2026, 8, 25)
    assert f.transactions[0].transaction_date == date(2026, 8, 25)


# --- multiple transactions and derivatives --------------------------------


DERIV = """
    <derivativeTable>
        <derivativeTransaction>
            <securityTitle><value>Stock Option</value></securityTitle>
            <transactionDate><value>2026-08-26</value></transactionDate>
            <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>5000</value></transactionShares>
                <transactionPricePerShare><value>0</value></transactionPricePerShare>
            </transactionAmounts>
        </derivativeTransaction>
    </derivativeTable>
"""


def test_derivative_transactions_are_included_and_flagged():
    f = parse_form4(toyota(extra=DERIV))
    assert len(f.transactions) == 2
    deriv = [t for t in f.transactions if t.derivative]
    assert len(deriv) == 1
    assert deriv[0].code == "M" and deriv[0].security_title == "Stock Option"


def test_multiple_transactions_in_one_filing_all_appear():
    body = (f"<ownershipDocument>{OWNER}<nonDerivativeTable>{TXN}{TXN}{TXN}"
            f"</nonDerivativeTable></ownershipDocument>")
    assert len(parse_form4(submission(body)).transactions) == 3


# --- role and signal -------------------------------------------------------


def test_role_falls_back_to_relationship_flags_without_a_title():
    body = f"""<ownershipDocument>
    <issuer><issuerCik>1</issuerCik><issuerName>ACME</issuerName></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>2</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector><isOfficer>0</isOfficer><isTenPercentOwner>1</isTenPercentOwner>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>{TXN}</nonDerivativeTable></ownershipDocument>"""
    t = parse_form4(submission(body)).transactions[0]
    assert t.role == "Director, 10% Owner"


def test_open_market_flag_separates_trades_from_compensation():
    """P and S are real trades; A grants and F withholdings are mechanics."""
    def with_code(code):
        return submission(
            f"<ownershipDocument>{OWNER}<nonDerivativeTable>"
            f"{TXN.replace('<transactionCode>A<', f'<transactionCode>{code}<')}"
            f"</nonDerivativeTable></ownershipDocument>"
        )

    assert parse_form4(with_code("S")).transactions[0].is_open_market
    assert parse_form4(with_code("P")).transactions[0].is_open_market
    assert not parse_form4(with_code("A")).transactions[0].is_open_market
    assert not parse_form4(with_code("F")).transactions[0].is_open_market


# --- messy real-world input ------------------------------------------------


def test_numbers_with_commas_and_currency_symbols_are_read():
    body = (f"<ownershipDocument>{OWNER}<nonDerivativeTable>"
            + TXN.replace("<value>86</value>", "<value>1,250</value>")
                 .replace("<value>19.53</value>", "<value>$12.50</value>")
            + "</nonDerivativeTable></ownershipDocument>")
    t = parse_form4(submission(body)).transactions[0]
    assert t.shares == 1250.0 and t.price_per_share == 12.50


def test_absent_price_yields_none_not_zero():
    """A grant often has no price. None and 0.0 mean different things."""
    body = (f"<ownershipDocument>{OWNER}<nonDerivativeTable>"
            + TXN.replace(
                "<transactionPricePerShare>\n                    <value>19.53</value><footnoteId id=\"F1\"/>\n                </transactionPricePerShare>",
                "<transactionPricePerShare></transactionPricePerShare>")
            + "</nonDerivativeTable></ownershipDocument>")
    t = parse_form4(submission(body)).transactions[0]
    assert t.price_per_share is None
    assert t.value_usd is None


def test_implausible_filer_values_are_reported_verbatim():
    """One filer reported $17,372.52/share for a hotel REIT. Not our job to fix.

    The parser reports what the filing says; flagging outliers belongs
    downstream, where the threshold can be seen and tuned.
    """
    body = (f"<ownershipDocument>{OWNER}<nonDerivativeTable>"
            + TXN.replace("<value>19.53</value>", "<value>17372.52</value>")
            + "</nonDerivativeTable></ownershipDocument>")
    assert parse_form4(submission(body)).transactions[0].price_per_share == 17372.52


def test_missing_ownership_document_raises():
    with pytest.raises(ParseError, match="ownershipDocument"):
        parse_form4(submission("<somethingElse/>"))


def test_wrong_form_type_raises():
    raw = "<DOCUMENT>\n<TYPE>8-K\n<TEXT>\n<html></html>\n</TEXT>\n</DOCUMENT>"
    with pytest.raises(ParseError, match="no <TYPE>4"):
        parse_form4(raw)


def test_accepts_bytes():
    assert parse_form4(toyota().encode("utf-8")).issuer_name == "TOYOTA MOTOR CORP/"
