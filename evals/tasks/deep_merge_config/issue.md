merge_config loses nested settings

`merge_config(base, override)` throws away nested settings. With `base = {"db": {"host": "a", "port": 1}}` and `override = {"db": {"port": 2}}`, the result loses `host`.

Nested dictionaries should be merged recursively. A non-dictionary value in `override` replaces whatever is in `base`. Neither input may be modified.
