"""Real image builds and test runs for each language. Skipped where Docker is unavailable."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import detect_profile, ensure_image

EXAMPLES = Path(__file__).parent.parent / "examples"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker is not available")


@pytest.mark.parametrize(
    ("example", "language", "fix"),
    [
        ("buggy_node", "node", lambda r: _replace(r / "pager.js", "Math.floor", "Math.ceil")),
        (
            "buggy_rust",
            "rust",
            lambda r: _replace(r / "src/lib.rs", "total / size", "total.div_ceil(size)"),
        ),
    ],
)
def test_example_builds_runs_offline_and_reports_failures(
    tmp_path: Path, example: str, language: str, fix: Callable[[Path], None]
) -> None:
    repo = tmp_path / example
    shutil.copytree(EXAMPLES / example, repo)
    profile = detect_profile(repo)
    assert profile.name == language

    with DockerSandbox(repo, ensure_image(repo)) as sandbox:
        setup = profile.setup_command()
        if setup:
            assert sandbox.exec(setup, timeout=300).exit_code == 0

        passing = sandbox.exec(profile.test_command(), timeout=profile.test_timeout)
        assert passing.exit_code == 0, passing.stdout + passing.stderr

        (repo / profile_test_path(language)).write_text(failing_test(language))
        failing = sandbox.exec(profile.test_command(), timeout=profile.test_timeout)
        assert failing.exit_code != 0
        assert profile.failure_fingerprints(failing.stdout + failing.stderr)

        fix(repo)
        fixed = sandbox.exec(profile.test_command(), timeout=profile.test_timeout)
        assert fixed.exit_code == 0, fixed.stdout + fixed.stderr


def profile_test_path(language: str) -> str:
    return "pager.hidden.test.js" if language == "node" else "tests/hidden.rs"


def failing_test(language: str) -> str:
    if language == "node":
        return (
            'const test = require("node:test");\n'
            'const assert = require("node:assert");\n'
            'const { pageCount } = require("./pager.js");\n'
            'test("partial page", () => assert.strictEqual(pageCount(11, 5), 3));\n'
        )
    return "#[test]\nfn partial_page() {\n    assert_eq!(buggy_rust::page_count(11, 5), 3);\n}\n"


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
