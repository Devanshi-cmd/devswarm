from retrying import retry


def test_success_first_time():
    assert retry(lambda: 5) == 5
