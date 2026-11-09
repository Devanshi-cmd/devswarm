"""The interface tools use to run commands, so tests can swap in a fake."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

WORKSPACE = "/workspace"

DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600

# Errors tend to be at the end of a log, so the tail gets the larger share.
DEFAULT_HEAD_CHARS = 2_000
DEFAULT_TAIL_CHARS = 8_000


class SandboxError(Exception):
    """The sandbox itself failed, as opposed to a command inside it."""


@dataclass(frozen=True)
class SandboxLimits:
    cpus: str = "2"
    memory: str = "2g"
    pids: int = 256
    tmpfs_size: str = "512m"
    # The container exits by itself after this long. Even a SIGKILL of DevSwarm,
    # which no cleanup code can catch, cannot leave one running forever.
    lifetime_seconds: int = 3_600


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class Sandbox(Protocol):
    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: int = DEFAULT_COMMAND_TIMEOUT,
        head_chars: int = DEFAULT_HEAD_CHARS,
        tail_chars: int = DEFAULT_TAIL_CHARS,
    ) -> CommandResult: ...
