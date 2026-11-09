from pathlib import Path
from typing import Any

import pytest
from fakes import FakeSandbox, result
from pydantic import BaseModel, ConfigDict

from devswarm.tools.base import ToolContext, ToolRegistry, ToolResult
from devswarm.tools.finish import finish_definition, parse_finish
from devswarm.tools.paths import Workspace
from devswarm.tools.registry import default_registry


class Env:
    def __init__(self, root: Path, sandbox: FakeSandbox | None = None) -> None:
        self.root = root
        self.sandbox = sandbox or FakeSandbox()
        self.registry = default_registry()
        self.context = ToolContext(Workspace(root), self.sandbox)

    def call(self, name: str, **tool_input: Any) -> ToolResult:
        return self.registry.execute(self.context, name, tool_input)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("one\ntwo\nthree\ntwo\n")
    return Env(tmp_path)


def test_read_file_numbers_lines(env: Env) -> None:
    assert env.call("read_file", path="pkg/mod.py").content == "1\tone\n2\ttwo\n3\tthree\n4\ttwo"


def test_read_file_line_range_says_there_is_more(env: Env) -> None:
    output = env.call("read_file", path="pkg/mod.py", start_line=2, end_line=3).content
    assert output.startswith("2\ttwo\n3\tthree")
    assert "file has 4 lines" in output


def test_read_file_caps_huge_output(env: Env) -> None:
    (env.root / "long.txt").write_text("x" * 100 + "\n" + ("y" * 1000 + "\n") * 900)
    output = env.call("read_file", path="long.txt").content
    assert len(output) < 41_000
    assert "output cut off" in output


def test_read_file_rejects_binary(env: Env) -> None:
    (env.root / "b.bin").write_bytes(b"\xff\xfe\x00")
    outcome = env.call("read_file", path="b.bin")
    assert outcome.is_error and "UTF-8" in outcome.content


def test_read_file_traversal_is_an_error_result(env: Env) -> None:
    assert env.call("read_file", path="../../etc/passwd").is_error


def test_list_dir_respects_depth_and_skips_noise(env: Env) -> None:
    (env.root / ".git").mkdir()
    (env.root / "pkg" / "sub").mkdir()
    (env.root / "pkg" / "sub" / "deep.py").write_text("")
    output = env.call("list_dir", path=".", depth=2).content.splitlines()
    assert "pkg/" in output and "pkg/sub/" in output and "pkg/mod.py" in output
    assert "pkg/sub/deep.py" not in output
    assert ".git/" not in output


def test_list_dir_marks_but_does_not_follow_symlinks(env: Env, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").write_text("x")
    (env.root / "link").symlink_to(outside)
    output = env.call("list_dir", depth=3).content
    assert "link@" in output
    assert "secret" not in output


def test_list_dir_outside_workspace_is_an_error(env: Env) -> None:
    assert env.call("list_dir", path="..").is_error


def test_edit_file_replaces_unique_match(env: Env) -> None:
    assert not env.call("edit_file", path="pkg/mod.py", old_str="three", new_str="3").is_error
    assert (env.root / "pkg/mod.py").read_text() == "one\ntwo\n3\ntwo\n"


def test_edit_file_refuses_non_unique_match(env: Env) -> None:
    outcome = env.call("edit_file", path="pkg/mod.py", old_str="two", new_str="2")
    assert outcome.is_error and "2 times" in outcome.content
    assert (env.root / "pkg/mod.py").read_text() == "one\ntwo\nthree\ntwo\n"


@pytest.mark.parametrize(("old", "new"), [("missing", "x"), ("", "x"), ("one", "one")])
def test_edit_file_refuses_bad_input(env: Env, old: str, new: str) -> None:
    assert env.call("edit_file", path="pkg/mod.py", old_str=old, new_str=new).is_error


def test_edit_file_preserves_crlf(env: Env) -> None:
    (env.root / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    env.call("edit_file", path="crlf.txt", old_str="a", new_str="z")
    assert (env.root / "crlf.txt").read_bytes() == b"z\r\nb\r\n"


def test_create_file_refuses_to_overwrite(env: Env) -> None:
    outcome = env.call("create_file", path="pkg/mod.py", content="gone")
    assert outcome.is_error
    assert (env.root / "pkg/mod.py").read_text().startswith("one")


def test_create_file_in_git_directory_is_refused(env: Env) -> None:
    (env.root / ".git").mkdir()
    assert env.call("create_file", path=".git/hooks/pre-commit", content="evil").is_error
    assert not (env.root / ".git/hooks").exists()


def make_env(tmp_path: Path, *results: Any) -> Env:
    return Env(tmp_path, FakeSandbox(*results))


def test_search_code_goes_through_sandbox(tmp_path: Path) -> None:
    env = make_env(tmp_path, result(stdout="a.py:1:hit\n"))
    assert env.call("search_code", pattern="hit", glob="*.py").content == "a.py:1:hit\n"
    call = env.sandbox.calls[0]
    assert call[0] == "rg"
    assert call[-3:] == ["-e", "hit", "."]
    assert call[call.index("--glob") + 1] == "*.py"


def test_search_pattern_starting_with_dash_is_not_a_flag(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.call("search_code", pattern="--files")
    assert env.sandbox.calls[0][-3:] == ["-e", "--files", "."]


def test_search_no_matches_is_not_an_error(tmp_path: Path) -> None:
    outcome = make_env(tmp_path, result(exit_code=1)).call("search_code", pattern="x")
    assert (outcome.content, outcome.is_error) == ("no matches", False)


def test_search_bad_regex_is_an_error(tmp_path: Path) -> None:
    env = make_env(tmp_path, result(exit_code=2, stderr="regex parse error"))
    outcome = env.call("search_code", pattern="(")
    assert outcome.is_error and "regex parse error" in outcome.content


def test_run_command_reports_all_streams(tmp_path: Path) -> None:
    env = make_env(tmp_path, result(exit_code=3, stdout="out", stderr="err"))
    outcome = env.call("run_command", cmd="pytest", timeout=30)
    assert not outcome.is_error  # A failing test run is information, not a tool failure.
    assert "exit_code: 3" in outcome.content
    assert "out" in outcome.content and "err" in outcome.content
    assert env.sandbox.calls[0] == ["bash", "-c", "pytest"]
    assert env.sandbox.timeouts == [30]


def test_run_command_timeout_is_an_error(tmp_path: Path) -> None:
    env = make_env(tmp_path, result(exit_code=-1, stdout="partial", timed_out=True))
    outcome = env.call("run_command", cmd="sleep 999", timeout=1)
    assert outcome.is_error and "timed out" in outcome.content and "partial" in outcome.content


def test_run_command_timeout_above_limit_is_rejected(tmp_path: Path) -> None:
    assert make_env(tmp_path).call("run_command", cmd="x", timeout=100_000).is_error


def test_git_diff_goes_through_sandbox_with_external_programs_off(tmp_path: Path) -> None:
    env = make_env(tmp_path, result(stdout="diff --git a b\n"))
    assert env.call("git_diff").content == "diff --git a b\n"
    script = env.sandbox.calls[0][2]
    assert "--no-ext-diff" in script and "--no-textconv" in script
    assert env.sandbox.head_chars[0] > 2_000


def test_git_diff_empty(tmp_path: Path) -> None:
    assert make_env(tmp_path).call("git_diff").content == "(no changes)"


def test_git_diff_failure_is_an_error(tmp_path: Path) -> None:
    env = make_env(tmp_path, result(exit_code=128, stderr="not a git repository"))
    assert env.call("git_diff").is_error


def test_unknown_tool_is_an_error_result(env: Env) -> None:
    assert env.call("delete_everything").is_error


def test_invalid_input_names_the_field(env: Env) -> None:
    outcome = env.call("read_file", start_line=1)
    assert outcome.is_error and "path" in outcome.content


def test_extra_input_fields_are_rejected(env: Env) -> None:
    assert env.call("read_file", path="pkg/mod.py", sudo=True).is_error


def test_read_only_allowlist_blocks_write_tools(env: Env) -> None:
    reviewer: ToolRegistry = env.registry.only(["read_file", "list_dir", "search_code", "git_diff"])
    outcome = reviewer.execute(
        env.context, "edit_file", {"path": "x", "old_str": "a", "new_str": "b"}
    )
    assert outcome.is_error and "not available" in outcome.content
    assert reviewer.execute(env.context, "run_command", {"cmd": "ls"}).is_error
    assert env.sandbox.calls == []
    assert {tool["name"] for tool in reviewer.definitions()} == {
        "read_file",
        "list_dir",
        "search_code",
        "git_diff",
    }


def test_unknown_allowlist_entry_raises(env: Env) -> None:
    with pytest.raises(KeyError):
        env.registry.only(["no_such_tool"])


def test_definitions_are_deterministic_and_complete() -> None:
    definitions = default_registry().definitions()
    assert [tool["name"] for tool in definitions] == sorted(tool["name"] for tool in definitions)
    assert definitions == default_registry().definitions()
    assert len(definitions) == 7
    assert all(tool["input_schema"]["type"] == "object" for tool in definitions)


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str]


class LooseOutput(BaseModel):
    steps: list[str]


def test_finish_definition_is_strict() -> None:
    definition = finish_definition(PlanOutput)
    assert definition["name"] == "finish"
    assert definition["strict"] is True
    assert definition["input_schema"]["additionalProperties"] is False


def test_finish_definition_refuses_loose_models() -> None:
    with pytest.raises(ValueError, match="extra='forbid'"):
        finish_definition(LooseOutput)


def test_parse_finish_success() -> None:
    assert parse_finish(PlanOutput, {"steps": ["a"]}) == PlanOutput(steps=["a"])


def test_parse_finish_failure_is_a_correctable_message() -> None:
    outcome = parse_finish(PlanOutput, {"steps": "not a list"})
    assert isinstance(outcome, str) and "steps" in outcome
