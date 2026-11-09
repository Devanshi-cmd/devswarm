from intervals import merge_intervals


def test_touching():
    assert merge_intervals([(1, 2), (2, 3)]) == [(1, 3)]


def test_unsorted():
    assert merge_intervals([(5, 6), (1, 2)]) == [(1, 2), (5, 6)]


def test_nested():
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]


def test_empty():
    assert merge_intervals([]) == []


def test_combined():
    assert merge_intervals([(8, 9), (1, 5), (2, 3), (5, 7)]) == [(1, 7), (8, 9)]
