from pager import get_page, page_count


def test_even_division():
    assert page_count(10, 5) == 2


def test_first_page():
    assert get_page([1, 2, 3, 4], 1, 2) == [1, 2]
