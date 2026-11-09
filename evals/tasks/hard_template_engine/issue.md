Improve render()

`render(template, context)` replaces `{{name}}` with a value from `context`. It needs to handle more:

1. Spaces inside the braces are allowed: `{{ name }}`.
2. Dotted names look inside nested dictionaries: `{{ user.name }}`.
3. A backslash escapes an opening: `\{{ name }}` renders as the literal text `{{ name }}`, with nothing substituted.
4. A name that can't be found, including a dotted path that hits something that isn't a dictionary, raises `MissingVariable`, a new exception in template.py that subclasses KeyError. Its message must contain the full name as written, such as `user.age`.
5. A value of None renders as an empty string.
6. Substituted values are final. If a value itself contains `{{x}}`, that text appears literally and isn't expanded.
