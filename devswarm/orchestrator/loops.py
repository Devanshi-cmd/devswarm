"""Notice when a run is repeating itself instead of making progress."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterable

FAILURE_REPEAT_LIMIT = 3

_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/.+$", re.MULTILINE)
_INDEX_LINE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+.*$\n?", re.MULTILINE)


def without_paths(diff: str, excluded: Callable[[str], bool]) -> str:
    """Drop the per-file sections of a unified diff whose path matches ``excluded``."""
    matches = list(_FILE_HEADER.finditer(diff))
    kept = [
        diff[match.start() : matches[index + 1].start() if index + 1 < len(matches) else len(diff)]
        for index, match in enumerate(matches)
        if not excluded(match.group(1))
    ]
    return "".join(kept)


class LoopDetector:
    def __init__(self) -> None:
        self._seen_diffs: set[str] = set()
        self._failures: Counter[str] = Counter()

    def reset(self) -> None:
        self._seen_diffs.clear()
        self._failures.clear()

    def record_diff(self, diff: str) -> bool:
        """Return True if this exact change has been produced before."""
        # Index lines hold blob hashes, which say nothing the content doesn't.
        normalized = _INDEX_LINE.sub("", diff).strip()
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        if digest in self._seen_diffs:
            return True
        self._seen_diffs.add(digest)
        return False

    def record_failures(self, fingerprints: Iterable[str]) -> bool:
        """Return True once any single failure has now been seen the limit number of times."""
        self._failures.update(set(fingerprints))
        return any(count >= FAILURE_REPEAT_LIMIT for count in self._failures.values())
