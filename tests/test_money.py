from decimal import Decimal

import pytest

from bank.money import EXPONENTS, Money, from_minor, to_minor


def test_eur_has_two_decimals():
    assert to_minor("12.34", "EUR") == 1234


def test_jpy_has_no_minor_unit():
    assert to_minor("5000", "JPY") == 5000


def test_bhd_has_three_decimals():
    assert to_minor("1.234", "BHD") == 1234


def test_half_even_rounding():
    assert to_minor("0.125", "EUR") == 12
    assert to_minor("0.135", "EUR") == 14


def test_float_is_rejected():
    with pytest.raises(TypeError):
        to_minor(0.1, "EUR")


@pytest.mark.parametrize("currency", sorted(EXPONENTS))
def test_round_trip_all_currencies(currency):
    minor = 123456
    assert to_minor(from_minor(minor, currency), currency) == minor


def test_money_add_and_mismatch():
    assert Money.of("1.10", "EUR") + Money.of("2.20", "EUR") == Money(330, "EUR")
    with pytest.raises(ValueError):
        Money.of("1", "EUR") + Money.of("1", "JPY")


def test_str_shows_major_units():
    assert str(Money(500000, "JPY")) == "500000 JPY"
    assert from_minor(5, "EUR") == Decimal("0.05")


def test_money_of_rejects_extra_decimals():
    with pytest.raises(ValueError):
        Money.of("1.005", "EUR")
    with pytest.raises(ValueError):
        Money.of("1.5", "JPY")
    assert Money.of("1.50", "EUR") == Money(150, "EUR")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "abc"])
def test_money_of_rejects_non_numbers(value):
    with pytest.raises(ValueError):
        Money.of(value, "EUR")
