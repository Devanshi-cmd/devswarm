import pytest

from orders import OrderError, Orders
from stock import OutOfStock, Stock


def setup():
    stock = Stock({"a": 5, "b": 1})
    return stock, Orders(stock)


def test_double_cancel_releases_once():
    stock, orders = setup()
    order = orders.place({"a": 2})
    orders.cancel(order)
    orders.cancel(order)
    assert stock.available("a") == 5


def test_failed_order_leaves_stock_untouched():
    stock, orders = setup()
    with pytest.raises(OutOfStock):
        orders.place({"a": 2, "b": 3})
    assert stock.available("a") == 5
    assert stock.available("b") == 1


def test_failed_order_is_order_independent():
    stock, orders = setup()
    with pytest.raises(OutOfStock):
        orders.place({"b": 1, "a": 9})
    assert stock.available("a") == 5
    assert stock.available("b") == 1


def test_cancel_shipped_is_refused():
    stock, orders = setup()
    order = orders.place({"a": 2})
    orders.ship(order)
    with pytest.raises(OrderError):
        orders.cancel(order)
    assert stock.available("a") == 3


def test_ship_cancelled_is_refused_and_stays_cancelled():
    stock, orders = setup()
    order = orders.place({"a": 2})
    orders.cancel(order)
    with pytest.raises(OrderError):
        orders.ship(order)
    orders.cancel(order)
    assert stock.available("a") == 5


@pytest.mark.parametrize("action", ["ship", "cancel"])
def test_unknown_order(action):
    _, orders = setup()
    with pytest.raises(OrderError):
        getattr(orders, action)(999)


def test_normal_flow_still_works():
    stock, orders = setup()
    order = orders.place({"a": 1, "b": 1})
    orders.ship(order)
    assert stock.available("a") == 4
    assert stock.available("b") == 0
