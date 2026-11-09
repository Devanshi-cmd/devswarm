from slug import slugify


def test_no_limit():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("Hello, World!", max_length=None) == "hello-world"


def test_cuts_at_word_boundary():
    assert slugify("Hello brave new world", max_length=12) == "hello-brave"


def test_exact_fit():
    assert slugify("Hello brave", max_length=11) == "hello-brave"


def test_no_trailing_hyphen():
    assert slugify("Hello brave new", max_length=6) == "hello"


def test_long_first_word_is_cut():
    assert slugify("Supercalifragilistic word", max_length=5) == "super"
