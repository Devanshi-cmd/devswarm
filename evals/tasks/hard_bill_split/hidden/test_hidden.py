import pytest

from money import split


def test_leftover_goes_to_earliest_on_ties():
    assert split(100, [1, 1, 1]) == [34, 33, 33]


def test_largest_remainder_gets_the_cent():
    assert split(10, [1, 1, 3]) == [2, 2, 6]
    assert split(7, [2, 3, 5]) == [1, 2, 4]


def test_zero_weight_gets_nothing():
    assert split(10, [0, 1, 2]) == [0, 3, 7]


def test_negative_amount():
    assert split(-100, [1, 1, 1]) == [-34, -33, -33]


def test_zero_amount():
    assert split(0, [1, 2]) == [0, 0]


@pytest.mark.parametrize("weights", [[0, 0], []])
def test_bad_weights(weights):
    with pytest.raises(ValueError):
        split(5, weights)


@pytest.mark.parametrize("amount", [1, 2, 7, 99, 101, 1000, 12345, -7, -12345])
@pytest.mark.parametrize("weights", [[1, 1, 1], [3, 7], [1, 2, 3, 4], [5, 0, 5], [33, 33, 34]])
def test_shares_always_sum_exactly(amount, weights):
    assert sum(split(amount, weights)) == amount
