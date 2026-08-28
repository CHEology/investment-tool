from decimal import Decimal

import pytest

from investment_tool.numeric import DecimalPolicyError, dec, dec_from_db, dec_text


def test_source_string_round_trip_is_exact():
    assert dec_text(dec("97.69")) == "97.69"
    assert dec_from_db("97.69") == Decimal("97.69")


def test_none_passes_through_never_zero():
    assert dec(None) is None
    assert dec_text(None) is None
    assert dec_from_db(None) is None


def test_float_rejected():
    with pytest.raises(DecimalPolicyError):
        dec(97.69)


def test_unparseable_rejected():
    with pytest.raises(DecimalPolicyError):
        dec("N/A")


def test_scientific_and_int_inputs():
    assert dec_text(dec("1E+2")) == "100"
    assert dec_text(dec(42)) == "42"
