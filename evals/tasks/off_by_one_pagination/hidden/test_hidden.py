from pager import get_page, page_count


def test_partial_page_counts():
    assert page_count(11, 5) == 3
    assert page_count(1, 5) == 1


def test_no_items():
    assert page_count(0, 5) == 0


def test_even_still_works():
    assert page_count(10, 5) == 2


def test_last_partial_page_is_reachable():
    items = list(range(11))
    assert get_page(items, page_count(11, 5), 5) == [10]
