from cart import Cart


def test_add_and_count():
    cart = Cart(items=["a"])
    cart.add("b")
    assert cart.count() == 2
