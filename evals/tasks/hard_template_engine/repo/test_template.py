from template import render


def test_simple():
    assert render("Hi {{name}}!", {"name": "Ada"}) == "Hi Ada!"
