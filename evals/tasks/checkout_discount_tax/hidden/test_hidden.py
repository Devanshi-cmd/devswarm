import pytest

from checkout import order_total
from pricing import apply_discount


def test_coupon_applied_once_and_tax_on_discounted():
    assert order_total([10, 20], 10) == pytest.approx(29.70)


def test_no_coupon():
    assert order_total([10, 20]) == pytest.approx(33.00)


def test_full_discount():
    assert order_total([10, 20], 100) == pytest.approx(0.0)


def test_rounding():
    assert order_total([9.99], 15) == pytest.approx(9.34)


def test_discount_helper_unchanged():
    assert apply_discount(100, 25) == 75
