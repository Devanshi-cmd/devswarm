"""Prepare a private working copy of a repo, and turn the result into a branch.

Agents never touch the user's checkout. They work on a copy, so a failed run leaves
the original exactly as it was.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
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
