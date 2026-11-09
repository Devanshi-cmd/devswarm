"""Workspace-confined file access. Paths come from the model, so none are trusted.

File tools run on the host, while the agent's commands can change the same folder from
inside the container. Checking a path and then opening it would leave a gap in which a
symlink could be swapped in, so files are opened one component at a time, relative to
an already-open directory, refusing symlinks at every step.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from devswarm.sandbox.base import WORKSPACE

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK stops a planted FIFO from hanging the host when it is opened.
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_PROTECTED_TOP_LEVEL = ".git"


class PathError(Exception):
    """A path was refused or could not be opened. The message is safe to show the model."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.realpath(root))

    def resolve(self, path: str) -> PurePosixPath:
        """Return the symlink-free path relative to the root, or refuse it."""
        if "\0" in path:
            raise PathError("path contains a NUL byte")
        if path == WORKSPACE or path.startswith(f"{WORKSPACE}/"):
            path = path[len(WORKSPACE) :].lstrip("/") or "."
        candidate = PurePosixPath(path)
        if candidate.is_absolute():
            raise PathError(f"absolute paths outside {WORKSPACE} are not allowed: {path}")
        if ".." in candidate.parts:
            raise PathError(f"'..' is not allowed in paths: {path}")

        real = Path(os.path.realpath(self.root / candidate))
        if not real.is_relative_to(self.root):
            raise PathError(f"path escapes the workspace: {path}")
        relative = PurePosixPath(real.relative_to(self.root).as_posix())
        # .git can hold credentials and hooks, including when reached through a symlink.
        if relative.parts and relative.parts[0] == _PROTECTED_TOP_LEVEL:
            raise PathError(f"access to {_PROTECTED_TOP_LEVEL} is not allowed: {path}")
        return relative

    def _open_parent(self, relative: PurePosixPath, *, create: bool = False) -> int:
        fd = os.open(self.root, _DIRECTORY_FLAGS)
        try:
            for part in relative.parts[:-1]:
                try:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=fd)
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_file(self, relative: PurePosixPath, flags: int, *, create: bool = False) -> int:
        if not relative.parts:
            raise PathError("expected a file, got the workspace root")
        try:
            parent = self._open_parent(relative, create=create)
            try:
                fd = os.open(relative.name, flags | _FILE_FLAGS, 0o644, dir_fd=parent)
            finally:
                os.close(parent)
        except FileNotFoundError:
            raise PathError(f"file not found: {relative}") from None
        except FileExistsError:
            raise PathError(f"file already exists: {relative}") from None
        except IsADirectoryError:
            raise PathError(f"is a directory: {relative}") from None
        except OSError as exc:  # ELOOP and ENOTDIR are how a swapped-in symlink shows up.
            raise PathError(f"cannot open {relative}: {exc.strerror}") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise PathError(f"not a regular file: {relative}")
        return fd

    def read_bytes(self, path: str, *, limit: int) -> bytes:
        relative = self.resolve(path)
        fd = self._open_file(relative, os.O_RDONLY)
        with os.fdopen(fd, "rb") as file:
            if os.fstat(file.fileno()).st_size > limit:
                raise PathError(f"file is larger than {limit} bytes: {relative}")
            return file.read(limit + 1)

    def write_bytes(self, path: str, data: bytes, *, exclusive: bool = False) -> None:
        relative = self.resolve(path)
        flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if exclusive else os.O_TRUNC)
        fd = self._open_file(relative, flags, create=exclusive)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
