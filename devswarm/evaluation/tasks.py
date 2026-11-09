"""Load eval tasks from disk.

Each task has ``repo/`` (the only part any agent ever sees), ``issue.md``, and
``hidden/`` and ``solution.json``, which never leave the task directory until scoring.
"""

from __future__ import annotations

import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path


class TaskError(Exception):
    """A task directory is malformed, or a requested task doesn't exist."""


@dataclass(frozen=True)
class EvalTask:
    id: str
    category: str
    issue: str
    root: Path

    @property
    def repo_path(self) -> Path:
        return self.root / "repo"

    @property
    def hidden_path(self) -> Path:
        return self.root / "hidden"


def load_tasks(root: Path, only: Collection[str] = ()) -> list[EvalTask]:
    if not root.is_dir():
        raise TaskError(f"no tasks directory at {root}")
    tasks = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if only and directory.name not in only:
            continue
        for required in ("repo", "hidden", "issue.md", "task.toml"):
            if not (directory / required).exists():
                raise TaskError(f"{directory.name} is missing {required}")
        metadata = tomllib.loads((directory / "task.toml").read_text())
        tasks.append(
            EvalTask(
                id=directory.name,
                category=metadata["category"],
                issue=(directory / "issue.md").read_text().strip(),
                root=directory.resolve(),
            )
        )
    missing = set(only) - {task.id for task in tasks}
    if missing:
        raise TaskError(f"unknown tasks: {', '.join(sorted(missing))}")
    return tasks
