from orders import Orders
from stock import Stock


def test_place_reserves():
    stock = Stock({"a": 5})
    Orders(stock).place({"a": 2})
    assert stock.available("a") == 3


def test_cancel_releases():
    stock = Stock({"a": 5})
    orders = Orders(stock)
    orders.cancel(orders.place({"a": 2}))
    assert stock.available("a") == 5
