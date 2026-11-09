"""A tiny command-line calculator: python calc.py 8 / 2"""

import sys


class CalculatorError(Exception):
    """A problem with the user's input, reported without a traceback."""


def calculate(left: float, operator: str, right: float) -> float:
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if operator == "/":
        return left / right
    raise CalculatorError(f"unknown operator: {operator}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: calc.py <number> <operator> <number>", file=sys.stderr)
        return 2
    try:
        result = calculate(float(argv[0]), argv[1], float(argv[2]))
    except CalculatorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
