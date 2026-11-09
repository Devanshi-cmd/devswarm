import re

_EMAIL = re.compile(r"^[a-z0-9]+@[a-z0-9]+\.[a-z]{2,}$")


def is_valid_email(address: str) -> bool:
    return _EMAIL.match(address) is not None
