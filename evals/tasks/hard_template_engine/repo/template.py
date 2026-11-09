import re


def render(template: str, context: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda match: str(context[match[1]]), template)
