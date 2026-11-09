Add a max_length option to slugify

Please add an optional `max_length` parameter to `slugify(text, max_length=None)`.

When it is set, the slug must be no longer than `max_length`, it must be cut at a word boundary rather than in the middle of a word, and it must not end with a hyphen. If even the first word is longer than `max_length`, cut that word to exactly `max_length` characters. With no `max_length`, behaviour stays the same.
