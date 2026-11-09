from pathlib import Path

import pytest
from typer.testing import CliRunner

from devswarm.cli import app
from devswarm.llm.mock import MockLLMClient, text_turn
from devswarm.llm.types import LLMError
from devswarm.smoke import run_smoke
from devswarm.storage.trace import TraceStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Running from an empty folder keeps the developer's real .env out of the tests.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEVSWARM_DB", str(tmp_path / "trace.sqlite3"))


def test_smoke_reports_cache_hit_and_traces(tmp_path: Path) -> None:
    client = MockLLMClient([text_turn("one"), text_turn("two")])
    with TraceStore(tmp_path / "s.sqlite3") as store:
        result = run_smoke(client, store, "claude-haiku-5-5")
        assert store.get_run(result.run_id)["llm_calls"] == 2
    assert result.cache_hit
    assert result.turns[0].usage.cache_read_input_tokens == 0


def test_smoke_records_failure(tmp_path: Path) -> None:
    client = MockLLMClient([LLMError("HTTP 401: bad key", status_code=401)])
    with TraceStore(tmp_path / "s.sqlite3") as store:
        with pytest.raises(LLMError):
            run_smoke(client, store, "claude-haiku-5-5")
        run = store.list_runs()[0]
    assert run["status"] == "failed"
    assert "bad key" in run["error"]


def test_cli_smoke_then_runs_then_report() -> None:
    smoke = runner.invoke(app, ["smoke"])
    assert smoke.exit_code == 0, smoke.output
    assert "Cache hit confirmed" in smoke.output

    with TraceStore(Path("trace.sqlite3")) as store:
        run_id = store.list_runs()[0]["id"]
    assert run_id in runner.invoke(app, ["runs"]).output

    report = runner.invoke(app, ["report", run_id])
    assert report.exit_code == 0
    assert "smoke" in report.output


def test_cli_live_smoke_needs_a_key() -> None:
    result = runner.invoke(app, ["smoke", "--live"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_cli_report_unknown_run() -> None:
    assert runner.invoke(app, ["report", "nope"]).exit_code == 1


def test_cli_run_shows_resolved_config() -> None:
    result = runner.invoke(
        app, ["run", "--issue", "a/b#1", "--model", "reviewer=claude-sonnet-5-5"]
    )
    assert "claude-sonnet-5-5" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert result.exit_code == 1


def test_cli_run_rejects_a_bad_issue_reference() -> None:
    result = runner.invoke(app, ["run", "--issue", "not-an-issue"])
    assert result.exit_code == 1
    assert "owner/repo#123" in result.output


def test_cli_run_needs_a_key() -> None:
    result = runner.invoke(app, ["run", "--repo", ".", "--task", "x"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--repo", "."],
        ["run", "--issue", "a/b#1", "--repo", ".", "--task", "x"],
        ["run", "--repo", ".", "--task", "x", "--model", "boss=claude-haiku-5-5"],
    ],
)
def test_cli_run_rejects_bad_options(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "not built yet" not in result.output


def test_cli_eval_rejects_unknown_config() -> None:
    result = runner.invoke(app, ["eval", "--config", "sonnet-api"])
    assert result.exit_code == 1
    assert "unknown configuration" in result.output


def test_cli_eval_needs_a_key_for_devswarm_configs() -> None:
    tasks = Path(__file__).parent.parent / "evals" / "tasks"
    result = runner.invoke(app, ["eval", "--tasks-dir", str(tasks)])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
