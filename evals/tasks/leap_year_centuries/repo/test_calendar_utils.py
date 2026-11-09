from calendar_utils import days_in_month, is_leap


def test_ordinary_leap_year():
    assert is_leap(2024)
    assert not is_leap(2023)


def test_february():
    assert days_in_month(2024, 2) == 29
    assert days_in_month(2023, 2) == 28
