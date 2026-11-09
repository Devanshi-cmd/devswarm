from checkout import order_total
from pricing import apply_discount


def test_no_coupon():
    assert order_total([10, 20]) == 33.0


def test_apply_discount():
    assert apply_discount(100, 25) == 75
