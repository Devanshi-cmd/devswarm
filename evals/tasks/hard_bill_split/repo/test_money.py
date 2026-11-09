from money import split


def test_even():
    assert split(100, [1, 1]) == [50, 50]


def test_weighted():
    assert split(90, [1, 2]) == [30, 60]
