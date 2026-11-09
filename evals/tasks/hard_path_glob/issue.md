match() treats paths wrongly

`match(pattern, path)` uses fnmatch, which doesn't understand paths. Please make it follow these rules, always matching the whole path:

- `*` matches any characters except `/`
- `?` matches exactly one character except `/`
- `**/` matches zero or more whole directories, so `**/a.py` matches `a.py` and `x/y/a.py`, and `a/**/b` matches `a/b` and `a/x/y/b`
- a pattern ending in `/**` matches everything below that directory but not the directory itself, so `src/**` matches `src/a` and `src/a/b` but not `src`
- `[abc]` matches one listed character, and `[!abc]` matches one character that isn't listed and isn't `/`
- every other character is literal, including `.`, `+`, `(` and `)`
