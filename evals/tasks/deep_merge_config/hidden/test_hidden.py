import copy

from settings import merge_config


def test_nested_keys_survive():
    result = merge_config({"db": {"host": "a", "port": 1}}, {"db": {"port": 2}})
    assert result == {"db": {"host": "a", "port": 2}}


def test_three_levels():
    result = merge_config({"a": {"b": {"c": 1, "d": 2}}}, {"a": {"b": {"d": 3}}})
    assert result == {"a": {"b": {"c": 1, "d": 3}}}


def test_scalar_replaces_dict():
    assert merge_config({"a": {"b": 1}}, {"a": 5}) == {"a": 5}


def test_dict_replaces_scalar():
    assert merge_config({"a": 5}, {"a": {"b": 1}}) == {"a": {"b": 1}}


def test_inputs_are_not_modified():
    base = {"db": {"host": "a"}}
    override = {"db": {"port": 2}}
    base_copy, override_copy = copy.deepcopy(base), copy.deepcopy(override)
    result = merge_config(base, override)
    result["db"]["host"] = "changed"
    assert base == base_copy
    assert override == override_copy
