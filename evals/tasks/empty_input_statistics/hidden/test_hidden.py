from stats import mean, median


def test_empty_mean():
    assert mean([]) == 0.0


def test_empty_median():
    assert median([]) == 0.0


def test_normal_values_unchanged():
    assert mean([2, 4]) == 3
    assert median([5, 1, 3]) == 3
    assert median([1, 2]) == 1.5
