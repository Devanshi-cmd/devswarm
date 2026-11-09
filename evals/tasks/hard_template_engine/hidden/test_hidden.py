import pytest

import template


def render(text, context):
    return template.render(text, context)


def test_simple_and_repeated():
    assert render("{{a}}-{{a}}", {"a": 1}) == "1-1"


def test_spaces():
    assert render("{{ a }}|{{  b}}|{{c  }}", {"a": 1, "b": 2, "c": 3}) == "1|2|3"


def test_dotted():
    assert render("{{ user.name.first }}", {"user": {"name": {"first": "Ada"}}}) == "Ada"


def test_escape():
    assert render(r"\{{ a }} {{ a }}", {"a": 1}) == "{{ a }} 1"


def test_missing_top_level():
    with pytest.raises(template.MissingVariable) as caught:
        render("{{ nope }}", {})
    assert isinstance(caught.value, KeyError)
    assert "nope" in str(caught.value)


def test_missing_dotted_keeps_full_name():
    with pytest.raises(template.MissingVariable) as caught:
        render("{{ user.age }}", {"user": {}})
    assert "user.age" in str(caught.value)


def test_path_through_non_dict():
    with pytest.raises(template.MissingVariable) as caught:
        render("{{ user.name.first }}", {"user": {"name": "Ada"}})
    assert "user.name.first" in str(caught.value)


def test_none_is_empty():
    assert render("[{{ a }}]", {"a": None}) == "[]"


def test_falsy_values_are_kept():
    assert render("{{ a }}{{ b }}", {"a": 0, "b": False}) == "0False"


def test_values_are_not_expanded():
    assert render("{{ a }}", {"a": "{{ b }}", "b": "x"}) == "{{ b }}"
