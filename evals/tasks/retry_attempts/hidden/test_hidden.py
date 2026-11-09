import pytest

from retrying import retry


class Flaky:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError(f"failure {self.calls}")
        return "ok"


def test_uses_every_attempt():
    fn = Flaky(2)
    assert retry(fn, attempts=3) == "ok"
    assert fn.calls == 3


def test_raises_last_error():
    fn = Flaky(10)
    with pytest.raises(ValueError, match="failure 3"):
        retry(fn, attempts=3)
    assert fn.calls == 3


def test_stops_after_success():
    fn = Flaky(0)
    retry(fn, attempts=3)
    assert fn.calls == 1
