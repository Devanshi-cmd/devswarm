from settings import merge_config


def test_flat_merge():
    assert merge_config({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_override_wins():
    assert merge_config({"a": 1}, {"a": 2}) == {"a": 2}
