import pytest
from calc import CalculatorError, calculate, main


@pytest.mark.parametrize(
    ("left", "operator", "right", "expected"),
    [(2, "+", 3, 5), (5, "-", 3, 2), (2, "*", 3, 6), (8, "/", 2, 4)],
)
def test_operations(left, operator, right, expected):
    assert calculate(left, operator, right) == expected


def test_unknown_operator():
    with pytest.raises(CalculatorError):
        calculate(1, "%", 2)


def test_main_prints_result(capsys):
    assert main(["8", "/", "2"]) == 0
    assert capsys.readouterr().out.strip() == "4.0"


def test_main_reports_unknown_operator(capsys):
    assert main(["1", "%", "2"]) == 1
    assert "unknown operator" in capsys.readouterr().err
