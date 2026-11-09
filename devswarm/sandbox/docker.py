"""One locked-down Docker container per run, with the repository mounted at /workspace.

The docker CLI is called directly so that each isolation flag is visible in one place.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any, Self

from devswarm.sandbox.base import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_HEAD_CHARS,
    DEFAULT_TAIL_CHARS,
    MAX_COMMAND_TIMEOUT,
    WORKSPACE,
    CommandResult,
    SandboxError,
    SandboxLimits,
)
from devswarm.sandbox.images import detect_profile
from devswarm.sandbox.output import HeadTailBuffer

_DOCKER_EXEC_FAILED = 125  # Docker's own failure code, e.g. the container is gone.
_CONTROL_TIMEOUT_SECONDS = 60


def _docker(args: list[str], timeout: int = _CONTROL_TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise SandboxError("the docker command is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"docker {args[0]} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise SandboxError(f"docker {args[0]} failed: {result.stderr.strip()[-1000:]}")
    return result.stdout.strip()


def _limits_for(repo: Path) -> SandboxLimits:
    try:
        return detect_profile(repo).limits
    except SandboxError:
        return SandboxLimits()


class DockerSandbox:
    def __init__(self, repo: Path, image: str, limits: SandboxLimits | None = None) -> None:
        self._repo = repo.resolve()
        if "," in str(self._repo):
            raise SandboxError("repository paths containing a comma cannot be mounted")
        self._image = image
        self._limits = limits or _limits_for(self._repo)
        self._name: str | None = None
        self._previous_sigterm: Any = None
        self._handles_sigterm = False

    def __enter__(self) -> Self:
        # Turning SIGTERM into an exception makes `with` blocks unwind and clean up.
        if threading.current_thread() is threading.main_thread():
            self._previous_sigterm = signal.signal(signal.SIGTERM, _raise_system_exit)
            self._handles_sigterm = True
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
        if self._handles_sigterm:
            signal.signal(signal.SIGTERM, self._previous_sigterm)
            self._handles_sigterm = False

    @property
    def name(self) -> str | None:
        return self._name

    def _run_arguments(self, name: str) -> list[str]:
        limits = self._limits
        arguments = [
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--label",
            "devswarm.sandbox=1",
            "--network",
            "none",
            "--cpus",
            limits.cpus,
            "--memory",
            limits.memory,
            "--pids-limit",
            str(limits.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,exec,size={limits.tmpfs_size}",
            # Running as the host user keeps files in the mounted repo owned by them.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            WORKSPACE,
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            # Tests must be able to import the project, which is deliberately not installed.
            "--env",
            f"PYTHONPATH={WORKSPACE}:{WORKSPACE}/src",
            "--mount",
            f"type=bind,source={self._repo},target={WORKSPACE}",
        ]
        if (self._repo / ".git").exists():
            # Host git later reads this directory, so a planted hook or config here
            # would run outside the sandbox. Read-only closes that path.
            arguments += [
                "--mount",
                f"type=bind,source={self._repo / '.git'},target={WORKSPACE}/.git,readonly",
            ]
        return [*arguments, self._image, "sleep", str(limits.lifetime_seconds)]

    def start(self) -> None:
        if self._name is not None:
            raise SandboxError("sandbox is already running")
        name = f"devswarm-{uuid.uuid4().hex[:12]}"
        _docker(self._run_arguments(name))
        self._name = name

    def stop(self) -> None:
        name, self._name = self._name, None
        if name is not None:
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True,
                timeout=_CONTROL_TIMEOUT_SECONDS,
                check=False,
            )

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: int = DEFAULT_COMMAND_TIMEOUT,
        head_chars: int = DEFAULT_HEAD_CHARS,
        tail_chars: int = DEFAULT_TAIL_CHARS,
    ) -> CommandResult:
        if self._name is None:
            raise SandboxError("sandbox is not running")
        timeout = max(1, min(timeout, MAX_COMMAND_TIMEOUT))
        stdout = HeadTailBuffer(head_chars, tail_chars)
        stderr = HeadTailBuffer(head_chars, tail_chars)
        started = time.perf_counter()
        process = subprocess.Popen(
            ["docker", "exec", self._name, *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
        finally:
            # Killing the docker client alone would leave the command running inside.
            if process.poll() is None:
                process.kill()
                process.wait()
        for reader in readers:
            reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()

        if timed_out:
            self._replace_container()
        elif exit_code == _DOCKER_EXEC_FAILED and "container" in stderr.text().lower():
            raise SandboxError(f"docker exec failed: {stderr.text().strip()[-1000:]}")
        return CommandResult(
            exit_code=exit_code,
            stdout=stdout.text(),
            stderr=stderr.text(),
            timed_out=timed_out,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _replace_container(self) -> None:
        # Killing the whole container is the only reliable way to stop every process a
        # timed-out command spawned. The repo is a mount, so no work is lost.
        self.stop()
        self.start()


def _raise_system_exit(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)
