"""Path safety: every input here is something a hostile repo or model could produce."""

import os
import threading
from pathlib import Path, PurePosixPath

import pytest

from devswarm.tools.paths import PathError, Workspace


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("TOP SECRET")
    return secret


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[remote]\nurl = https://token@example.com\n")
    return Workspace(root)


@pytest.mark.parametrize(
    "path",
    [
        "../outside/secret.txt",
        "src/../../outside/secret.txt",
        "src/../a.py",
        "/etc/passwd",
        "/workspace/../etc/passwd",
        "/workspacefoo/a.py",
        "src\0/a.py",
    ],
)
def test_escaping_paths_are_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(PathError):
        workspace.resolve(path)


@pytest.mark.parametrize("path", ["src/a.py", "./src/a.py", "/workspace/src/a.py"])
def test_valid_spellings_resolve_to_the_same_file(workspace: Workspace, path: str) -> None:
    assert workspace.resolve(path) == PurePosixPath("src/a.py")


def test_workspace_root_resolves(workspace: Workspace) -> None:
    assert workspace.resolve("/workspace") == PurePosixPath(".")
    assert workspace.resolve(".") == PurePosixPath(".")


def test_symlink_to_outside_file_is_refused(workspace: Workspace, outside: Path) -> None:
    (workspace.root / "link.txt").symlink_to(outside)
    with pytest.raises(PathError, match="escapes"):
        workspace.read_bytes("link.txt", limit=1000)


def test_symlinked_directory_to_outside_is_refused(workspace: Workspace, outside: Path) -> None:
    (workspace.root / "linkdir").symlink_to(outside.parent)
    with pytest.raises(PathError, match="escapes"):
        workspace.read_bytes("linkdir/secret.txt", limit=1000)


def test_write_through_symlink_to_outside_is_refused(workspace: Workspace, outside: Path) -> None:
    (workspace.root / "link.txt").symlink_to(outside)
    with pytest.raises(PathError):
        workspace.write_bytes("link.txt", b"overwritten")
    assert outside.read_text() == "TOP SECRET"


def test_broken_symlink_pointing_outside_cannot_create_a_file(
    workspace: Workspace, tmp_path: Path
) -> None:
    target = tmp_path / "planted.txt"
    (workspace.root / "new.txt").symlink_to(target)
    with pytest.raises(PathError):
        workspace.write_bytes("new.txt", b"x", exclusive=True)
    assert not target.exists()


def test_symlink_inside_workspace_is_followed_safely(workspace: Workspace) -> None:
    (workspace.root / "alias.py").symlink_to(workspace.root / "src" / "a.py")
    assert workspace.read_bytes("alias.py", limit=1000) == b"print('a')\n"


@pytest.mark.parametrize("path", [".git/config", ".git", "/workspace/.git/HEAD"])
def test_git_directory_is_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(PathError, match=r"\.git"):
        workspace.resolve(path)


def test_symlink_into_git_directory_is_refused(workspace: Workspace) -> None:
    (workspace.root / "cfg").symlink_to(workspace.root / ".git" / "config")
    with pytest.raises(PathError, match=r"\.git"):
        workspace.read_bytes("cfg", limit=1000)


def test_lookalike_git_names_are_allowed(workspace: Workspace) -> None:
    assert workspace.resolve(".github/workflows/ci.yml").parts[0] == ".github"
    assert workspace.resolve(".gitignore").name == ".gitignore"


def test_symlink_swapped_in_after_the_check_is_not_followed(
    workspace: Workspace, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates the race: the path was clean when checked, then became a symlink.
    target = workspace.root / "src" / "a.py"
    real_resolve = workspace.resolve

    def resolve_then_swap(path: str) -> PurePosixPath:
        relative = real_resolve(path)
        target.unlink()
        target.symlink_to(outside)
        return relative

    monkeypatch.setattr(workspace, "resolve", resolve_then_swap)
    with pytest.raises(PathError):
        workspace.read_bytes("src/a.py", limit=1000)


def test_directory_swapped_for_symlink_after_the_check_is_not_followed(
    workspace: Workspace, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_resolve = workspace.resolve

    def resolve_then_swap(path: str) -> PurePosixPath:
        relative = real_resolve(path)
        (workspace.root / "src" / "a.py").unlink()
        (workspace.root / "src").rmdir()
        (workspace.root / "src").symlink_to(outside.parent)
        return PurePosixPath("src/secret.txt") if relative else relative

    monkeypatch.setattr(workspace, "resolve", resolve_then_swap)
    with pytest.raises(PathError):
        workspace.read_bytes("src/a.py", limit=1000)


def test_fifo_does_not_hang(workspace: Workspace) -> None:
    os.mkfifo(workspace.root / "pipe")
    outcome: list[type[Exception]] = []

    def attempt() -> None:
        try:
            workspace.read_bytes("pipe", limit=1000)
        except PathError:
            outcome.append(PathError)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome == [PathError]


def test_oversized_file_is_refused(workspace: Workspace) -> None:
    (workspace.root / "big.bin").write_bytes(b"x" * 2000)
    with pytest.raises(PathError, match="larger than"):
        workspace.read_bytes("big.bin", limit=1000)


def test_missing_file_message_has_no_host_path(workspace: Workspace) -> None:
    with pytest.raises(PathError) as caught:
        workspace.read_bytes("src/missing.py", limit=1000)
    assert str(workspace.root) not in str(caught.value)


def test_exclusive_write_creates_parents(workspace: Workspace) -> None:
    workspace.write_bytes("new/deep/file.txt", b"hi", exclusive=True)
    assert (workspace.root / "new/deep/file.txt").read_bytes() == b"hi"


def test_exclusive_write_refuses_existing(workspace: Workspace) -> None:
    with pytest.raises(PathError, match="already exists"):
        workspace.write_bytes("src/a.py", b"x", exclusive=True)
