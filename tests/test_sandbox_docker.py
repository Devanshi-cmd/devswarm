"""Against a real Docker daemon. Skipped automatically where Docker is unavailable."""

import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from devswarm.sandbox.base import SandboxError
from devswarm.sandbox.check import PROBES
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import ensure_image
from devswarm.tools.base import ToolContext
from devswarm.tools.paths import Workspace
from devswarm.tools.registry import default_registry


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker is not available")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


@pytest.fixture
def image(repo: Path) -> str:
    return ensure_image(repo)


@pytest.fixture
def sandbox(repo: Path, image: str) -> Iterator[DockerSandbox]:
    with DockerSandbox(repo, image) as running:
        yield running


def _bash(sandbox: DockerSandbox, command: str, timeout: int = 30) -> tuple[int, str]:
    outcome = sandbox.exec(["bash", "-c", command], timeout=timeout)
    return outcome.exit_code, outcome.stdout + outcome.stderr


def _exists(name: str) -> bool:
    output = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(output.stdout.strip())


def _wait_until_gone(name: str) -> bool:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _exists(name):
            return True
        time.sleep(0.2)
    return False


@pytest.mark.parametrize("probe", PROBES, ids=lambda probe: probe.name)
def test_isolation_guarantees(sandbox: DockerSandbox, probe: object) -> None:
    command = probe.command  # type: ignore[attr-defined]
    passed = probe.passed  # type: ignore[attr-defined]
    assert passed(sandbox.exec(["bash", "-c", command], timeout=30))


def test_tests_run_inside_with_no_install(sandbox: DockerSandbox, repo: Path) -> None:
    code, output = _bash(sandbox, "pytest -q", timeout=60)
    assert code == 0, output
    assert "1 passed" in output
    assert not (repo / ".pytest_cache").exists()
    assert not (repo / "__pycache__").exists()


def test_host_environment_is_not_visible(repo: Path, image: str) -> None:
    os.environ["DEVSWARM_TEST_SECRET_TOKEN"] = "must-not-leak"
    try:
        with DockerSandbox(repo, image) as sandbox:
            assert "must-not-leak" not in _bash(sandbox, "env")[1]
    finally:
        del os.environ["DEVSWARM_TEST_SECRET_TOKEN"]


def test_writes_reach_the_mounted_repo_with_host_ownership(
    sandbox: DockerSandbox, repo: Path
) -> None:
    assert _bash(sandbox, "echo hi > made.txt")[0] == 0
    made = repo / "made.txt"
    assert made.read_text() == "hi\n"
    assert made.stat().st_uid == os.getuid()


def test_timeout_kills_everything_and_sandbox_recovers(sandbox: DockerSandbox, repo: Path) -> None:
    first_name = sandbox.name
    assert first_name is not None
    outcome = sandbox.exec(
        ["bash", "-c", "(sleep 3; touch straggler.txt) & echo started; sleep 60"], timeout=1
    )
    assert outcome.timed_out
    assert "started" in outcome.stdout
    assert sandbox.name != first_name
    assert _wait_until_gone(first_name)

    time.sleep(4)
    assert not (repo / "straggler.txt").exists()
    assert _bash(sandbox, "echo alive") == (0, "alive\n")


def test_output_flood_stays_bounded(sandbox: DockerSandbox) -> None:
    outcome = sandbox.exec(["bash", "-c", "head -c 50000000 /dev/zero | tr '\\0' 'x'; echo END"])
    assert outcome.exit_code == 0
    assert len(outcome.stdout) < 11_000
    assert "bytes omitted" in outcome.stdout
    assert outcome.stdout.rstrip().endswith("END")


def test_container_removed_on_normal_exit(repo: Path, image: str) -> None:
    with DockerSandbox(repo, image) as sandbox:
        name = sandbox.name
    assert name is not None and _wait_until_gone(name)


def test_container_removed_when_exception_escapes(repo: Path, image: str) -> None:
    name = None
    with pytest.raises(RuntimeError), DockerSandbox(repo, image) as sandbox:
        name = sandbox.name
        raise RuntimeError("crash")
    assert name is not None and _wait_until_gone(name)


@pytest.mark.parametrize(
    "signal_number", [signal.SIGINT, signal.SIGTERM], ids=["ctrl_c", "sigterm"]
)
def test_container_removed_on_signal(repo: Path, image: str, signal_number: int) -> None:
    script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from devswarm.sandbox.docker import DockerSandbox
        with DockerSandbox(Path({str(repo)!r}), {image!r}) as sandbox:
            print(sandbox.name, flush=True)
            time.sleep(60)
    """)
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    name = process.stdout.readline().strip()
    assert name.startswith("devswarm-")
    process.send_signal(signal_number)
    process.wait(timeout=30)
    assert _wait_until_gone(name)


def test_stopped_sandbox_refuses_commands(repo: Path, image: str) -> None:
    with DockerSandbox(repo, image) as sandbox:
        pass
    with pytest.raises(SandboxError):
        sandbox.exec(["true"])


def test_second_image_request_uses_the_cache(repo: Path, image: str) -> None:
    started = time.monotonic()
    assert ensure_image(repo) == image
    assert time.monotonic() - started < 5


def test_tools_end_to_end(sandbox: DockerSandbox, repo: Path) -> None:
    context = ToolContext(Workspace(repo), sandbox)
    registry = default_registry()

    search = registry.execute(context, "search_code", {"pattern": "def add", "glob": "*.py"})
    assert "calc.py:1:def add" in search.content

    edit = registry.execute(
        context, "edit_file", {"path": "calc.py", "old_str": "a + b", "new_str": "a + b + 1"}
    )
    assert not edit.is_error
    registry.execute(context, "create_file", {"path": "brand_new.py", "content": "x = 1\n"})

    diff = registry.execute(context, "git_diff", {})
    assert not diff.is_error, diff.content
    assert "+    return a + b + 1" in diff.content
    assert "brand_new.py" in diff.content  # Untracked files count as changes.

    tests = registry.execute(context, "run_command", {"cmd": "pytest -q", "timeout": 60})
    assert "exit_code: 1" in tests.content
    assert "assert 4 == 3" in tests.content

    # The diff must not have touched the real index, which is mounted read-only.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert "?? brand_new.py" in status.stdout
