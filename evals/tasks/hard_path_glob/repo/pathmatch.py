import fnmatch


def match(pattern: str, path: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)
