from pathlib import Path

import pytest
from git import Repo

from devswarm.github.local import (
    WorkspaceError,
    branch_name,
    commit_all,
    export_branch,
    last_commit_diff,
    prepare_local_workspace,
)


def _git_repo(path: Path) -> Repo:
    path.mkdir()
    (path / "a.txt").write_text("one\n")
    repo = Repo.init(path, initial_branch="main")
    with repo.config_writer() as config:
        config.set_value("user", "name", "t")
        config.set_value("user", "email", "t@example.com")
    repo.git.add(all=True)
    repo.git.commit("-m", "init")
    return repo


def test_branch_name_is_safe() -> None:
    assert branch_name("Division by zero: CRASHES!", "abcdef123") == (
        "devswarm/division-by-zero-crashes-abcdef"
    )
    assert branch_name("!!!", "abcdef123") == "devswarm/task-abcdef"


def test_plain_folder_is_copied_with_a_baseline(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("x\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "a.pyc").write_text("junk")

    workspace = prepare_local_workspace(source, tmp_path / "w" / "repo", "devswarm/x")
    assert workspace.source is None
    repo = Repo(workspace.path)
    assert repo.active_branch.name == "devswarm/x"
    assert repo.head.commit.message.startswith("Baseline")
    assert not (workspace.path / "__pycache__").exists()


def test_symlinks_are_copied_as_links_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    source = tmp_path / "src"
    source.mkdir()
    (source / "link").symlink_to(secret)

    workspace = prepare_local_workspace(source, tmp_path / "w" / "repo", "devswarm/x")
    assert (workspace.path / "link").is_symlink()


def test_changes_become_a_commit_and_a_diff(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("x\n")
    workspace = prepare_local_workspace(source, tmp_path / "w" / "repo", "devswarm/x")

    (workspace.path / "a.py").write_text("y\n")
    assert commit_all(workspace, "change") is True
    assert "+y" in last_commit_diff(workspace)
    assert Repo(workspace.path).head.commit.author.name == "DevSwarm"


def test_nothing_to_commit_returns_false(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("x\n")
    workspace = prepare_local_workspace(source, tmp_path / "w" / "repo", "devswarm/x")
    assert commit_all(workspace, "nothing") is False


def test_git_repo_gets_the_branch_and_its_checkout_is_untouched(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "src")
    workspace = prepare_local_workspace(Path(source.working_dir), tmp_path / "w" / "repo", "dev/x")
    (workspace.path / "a.txt").write_text("changed\n")
    commit_all(workspace, "change")
    export_branch(workspace)

    assert "dev/x" in [head.name for head in source.heads]
    assert source.active_branch.name == "main"
    assert (tmp_path / "src" / "a.txt").read_text() == "one\n"
    assert source.git.show("dev/x:a.txt") == "changed"


def test_hooks_in_the_original_are_not_carried_over(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "src")
    hook = Path(source.git_dir) / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch /tmp/devswarm-hook-ran\nexit 1\n")
    hook.chmod(0o755)

    workspace = prepare_local_workspace(Path(source.working_dir), tmp_path / "w" / "repo", "dev/x")
    (workspace.path / "a.txt").write_text("changed\n")
    assert commit_all(workspace, "change") is True


def test_missing_repo_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not found"):
        prepare_local_workspace(tmp_path / "nope", tmp_path / "w", "dev/x")


def test_repo_without_commits_is_an_error(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    Repo.init(source)
    with pytest.raises(WorkspaceError, match="no commits"):
        prepare_local_workspace(source, tmp_path / "w", "dev/x")
