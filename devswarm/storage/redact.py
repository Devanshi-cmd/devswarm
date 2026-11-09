"""Remove secrets from anything we store or log."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from devswarm.config import is_placeholder

REPLACEMENT = "[REDACTED]"

_MIN_SECRET_LENGTH = 8  # Shorter values would mangle ordinary text.
_SECRET_ENV_NAME = re.compile(r"(^|_)(KEY|APIKEY|TOKEN|SECRET|PASSWORD)$")

# Patterns catch secrets we were never told about, such as one pasted into an issue.
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        unique = {secret for secret in secrets if len(secret) >= _MIN_SECRET_LENGTH}
        # Longest first, so a secret containing another is replaced whole.
        self._secrets = sorted(unique, key=len, reverse=True)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> Redactor:
        source = os.environ if env is None else env
        found = [
            value
            for name, value in source.items()
            if _SECRET_ENV_NAME.search(name.upper()) and not is_placeholder(value)
        ]
        return cls([*found, *extra])

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REPLACEMENT)
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(REPLACEMENT, value)
        return value

    def value(self, data: Any) -> Any:
        """Redact every string inside nested dicts and lists, including dict keys."""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, dict):
            return {self.value(key): self.value(item) for key, item in data.items()}
        if isinstance(data, list | tuple):
            return [self.value(item) for item in data]
        return data


class RedactingLogFilter(logging.Filter):
    """Attach to a handler; filters on a logger miss records from its child loggers."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self._redactor.text(
                logging.Formatter().formatException(record.exc_info)
            )
        return True
