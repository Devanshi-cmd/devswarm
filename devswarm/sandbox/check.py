"""Probe a live sandbox and report whether each isolation guarantee holds."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devswarm.sandbox.base import CommandResult
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import ensure_image

_CONNECT = "import socket; socket.create_connection(('1.1.1.1', 53), 3)"


@dataclass(frozen=True)
class Probe:
    name: str
    command: str
    passed: Callable[[CommandResult], bool]


def _failed(result: CommandResult) -> bool:
    return result.exit_code != 0


PROBES = (
    Probe("network is disabled", f'python -c "{_CONNECT}"', _failed),
    Probe(
        "no secrets in environment",
        "env | grep -E 'ANTHROPIC|GITHUB_TOKEN'",
        _failed,
    ),
    Probe("runs as non-root", 'test "$(id -u)" != 0', lambda result: result.exit_code == 0),
    Probe("root filesystem is read-only", "touch /usr/devswarm-probe", _failed),
    Probe("repository .git is read-only", "touch .git/devswarm-probe", _failed),
    Probe(
        "memory limit is applied",
        "cat /sys/fs/cgroup/memory.max",
        lambda result: result.stdout.strip().isdigit(),
    ),
)


def run_checks(repo: Path) -> list[tuple[str, bool]]:
    with DockerSandbox(repo, ensure_image(repo)) as sandbox:
        return [
            (probe.name, probe.passed(sandbox.exec(["bash", "-c", probe.command], timeout=30)))
            for probe in PROBES
        ]
