from csvline import parse_line


def test_simple():
    assert parse_line("a,b,c") == ["a", "b", "c"]


def test_quoted_comma():
    assert parse_line('a,"b,c",d') == ["a", "b,c", "d"]


def test_doubled_quote():
    assert parse_line('"say ""hi""",x') == ['say "hi"', "x"]


def test_empty_fields():
    assert parse_line("a,,b") == ["a", "", "b"]
    assert parse_line('"",x') == ["", "x"]
