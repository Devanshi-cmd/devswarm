from scheduler import schedule


def test_chain():
    assert schedule({"a": [], "b": ["a"], "c": ["b"]}) == ["a", "b", "c"]
