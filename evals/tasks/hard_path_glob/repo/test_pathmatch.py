from pathmatch import match


def test_star():
    assert match("*.py", "a.py")


def test_question():
    assert match("a?c", "abc")
