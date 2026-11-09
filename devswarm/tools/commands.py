"""Tools that run inside the sandbox: search_code, run_command and git_diff.

Even git and ripgrep run there. A repository's own git config can name programs for
git to execute, so running git on untrusted repos on the host would break rule one.
"""

from __future__ import annotations

from pydantic import Field

from devswarm.sandbox.base import DEFAULT_COMMAND_TIMEOUT, MAX_COMMAND_TIMEOUT
from devswarm.tools.base import Tool, ToolContext, ToolError, ToolInput

_DIFF_HEAD_CHARS = 30_000
_DIFF_TAIL_CHARS = 10_000
_RG_NO_MATCHES = 1

# .git is mounted read-only, so `add` works on a private copy of the index and writes any
# object to a temp directory, while still reading the real ones through the alternates
# path. Intent-to-add makes new untracked files appear in the diff.
_GIT_DIFF_SCRIPT = r"""
export GIT_INDEX_FILE="$(mktemp)" GIT_OPTIONAL_LOCKS=0
export GIT_OBJECT_DIRECTORY="$(mktemp -d)" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PWD/.git/objects"
cp .git/index "$GIT_INDEX_FILE" 2>/dev/null || rm -f "$GIT_INDEX_FILE"
git -c safe.directory='*' add --intent-to-add . &&
git -c safe.directory='*' diff --no-color --no-ext-diff --no-textconv HEAD
"""


class SearchCodeInput(ToolInput):
    pattern: str = Field(description="Ripgrep regular expression.")
    glob: str | None = Field(default=None, description="Limit files, e.g. '*.py'.")


def search_code(context: ToolContext, args: SearchCodeInput) -> str:
    argv = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--max-count=50",
        "--max-columns=300",
        "--max-columns-preview",
    ]
    if args.glob:
        argv += ["--glob", args.glob]
    result = context.sandbox.exec([*argv, "-e", args.pattern, "."], timeout=60)
    if result.exit_code == _RG_NO_MATCHES:
        return "no matches"
    if result.exit_code != 0:
        raise ToolError(f"search failed: {result.stderr.strip()}")
    return result.stdout


class RunCommandInput(ToolInput):
    cmd: str = Field(description="Shell command, run with bash in the repository root.")
    timeout: int = Field(
        default=DEFAULT_COMMAND_TIMEOUT,
        ge=1,
        le=MAX_COMMAND_TIMEOUT,
        description="Seconds before the command is killed.",
    )


def run_command(context: ToolContext, args: RunCommandInput) -> str:
    result = context.sandbox.exec(["bash", "-c", args.cmd], timeout=args.timeout)
    report = f"exit_code: {result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    if result.timed_out:
        raise ToolError(f"command timed out after {args.timeout}s\n{report}")
    return report


class GitDiffInput(ToolInput):
    pass


def git_diff(context: ToolContext, args: GitDiffInput) -> str:
    result = context.sandbox.exec(
        ["bash", "-c", _GIT_DIFF_SCRIPT],
        timeout=60,
        head_chars=_DIFF_HEAD_CHARS,
        tail_chars=_DIFF_TAIL_CHARS,
    )
    if result.exit_code != 0:
        raise ToolError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout or "(no changes)"


SEARCH_CODE = Tool(
    "search_code",
    "Search the repository with a ripgrep regular expression, optionally limited by glob.",
    SearchCodeInput,
    search_code,
)
RUN_COMMAND = Tool(
    "run_command",
    "Run a shell command in the sandbox. There is no network. Returns exit code and output.",
    RunCommandInput,
    run_command,
)
GIT_DIFF = Tool(
    "git_diff",
    "Show all changes against HEAD, including new untracked files.",
    GitDiffInput,
    git_diff,
)
