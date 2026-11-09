from validate import is_valid_email


def test_simple_address():
    assert is_valid_email("user@example.com")


def test_missing_at():
    assert not is_valid_email("userexample.com")
