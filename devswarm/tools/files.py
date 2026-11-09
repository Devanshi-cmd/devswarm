"""File tools: read_file, list_dir, edit_file and create_file."""

from __future__ import annotations

import os

from pydantic import Field

from devswarm.tools.base import Tool, ToolContext, ToolError, ToolInput
from devswarm.tools.paths import PathError, Workspace

_MAX_FILE_BYTES = 5_000_000
_MAX_READ_LINES = 1_000
_MAX_READ_CHARS = 40_000
_MAX_LIST_ENTRIES = 500
_SKIPPED_DIRECTORIES = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


def _read_text(workspace: Workspace, path: str) -> str:
    try:
        data = workspace.read_bytes(path, limit=_MAX_FILE_BYTES)
    except PathError as exc:
        raise ToolError(str(exc)) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"not a UTF-8 text file: {path}") from None


class ReadFileInput(ToolInput):
    path: str = Field(description="File path relative to the repository root.")
    start_line: int | None = Field(default=None, ge=1, description="First line, 1-indexed.")
    end_line: int | None = Field(default=None, ge=1, description="Last line, inclusive.")


def read_file(context: ToolContext, args: ReadFileInput) -> str:
    lines = _read_text(context.workspace, args.path).splitlines()
    first = args.start_line or 1
    last = min(args.end_line or len(lines), len(lines), first + _MAX_READ_LINES - 1)
    numbered = "\n".join(f"{number}\t{lines[number - 1]}" for number in range(first, last + 1))
    if len(numbered) > _MAX_READ_CHARS:
        numbered = numbered[:_MAX_READ_CHARS] + "\n... [output cut off, request fewer lines]"
    if last < len(lines):
        numbered += f"\n... [file has {len(lines)} lines; showing {first}-{last}]"
    return numbered or "(no lines in this range)"


class ListDirInput(ToolInput):
    path: str = Field(default=".", description="Directory relative to the repository root.")
    depth: int = Field(default=2, ge=1, le=6, description="How many levels to descend.")


def list_dir(context: ToolContext, args: ListDirInput) -> str:
    try:
        relative = context.workspace.resolve(args.path)
    except PathError as exc:
        raise ToolError(str(exc)) from exc
    start = context.workspace.root / relative
    if not start.is_dir():
        raise ToolError(f"not a directory: {args.path}")

    entries: list[str] = []
    pending = [(start, 1)]
    while pending and len(entries) <= _MAX_LIST_ENTRIES:
        directory, level = pending.pop(0)
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError:  # Removed by a running command, or unreadable.
            continue
        for child in children:
            if child.name in _SKIPPED_DIRECTORIES:
                continue
            shown = os.path.relpath(child.path, start)
            # Symlinks are listed but never followed, so a loop or escape goes nowhere.
            if child.is_symlink():
                entries.append(f"{shown}@")
            elif child.is_dir(follow_symlinks=False):
                entries.append(f"{shown}/")
                if level < args.depth:
                    pending.append((directory / child.name, level + 1))
            else:
                entries.append(shown)
    if len(entries) > _MAX_LIST_ENTRIES:
        entries = [*entries[:_MAX_LIST_ENTRIES], "... [listing cut off]"]
    return "\n".join(entries) or "(empty directory)"


class EditFileInput(ToolInput):
    path: str = Field(description="File path relative to the repository root.")
    old_str: str = Field(description="Exact text to replace. Must occur exactly once.")
    new_str: str = Field(description="Replacement text.")


def edit_file(context: ToolContext, args: EditFileInput) -> str:
    if not args.old_str:
        raise ToolError("old_str must not be empty")
    if args.old_str == args.new_str:
        raise ToolError("old_str and new_str are identical, so nothing would change")
    text = _read_text(context.workspace, args.path)
    count = text.count(args.old_str)
    if count == 0:
        raise ToolError(f"old_str was not found in {args.path}; read the file and copy it exactly")
    if count > 1:
        raise ToolError(f"old_str occurs {count} times in {args.path}; add surrounding context")
    try:
        context.workspace.write_bytes(
            args.path, text.replace(args.old_str, args.new_str).encode("utf-8")
        )
    except PathError as exc:
        raise ToolError(str(exc)) from exc
    return f"edited {args.path}"


class CreateFileInput(ToolInput):
    path: str = Field(description="New file path relative to the repository root.")
    content: str = Field(description="Full contents of the new file.")


def create_file(context: ToolContext, args: CreateFileInput) -> str:
    try:
        # Exclusive: replacing a whole existing file would bypass edit_file's exact match.
        context.workspace.write_bytes(args.path, args.content.encode("utf-8"), exclusive=True)
    except PathError as exc:
        raise ToolError(str(exc)) from exc
    return f"created {args.path}"


READ_FILE = Tool(
    "read_file",
    "Read a text file with line numbers, optionally limited to a line range.",
    ReadFileInput,
    read_file,
)
LIST_DIR = Tool(
    "list_dir",
    "List a directory tree. Directories end in / and symlinks end in @.",
    ListDirInput,
    list_dir,
)
EDIT_FILE = Tool(
    "edit_file",
    "Replace one exact, unique occurrence of old_str with new_str in a file.",
    EditFileInput,
    edit_file,
)
CREATE_FILE = Tool(
    "create_file",
    "Create a new file, including parent directories. Fails if the file already exists.",
    CreateFileInput,
    create_file,
)
