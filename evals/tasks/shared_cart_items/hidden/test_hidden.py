from cart import Cart


def test_carts_are_independent():
    first = Cart()
    second = Cart()
    first.add("apple")
    assert second.count() == 0
    assert first.items == ["apple"]


def test_input_list_is_not_changed():
    original = ["a"]
    cart = Cart(items=original)
    cart.add("b")
    assert original == ["a"]
    assert cart.items == ["a", "b"]
