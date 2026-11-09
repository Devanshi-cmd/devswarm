from intervals import merge_intervals


def test_overlap():
    assert merge_intervals([(1, 3), (2, 4)]) == [(1, 4)]


def test_separate():
    assert merge_intervals([(1, 2), (3, 4)]) == [(1, 2), (3, 4)]
