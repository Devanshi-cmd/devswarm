import pytest

from validate import is_valid_email


@pytest.mark.parametrize(
    "address",
    ["first.last@example.com", "user+tag@example.com", "USER@EXAMPLE.COM", "a@mail.example.co.uk"],
)
def test_accepts(address):
    assert is_valid_email(address)


@pytest.mark.parametrize(
    "address",
    ["plain", "a@b", "a b@example.com", "@example.com", "a@.com", "a@example.c"],
)
def test_rejects(address):
    assert not is_valid_email(address)
