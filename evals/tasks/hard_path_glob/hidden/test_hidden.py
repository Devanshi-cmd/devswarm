import pytest

from pathmatch import match


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("*.py", "a.py"),
        ("src/*.py", "src/a.py"),
        ("a?c", "abc"),
        ("**/a.py", "a.py"),
        ("**/a.py", "x/y/a.py"),
        ("a/**/b", "a/b"),
        ("a/**/b", "a/x/y/b"),
        ("src/**", "src/a"),
        ("src/**", "src/a/b"),
        ("[abc].py", "b.py"),
        ("[!abc].py", "z.py"),
        ("a+b(c).txt", "a+b(c).txt"),
        ("**/*.py", "x/y/z.py"),
    ],
)
def test_matches(pattern, path):
    assert match(pattern, path)


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("*.py", "a/b.py"),
        ("src/*.py", "src/x/a.py"),
        ("a?c", "a/c"),
        ("a?c", "ac"),
        ("**/a.py", "xa.py"),
        ("a/**/b", "ab"),
        ("a/**/b", "a/xb"),
        ("src/**", "src"),
        ("src/**", "srcx/a"),
        ("[abc].py", "d.py"),
        ("[!abc].py", "a.py"),
        ("x[!a]y", "x/y"),
        ("a.py", "axpy"),
        ("a+", "aa"),
        ("*.py", "a.pyc"),
    ],
)
def test_non_matches(pattern, path):
    assert not match(pattern, path)
