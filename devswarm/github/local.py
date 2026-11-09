"""Prepare a private working copy of a repo, and turn the result into a branch.

Agents never touch the user's checkout. They work on a copy, so a failed run leaves
the original exactly as it was.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from git import Repo
from git.exc import GitCommandError

logger = logging.getLogger(__name__)

_COPY_IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".devswarm"
)
_BOT_NAME = "DevSwarm"
_BOT_EMAIL = "devswarm@users.noreply.github.com"
_SLUG_LENGTH = 40
_PROTECTED_BRANCHES = frozenset({"main", "master"})
_PUSHABLE_PREFIX = "devswarm/"


class WorkspaceError(Exception):
    """The repository could not be prepared or the result could not be saved."""


@dataclass(frozen=True)
class LocalWorkspace:
    path: Path
    branch: str
    source: Path | None  # Set when the original is a git repo that can receive the branch.


def branch_name(task: str, run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:_SLUG_LENGTH].strip("-")
    return f"devswarm/{slug or 'task'}-{run_id[:6]}"


def _git_with_identity(repo: Repo) -> None:
    repo.git.update_environment(
        GIT_AUTHOR_NAME=_BOT_NAME,
        GIT_AUTHOR_EMAIL=_BOT_EMAIL,
        GIT_COMMITTER_NAME=_BOT_NAME,
        GIT_COMMITTER_EMAIL=_BOT_EMAIL,
    )


def prepare_local_workspace(repo: Path, target: Path, branch: str) -> LocalWorkspace:
    source = repo.resolve()
    if not source.is_dir():
        raise WorkspaceError(f"repository not found: {repo}")
    target.parent.mkdir(parents=True, exist_ok=True)

    if (source / ".git").exists():
        original = Repo(source)
        if not original.head.is_valid():
            raise WorkspaceError("the repository has no commits yet")
        if original.is_dirty(untracked_files=True):
            logger.warning("uncommitted changes in %s will not be included", repo)
        # A clone carries no hooks or local config from the original.
        working = Repo.clone_from(str(source), str(target), no_hardlinks=True)
        kept_source: Path | None = source
    else:
        # symlinks=True copies links as links, so one pointing outside can't pull files in.
        shutil.copytree(source, target, symlinks=True, ignore=_COPY_IGNORED)
        working = Repo.init(target, initial_branch="main")
        _git_with_identity(working)
        working.git.add(all=True)
        working.git.commit("--no-verify", "--allow-empty", "-m", "Baseline before DevSwarm")
        kept_source = None

    working.git.checkout("-b", branch)
    return LocalWorkspace(path=target, branch=branch, source=kept_source)


def exclude_generated(workspace: LocalWorkspace, names: tuple[str, ...]) -> None:
    """Keep installed packages and build output out of diffs and commits.

    Uses git's local exclude file, because a repository's own .gitignore can't be relied on.
    """
    if not names:
        return
    exclude = workspace.path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as file:
        file.write("".join(f"{name}/\n" for name in names))


def remove_generated(root: Path, names: tuple[str, ...]) -> None:
    for directory, subdirectories, _ in os.walk(root):
        for name in list(subdirectories):
            if name == ".git":
                subdirectories.remove(name)
            elif name in names:
                path = Path(directory) / name
                # A symlink is removed itself, never followed into wherever it points.
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=True)
                subdirectories.remove(name)


def commit_all(workspace: LocalWorkspace, message: str) -> bool:
    """Commit everything in the working copy. Returns False when there was nothing to commit."""
    repo = Repo(workspace.path)
    _git_with_identity(repo)
    repo.git.add(all=True)
    if not repo.git.status(porcelain=True):
        return False
    repo.git.commit("--no-verify", "-m", message)
    return True


def last_commit_diff(workspace: LocalWorkspace) -> str:
    return str(Repo(workspace.path).git.diff("--no-ext-diff", "--no-textconv", "HEAD~1", "HEAD"))


def export_branch(workspace: LocalWorkspace) -> None:
    """Copy the finished branch into the user's repo, without touching their checkout."""
    if workspace.source is None:
        return
    refspec = f"{workspace.branch}:{workspace.branch}"
    try:
        Repo(workspace.source).git.fetch(os.fspath(workspace.path), refspec)
    except GitCommandError as exc:
        raise WorkspaceError(f"could not export branch: {exc.stderr.strip()}") from exc


class GitRepoState:
    """The orchestrator's view of the working copy.

    Runs host git on files the agent has touched, which is safe because the sandbox
    can only reach .git read-only, so hooks and config in it are still ours.
    """

    def __init__(self, workspace: LocalWorkspace, is_test_path: Callable[[str], bool]) -> None:
        self._repo = Repo(workspace.path)
        self._is_test_path = is_test_path

    def is_test_path(self, path: str) -> bool:
        return self._is_test_path(path)

    def snapshot(self) -> str:
        """Record the whole working tree in git's object store and return its id."""
        self._repo.git.add(all=True)
        return str(self._repo.git.write_tree())

    def changed_paths(self, before: str, after: str) -> list[str]:
        output = str(self._repo.git.diff("--name-only", "--no-renames", "-z", before, after))
        return [path for path in output.split("\0") if path]

    def restore(self, snapshot: str, paths: list[str]) -> None:
        for path in paths:
            if self._repo.git.ls_tree(snapshot, "--", path):
                self._repo.git.checkout(snapshot, "--", path)
            else:
                self._repo.git.rm("-f", "--ignore-unmatch", "--", path)

    def diff(self) -> str:
        self._repo.git.add(all=True)
        return str(self._repo.git.diff("--cached", "--no-ext-diff", "--no-textconv", "HEAD"))

    def reset_to_base(self) -> None:
        self._repo.git.reset("--hard", "HEAD")
        self._repo.git.clean("-fdx")


def clone_for_issue(url: str, target: Path, branch: str, auth: dict[str, str]) -> LocalWorkspace:
    target.parent.mkdir(parents=True, exist_ok=True)
    repo = Repo.clone_from(url, str(target), env=auth)
    repo.git.checkout("-b", branch)
    return LocalWorkspace(path=target, branch=branch, source=None)


def push_branch(workspace: LocalWorkspace, default_branch: str, auth: dict[str, str]) -> None:
    """Push the run's branch. The only code path in DevSwarm that writes to a remote."""
    branch = workspace.branch
    if (
        branch in _PROTECTED_BRANCHES
        or branch == default_branch
        or not branch.startswith(_PUSHABLE_PREFIX)
    ):
        raise WorkspaceError(f"refusing to push to branch {branch!r}")
    repo = Repo(workspace.path)
    # No "+" in the refspec and no --force: a push that isn't a fast-forward just fails.
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    try:
        with repo.git.custom_environment(**auth):
            repo.git.push("--no-verify", "origin", refspec)
    except GitCommandError as exc:
        raise WorkspaceError(f"push failed: {exc.stderr.strip()}") from exc
