import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from anthropic.types import MessageParam
from fastapi.testclient import TestClient

from devswarm.dashboard.app import create_app
from devswarm.dashboard.charts import Datum, Series, horizontal_bars
from devswarm.dashboard.views import format_duration, format_tokens, status_view
from devswarm.llm.mock import MockLLMClient, text_turn, tool_turn
from devswarm.llm.types import LLMError, LLMRequest
from devswarm.storage.redact import Redactor
from devswarm.storage.trace import TraceStore

XSS = "<script>alert('pwned')</script>"


class Seed:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "t.sqlite3"
        self.work_root = tmp_path / "workspaces"
        self.work_root.mkdir()
        self.store = TraceStore(self.db, Redactor())

    def client(self) -> TestClient:
        return TestClient(
            create_app(self.db, self.work_root, allowed_hosts=["127.0.0.1", "testserver"])
        )

    def add_step(self, run_id: str, agent: str, turns: int = 2) -> int:
        step = self.store.start_step(run_id, agent, iteration=1)
        llm = MockLLMClient(
            [tool_turn("read_file", {"path": "a.py"}, tool_id=f"t{i}") for i in range(turns)]
        )
        messages: list[MessageParam] = [{"role": "user", "content": f"task for {agent}"}]
        for _ in range(turns):
            request = LLMRequest(
                model="claude-haiku-5-5", system=f"{agent} sys", messages=list(messages)
            )
            response = llm.create(request)
            call = self.store.record_llm_call(step, request, response)
            self.store.record_tool_call(
                step,
                call,
                tool_name="read_file",
                tool_input={"path": "a.py"},
                output="1\tx = 1",
                is_error=False,
                latency_ms=3,
            )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": "more"})
        self.store.finish_step(step, "finished")
        return step


@pytest.fixture
def seed(tmp_path: Path) -> Iterator[Seed]:
    instance = Seed(tmp_path)
    yield instance
    instance.store.close()


def finished_run(seed: Seed, task: str = "division by zero") -> str:
    run_id = seed.store.start_run(task, repo="./repo")
    seed.add_step(run_id, "planner")
    seed.add_step(run_id, "coder")
    verify = seed.store.start_step(run_id, "verify", iteration=1)
    seed.store.finish_step(verify, "passed", summary="5 passed")
    seed.store.finish_run(run_id, "done")
    return run_id


def test_empty_list_when_no_database_exists(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "missing.sqlite3", tmp_path, ["testserver"]))
    response = client.get("/")
    assert response.status_code == 200
    assert "No runs yet" in response.text
    assert not (tmp_path / "missing.sqlite3").exists()


def test_run_list_shows_runs(seed: Seed) -> None:
    run_id = finished_run(seed)
    page = seed.client().get("/").text
    assert run_id in page
    assert "division by zero" in page
    assert "Done" in page


def test_list_polls_only_while_something_runs(seed: Seed) -> None:
    run_id = seed.store.start_run("still going")
    client = seed.client()
    assert 'hx-trigger="every 3s"' in client.get("/").text
    seed.store.finish_run(run_id, "done")
    assert "hx-trigger" not in client.get("/partials/runs").text


def test_run_detail_has_every_section(seed: Seed) -> None:
    run_id = finished_run(seed)
    (seed.work_root / run_id).mkdir()
    (seed.work_root / run_id / "changes.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    page = seed.client().get(f"/runs/{run_id}").text
    for expected in (
        "Cost per agent",
        "Tokens per agent",
        "Timeline",
        "Final diff",
        "View as table",
    ):
        assert expected in page
    assert re.search(r'class="gi">\+new', page)
    assert re.search(r'class="gd">-old', page)
    assert "<svg" in page


def test_tiles_report_tests_and_counts(seed: Seed) -> None:
    run_id = finished_run(seed)
    page = seed.client().get(f"/runs/{run_id}").text
    assert "Passed" in page
    assert "Re-plans" in page
    assert ">4<" in page  # LLM calls across both agents.


def test_chart_table_holds_exact_values(seed: Seed) -> None:
    run_id = finished_run(seed)
    usage = seed.store.usage_by_agent(run_id)
    assert [row["agent"] for row in usage] == ["planner", "coder"]
    page = seed.client().get(f"/runs/{run_id}").text
    assert f"{usage[0]['output_tokens']:,}" in page


def test_unknown_run_is_404(seed: Seed) -> None:
    client = seed.client()
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/partials/runs/nope").status_code == 404


@pytest.mark.parametrize("run_id", ["..%2F..%2Fetc", "%2e%2e", "x' OR '1'='1"])
def test_hostile_run_ids_find_nothing(seed: Seed, run_id: str) -> None:
    assert seed.client().get(f"/runs/{run_id}").status_code == 404


def test_step_detail_shows_prompts_and_tools(seed: Seed) -> None:
    run_id = seed.store.start_run("t")
    step = seed.add_step(run_id, "coder")
    page = seed.client().get(f"/partials/steps/{step}").text
    for expected in ("Turn 1", "Turn 2", "coder sys", "task for coder", "read_file", "x = 1"):
        assert expected in page
    assert "mock reasoning" in page
    assert page.count("System prompt") == 1


def test_step_with_failed_call_shows_the_error(seed: Seed) -> None:
    run_id = seed.store.start_run("t")
    step = seed.store.start_step(run_id, "coder")
    request = LLMRequest(model="claude-haiku-5-5", system="s", messages=[])
    seed.store.record_llm_error(step, request, LLMError("HTTP 400: bad request"))
    assert "HTTP 400: bad request" in seed.client().get(f"/partials/steps/{step}").text


def test_verify_output_is_shown_without_expanding(seed: Seed) -> None:
    run_id = finished_run(seed)
    assert "5 passed" in seed.client().get(f"/runs/{run_id}").text


def test_run_page_polls_only_while_running(seed: Seed) -> None:
    run_id = seed.store.start_run("t")
    client = seed.client()
    assert 'hx-trigger="every 3s"' in client.get(f"/runs/{run_id}").text
    seed.store.finish_run(run_id, "done")
    assert 'hx-trigger="every 3s"' not in client.get(f"/runs/{run_id}").text


def test_finished_steps_are_preserved_across_polls(seed: Seed) -> None:
    run_id = seed.store.start_run("t")
    seed.add_step(run_id, "coder")
    assert 'hx-preserve="true"' in seed.client().get(f"/runs/{run_id}").text


def test_step_detail_loads_lazily(seed: Seed) -> None:
    run_id = seed.store.start_run("t")
    step = seed.add_step(run_id, "coder")
    page = seed.client().get(f"/runs/{run_id}").text
    assert f'hx-get="/partials/steps/{step}"' in page
    assert 'hx-trigger="toggle once"' in page
    assert "x = 1" not in page


def test_failure_report_and_pr_link(seed: Seed) -> None:
    run_id = seed.store.start_run("t")
    seed.store.finish_run(run_id, "failed", error="ran out of money")
    (seed.work_root / run_id).mkdir()
    (seed.work_root / run_id / "failure_report.md").write_text("# what happened")
    (seed.work_root / run_id / "pr_url.txt").write_text("https://github.com/o/r/pull/1\n")
    page = seed.client().get(f"/runs/{run_id}").text
    assert "ran out of money" in page
    assert "# what happened" in page
    assert 'href="https://github.com/o/r/pull/1"' in page


def test_untrusted_text_is_escaped_everywhere(seed: Seed) -> None:
    run_id = seed.store.start_run(XSS, repo=XSS)
    step = seed.store.start_step(run_id, f"agent{XSS}", iteration=1)
    llm = MockLLMClient([text_turn(XSS)])
    request = LLMRequest(
        model="claude-haiku-5-5", system=XSS, messages=[{"role": "user", "content": XSS}]
    )
    call = seed.store.record_llm_call(step, request, llm.create(request))
    seed.store.record_tool_call(
        step, call, tool_name=XSS, tool_input={"x": XSS}, output=XSS, is_error=True, latency_ms=1
    )
    seed.store.finish_step(step, "finished", summary=XSS)
    seed.store.finish_run(run_id, "failed", error=XSS)
    (seed.work_root / run_id).mkdir()
    (seed.work_root / run_id / "changes.diff").write_text(f"+{XSS}\n")
    (seed.work_root / run_id / "failure_report.md").write_text(XSS)

    client = seed.client()
    for url in ("/", f"/runs/{run_id}", f"/partials/steps/{step}"):
        assert "<script>alert" not in client.get(url).text


def test_security_headers_are_set(seed: Seed) -> None:
    headers = seed.client().get("/").headers
    policy = headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert headers["x-content-type-options"] == "nosniff"


def test_page_has_no_inline_styles_or_scripts(seed: Seed) -> None:
    run_id = finished_run(seed)
    page = seed.client().get(f"/runs/{run_id}").text
    assert "style=" not in page
    assert not re.search(r"<script(?![^>]*\bsrc=)", page)


def test_unknown_host_is_rejected(seed: Seed) -> None:
    response = seed.client().get("/", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_static_assets_are_served_locally(seed: Seed) -> None:
    client = seed.client()
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/dashboard.css").status_code == 200


def test_read_only_connection_cannot_write(seed: Seed) -> None:
    with (
        TraceStore(seed.db, read_only=True) as reader,
        pytest.raises(Exception, match="readonly"),
    ):
        reader.start_run("nope")


SERIES = [Series("a", "A", "series-1"), Series("b", "B", "series-2")]


def _numbers(path: str) -> list[float]:
    return [float(value) for value in re.findall(r"-?\d+\.?\d*", path)]


def test_no_data_means_no_chart() -> None:
    assert horizontal_bars("T", SERIES, [Datum("x", {"a": 0, "b": 0})], str) is None
    assert horizontal_bars("T", SERIES, [], str) is None


def test_largest_row_fills_the_plot_and_others_scale() -> None:
    chart = horizontal_bars(
        "T", SERIES[:1], [Datum("big", {"a": 100}), Datum("small", {"a": 25})], str
    )
    assert chart is not None
    big, small = chart.rows
    assert big.value_x - chart.baseline_x - 8 == pytest.approx(
        4 * (small.value_x - chart.baseline_x - 8)
    )


def test_rows_never_overlap() -> None:
    chart = horizontal_bars("T", SERIES, [Datum(str(i), {"a": 1, "b": 1}) for i in range(5)], str)
    assert chart is not None
    assert [row.top for row in chart.rows] == [0, 36, 72, 108, 144]
    assert chart.height == 180


def test_stacked_segments_have_a_two_pixel_gap() -> None:
    chart = horizontal_bars("T", SERIES, [Datum("x", {"a": 50, "b": 50})], str)
    assert chart is not None
    first, second = chart.rows[0].segments
    first_right = _numbers(first.path)[2]
    second_left = _numbers(second.path)[0]
    assert second_left - first_right == pytest.approx(2)


def test_only_the_last_segment_is_rounded() -> None:
    chart = horizontal_bars("T", SERIES, [Datum("x", {"a": 50, "b": 50})], str)
    assert chart is not None
    first, second = chart.rows[0].segments
    assert "Q" not in first.path
    assert "Q" in second.path


def test_bars_stay_inside_the_chart() -> None:
    chart = horizontal_bars("T", SERIES, [Datum("x", {"a": 123456, "b": 9})], str)
    assert chart is not None
    assert max(_numbers(chart.rows[0].segments[0].path)) <= chart.width
    assert chart.rows[0].value_x < chart.width


def test_invisible_slivers_are_skipped_but_the_total_counts_them() -> None:
    chart = horizontal_bars("T", SERIES, [Datum("x", {"a": 1_000_000, "b": 1})], str)
    assert chart is not None
    assert len(chart.rows[0].segments) == 1
    assert chart.rows[0].value == "1000001"


def test_single_series_has_no_legend_but_stacks_do() -> None:
    single = horizontal_bars("T", SERIES[:1], [Datum("x", {"a": 1})], str)
    double = horizontal_bars("T", SERIES, [Datum("x", {"a": 1})], str)
    assert single is not None and single.legend == ()
    assert double is not None and len(double.legend) == 2


def test_colors_follow_the_series_not_the_position() -> None:
    chart = horizontal_bars("T", SERIES, [Datum("x", {"b": 5})], str)
    assert chart is not None
    assert chart.rows[0].segments[0].css_class == "series-2"


def test_tooltips_name_agent_series_and_value() -> None:
    chart = horizontal_bars("T", SERIES, [Datum("coder", {"a": 7})], lambda v: f"{v:.0f} tok")
    assert chart is not None
    assert chart.rows[0].segments[0].tooltip == "coder, A: 7 tok"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (9_999, "9,999"), (12_900, "12.9K"), (2_500_000, "2.5M")],
)
def test_format_tokens(value: int, expected: str) -> None:
    assert format_tokens(value) == expected


def test_format_duration() -> None:
    start = "2026-01-01T00:00:00+00:00"
    assert format_duration(start, "2026-01-01T00:00:12.5+00:00") == "12.5s"
    assert format_duration(start, "2026-01-01T00:02:05+00:00") == "2m 05s"
    assert format_duration(start, None) == "-"


@pytest.mark.parametrize(
    ("status", "kind"),
    [("done", "good"), ("failed", "critical"), ("running", "running"), ("max_turns", "serious")],
)
def test_status_always_has_an_icon_and_a_label(status: str, kind: str) -> None:
    view = status_view(status)
    assert view.kind == kind and view.icon and view.label


def test_usage_ignores_steps_without_llm_calls(seed: Seed) -> None:
    run_id = finished_run(seed)
    assert "verify" not in [row["agent"] for row in seed.store.usage_by_agent(run_id)]
