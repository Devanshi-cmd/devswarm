"""Sandbox logic that needs no Docker daemon."""

from pathlib import Path

import pytest

from devswarm.sandbox.base import SandboxError
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import PythonProfile, detect_profile, image_tag
from devswarm.sandbox.output import HeadTailBuffer


def test_short_output_is_unchanged() -> None:
    buffer = HeadTailBuffer(10, 10)
    buffer.feed(b"hello")
    assert buffer.text() == "hello"


def test_long_output_keeps_both_ends_and_counts_the_gap() -> None:
    buffer = HeadTailBuffer(5, 5)
    buffer.feed(b"AAAAA" + b"x" * 100 + b"ZZZZZ")
    assert buffer.text() == "AAAAA\n... [100 bytes omitted] ...\nZZZZZ"


def test_chunks_are_handled_across_boundaries() -> None:
    buffer = HeadTailBuffer(3, 3)
    for chunk in (b"ab", b"cd", b"ef", b"gh"):
        buffer.feed(chunk)
    assert buffer.text() == "abc\n... [2 bytes omitted] ...\nfgh"


def test_exactly_full_is_not_reported_as_truncated() -> None:
    buffer = HeadTailBuffer(3, 3)
    buffer.feed(b"abcdef")
    assert buffer.text() == "abcdef"


def test_invalid_utf8_does_not_crash() -> None:
    buffer = HeadTailBuffer(10, 10)
    buffer.feed(b"\xff\xfe")
    assert buffer.text()


def test_python_repo_is_detected(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    assert detect_profile(tmp_path).name == "python"


def test_unknown_language_is_refused(tmp_path: Path) -> None:
    (tmp_path / "main.rs").write_text("")
    with pytest.raises(SandboxError, match="no language profile"):
        detect_profile(tmp_path)


def test_dependencies_are_merged_and_pytest_always_included(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==3.0  # web\nrequests\n\n# comment\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["click>=8"]\n'
        '[project.optional-dependencies]\ntest=["hypothesis"]\ndocs=["sphinx"]\n'
        '[dependency-groups]\ndev=["ruff", {include-group="other"}]\n'
    )
    assert PythonProfile().dependencies(tmp_path) == [
        "click>=8",
        "flask==3.0",
        "hypothesis",
        "pytest",
        "requests",
        "ruff",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "-r ../../etc/passwd",
        "--index-url https://evil.example/simple",
        "git+https://evil.example/pkg.git",
        "./local/package",
        "/abs/package",
        "https://evil.example/pkg.whl",
        "-e .",
    ],
)
def test_dangerous_requirement_lines_are_dropped(tmp_path: Path, line: str) -> None:
    (tmp_path / "requirements.txt").write_text(f"{line}\n")
    assert PythonProfile().dependencies(tmp_path) == ["pytest"]


def test_invalid_pyproject_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("not [valid toml")
    assert PythonProfile().dependencies(tmp_path) == ["pytest"]


def test_image_tag_changes_only_when_inputs_change() -> None:
    profile = PythonProfile()
    assert image_tag(profile, "D", ["a"]) == image_tag(profile, "D", ["a"])
    assert image_tag(profile, "D", ["a"]) != image_tag(profile, "D", ["b"])


def _arguments(repo: Path) -> list[str]:
    return DockerSandbox(repo, "image")._run_arguments("name")


def test_container_is_locked_down(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path)
    pairs = dict(zip(arguments, arguments[1:], strict=False))
    assert pairs["--network"] == "none"
    assert pairs["--cap-drop"] == "ALL"
    assert pairs["--security-opt"] == "no-new-privileges"
    assert "--read-only" in arguments
    assert "--memory" in arguments and "--cpus" in arguments and "--pids-limit" in arguments


def test_no_secret_is_passed_to_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-appear")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-appear")
    joined = " ".join(_arguments(tmp_path))
    assert "should-not-appear" not in joined
    assert "ANTHROPIC" not in joined and "GITHUB" not in joined


def test_git_directory_is_mounted_read_only_when_present(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert any(
        argument.endswith("target=/workspace/.git,readonly") for argument in _arguments(tmp_path)
    )


def test_missing_git_directory_is_not_mounted(tmp_path: Path) -> None:
    assert not any("/.git" in argument for argument in _arguments(tmp_path))


def test_comma_in_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="comma"):
        DockerSandbox(tmp_path / "a,b", "image")


def test_exec_requires_a_running_container(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="not running"):
        DockerSandbox(tmp_path, "image").exec(["true"])
