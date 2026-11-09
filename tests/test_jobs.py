"""Submitting issues from the dashboard, through to a pull request."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from fakes import FakeSandbox, result
from fastapi.testclient import TestClient
from git import Repo
from test_github import REF, TOKEN, FakeGitHub, fixing_script, remote  # noqa: F401

from devswarm.dashboard.app import SubmissionLimits, create_app
from devswarm.github.client import GitHubError, IssueRef, RepoAccess, parse_issue_url
from devswarm.jobs.queue import JobQueue
from devswarm.jobs.worker import Worker, refusal
from devswarm.llm.mock import MockLLMClient
from devswarm.pipeline import RunOptions
from devswarm.storage.redact import Redactor
from devswarm.storage.trace import TraceStore

CODE = "correct-horse-battery"
URL = "https://github.com/octo/calc/issues/7"
FORM = {"content-type": "application/x-www-form-urlencoded"}


@dataclass
class Env:
    root: Path
    db: Path
    jobs: Path
    work: Path

    def client(self, code: str | None = CODE, limits: SubmissionLimits | None = None) -> TestClient:
        app = create_app(
            self.db,
            self.work,
            allowed_hosts=["testserver"],
            jobs_path=self.jobs,
            access_code=code,
            limits=limits,
        )
        return TestClient(app, follow_redirects=False)

    @property
    def queue(self) -> JobQueue:
        return JobQueue(self.jobs)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    work = tmp_path / "workspaces"
    work.mkdir()
    return Env(tmp_path, tmp_path / "trace.sqlite3", tmp_path / "jobs.sqlite3", work)


def submit(client: TestClient, url: str = URL, code: str = CODE) -> Any:
    return client.post(
        "/jobs", content=urlencode({"issue_url": url, "access_code": code}), headers=FORM
    )


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/octo/calc/issues/7",
        "http://github.com/octo/calc/issues/7/",
        "https://www.github.com/octo/calc/issues/7#issuecomment-1",
        "octo/calc#7",
    ],
)
def test_issue_links_are_understood(text: str) -> None:
    assert parse_issue_url(text) == IssueRef("octo", "calc", 7)


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/octo/calc/pull/7",
        "https://evil.example/octo/calc/issues/7",
        "https://github.com.evil.example/octo/calc/issues/7",
        "https://github.com/octo/calc/issues/0",
        "javascript:alert(1)",
        "",
    ],
)
def test_other_links_are_refused(text: str) -> None:
    with pytest.raises(GitHubError):
        parse_issue_url(text)


def test_home_shows_the_form(env: Env) -> None:
    page = env.client().get("/").text
    assert 'action="/jobs"' in page
    assert 'name="access_code"' in page


def test_form_is_hidden_without_an_access_code(env: Env) -> None:
    assert 'action="/jobs"' not in env.client(code=None).get("/").text


def test_good_submission_creates_a_job_and_redirects(env: Env) -> None:
    response = submit(env.client())
    assert response.status_code == 303
    job = env.queue.active()[0]
    assert response.headers["location"] == f"/jobs/{job.id}"
    assert job.issue_ref == "octo/calc#7"
    assert job.status == "queued"


def test_wrong_access_code_creates_nothing(env: Env) -> None:
    response = submit(env.client(), code="guess")
    assert response.status_code == 403
    assert "not correct" in response.text
    assert env.queue.active() == []


def test_missing_access_code_creates_nothing(env: Env) -> None:
    response = env.client().post("/jobs", content=urlencode({"issue_url": URL}), headers=FORM)
    assert response.status_code == 403
    assert env.queue.active() == []


def test_submissions_refused_when_no_code_is_configured(env: Env) -> None:
    assert submit(env.client(code=None), code="").status_code == 503
    assert env.queue.active() == []


def test_empty_code_never_matches_an_unset_code(env: Env) -> None:
    assert submit(env.client(code=""), code="").status_code == 503


def test_bad_link_is_refused_and_kept_in_the_form(env: Env) -> None:
    response = submit(env.client(), url="https://example.com/x")
    assert response.status_code == 400
    assert 'value="https://example.com/x"' in response.text
    assert env.queue.active() == []


def test_same_issue_twice_goes_to_the_existing_job(env: Env) -> None:
    client = env.client()
    first = submit(client)
    second = submit(client)
    assert second.headers["location"] == first.headers["location"]
    assert len(env.queue.active()) == 1


def test_queue_limit(env: Env) -> None:
    client = env.client(limits=SubmissionLimits(max_active=1))
    submit(client)
    response = submit(client, url="https://github.com/octo/calc/issues/8")
    assert response.status_code == 429
    assert "queue is full" in response.text


def test_daily_limit_counts_finished_jobs_too(env: Env) -> None:
    client = env.client(limits=SubmissionLimits(max_per_day=1))
    submit(client)
    job = env.queue.active()[0]
    env.queue.finish(job.id, "done")
    response = submit(client, url="https://github.com/octo/calc/issues/8")
    assert response.status_code == 429
    assert "daily limit" in response.text


def test_oversized_form_is_rejected(env: Env) -> None:
    response = env.client().post("/jobs", content="x" * 5000, headers=FORM)
    assert response.status_code == 413


def test_get_cannot_submit(env: Env) -> None:
    assert env.client().get("/jobs").status_code == 405


def test_csp_allows_only_same_site_forms(env: Env) -> None:
    assert "form-action 'self'" in env.client().get("/").headers["content-security-policy"]


def test_unknown_job_is_404(env: Env) -> None:
    client = env.client()
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/partials/jobs/nope").status_code == 404


def test_queued_job_page_polls_and_shows_position(env: Env) -> None:
    client = env.client()
    location = submit(client).headers["location"]
    page = client.get(location).text
    assert 'hx-trigger="every 2s"' in page
    assert "Queued" in page
    assert "next in line" in page.lower()


def test_failed_job_shows_why_and_stops_polling(env: Env) -> None:
    client = env.client()
    location = submit(client).headers["location"]
    env.queue.finish(env.queue.active()[0].id, "failed", error="no write access")
    page = client.get(location).text
    assert "no write access" in page
    assert "was not fixed" in page
    assert 'hx-trigger="every 2s"' not in page


def test_active_jobs_appear_on_the_home_page(env: Env) -> None:
    client = env.client()
    submit(client)
    assert "octo/calc#7" in client.get("/").text
    assert "octo/calc#7" in client.get("/partials/active").text


def test_queue_order_and_claiming(env: Env) -> None:
    queue = env.queue
    first, second = queue.create("a/b#1"), queue.create("a/b#2")
    assert queue.position(second) == 2
    claimed = queue.claim_next()
    assert claimed is not None and claimed.id == first.id
    assert claimed.status == "running"
    next_claim = queue.claim_next()
    assert next_claim is not None and next_claim.id == second.id
    assert queue.claim_next() is None


def test_interrupted_jobs_are_marked_failed(env: Env) -> None:
    queue = env.queue
    job = queue.create("a/b#1")
    queued = queue.create("a/b#2")
    queue.claim_next()
    assert queue.fail_interrupted() == 1
    refreshed = queue.get(job.id)
    assert refreshed is not None and refreshed.status == "failed"
    still = queue.get(queued.id)
    assert still is not None and still.status == "queued"


class WorkerGitHub(FakeGitHub):
    def __init__(self, url: str, access: RepoAccess | None = None, **issue: Any) -> None:
        super().__init__(url, **issue)
        self.access = access or RepoAccess(False, True, 100, False)

    def repo_access(self, ref: IssueRef, branch: str) -> RepoAccess:
        return self.access


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        (RepoAccess(True, True, 1, False), "public"),
        (RepoAccess(False, True, 999_999_999, False), "too large"),
        (RepoAccess(False, True, 1, False, "Haskell"), "supported"),
    ],
)
def test_refusals(access: RepoAccess, expected: str) -> None:
    reason = refusal(access, REF)
    assert reason is not None and expected in reason


@pytest.mark.parametrize(
    "language", ["Python", "Jupyter Notebook", "TypeScript", "JavaScript", "Rust", None]
)
def test_supported_languages(language: str | None) -> None:
    assert refusal(RepoAccess(False, True, 1, False, language), REF) is None


@contextmanager
def _context(sandbox: FakeSandbox) -> Iterator[FakeSandbox]:
    yield sandbox


@pytest.fixture
def store(env: Env) -> Iterator[TraceStore]:
    with TraceStore(env.db, Redactor()) as trace:
        yield trace


def make_worker(
    env: Env, store: TraceStore, github: Any, script: list[Any] | None = None, sandbox: Any = None
) -> Worker:
    box = sandbox or FakeSandbox(result(stdout="5 passed"))
    return Worker(
        queue=env.queue,
        github=github,
        token=TOKEN,
        options=RunOptions(
            llm=MockLLMClient(fixing_script() if script is None else script),
            store=store,
            work_root=env.work,
            sandbox_factory=lambda path, image: _context(box),
            image_resolver=lambda path: "image",
            test_baseline=False,
        ),
        redactor=Redactor([TOKEN]),
    )


def test_worker_turns_a_job_into_a_pull_request(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    assert make_worker(env, store, WorkerGitHub(str(remote))).run_once() is True

    finished = env.queue.get(job.id)
    assert finished is not None
    assert finished.status == "done"
    assert finished.pr_url == "https://github.com/octo/calc/pull/8"
    assert finished.run_id is not None
    assert "devswarm/issue-7" in [head.name for head in Repo(remote).heads]


def test_finished_job_page_shows_the_result_and_history_records_it(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    make_worker(env, store, WorkerGitHub(str(remote))).run_once()
    client = env.client()

    page = client.get(f"/jobs/{job.id}").text
    assert "Pull request opened" in page
    assert 'href="https://github.com/octo/calc/pull/8"' in page
    assert "Cost per agent" in page
    assert 'hx-trigger="every 2s"' not in page

    history = client.get("/history").text
    assert "octo/calc#7" in history
    assert 'href="https://github.com/octo/calc/pull/8"' in history
    assert "#8" in history
    home = client.get("/").text
    assert "Recent fixes" in home
    assert re.search(r"Pull requests opened</div><div class=\"tile-value\">1<", home)


def test_refused_repo_spends_nothing_and_never_forks(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    github = ForkingGitHub(str(remote), RepoAccess(False, False, 1, False, "Haskell"))
    make_worker(env, store, github, script=[]).run_once()
    finished = env.queue.get(job.id)
    assert finished is not None and finished.status == "failed"
    assert "Haskell" in (finished.error or "")
    assert store.list_runs() == []
    assert github.forked == []


def test_leftover_branch_on_own_repo_is_refused(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    make_worker(
        env, store, WorkerGitHub(str(remote), RepoAccess(False, True, 1, True)), script=[]
    ).run_once()
    finished = env.queue.get(job.id)
    assert finished is not None and "already exists" in (finished.error or "")


class ForkingGitHub(WorkerGitHub):
    def __init__(
        self, url: str, access: RepoAccess, *, fork_has_branch: bool = False, **issue: Any
    ) -> None:
        super().__init__(url, access, **issue)
        self.forked: list[str] = []
        self.fork_has_branch = fork_has_branch
        self.pull_targets: list[str] = []
        self.cloned: list[str] = []

    def ensure_fork(self, ref: IssueRef, default_branch: str) -> str:
        self.forked.append(default_branch)
        return "me/calc"

    def branch_exists(self, full_name: str, branch: str) -> bool:
        return self.fork_has_branch

    def clone_url(self, ref: IssueRef) -> str:
        self.cloned.append(ref.full_name)
        return self.url

    def create_pull(self, ref: IssueRef, *, title: str, body: str, head: str, base: str) -> str:
        self.pull_targets.append(ref.full_name)
        return super().create_pull(ref, title=title, body=body, head=head, base=base)


NO_ACCESS = RepoAccess(False, False, 100, False, "Python")


def test_without_write_access_the_fix_goes_to_a_fork(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    github = ForkingGitHub(str(remote), NO_ACCESS)
    make_worker(env, store, github).run_once()

    finished = env.queue.get(job.id)
    assert finished is not None and finished.status == "done", finished
    assert github.forked == ["main"]
    assert github.cloned == ["me/calc"]
    assert github.pull_targets == ["me/calc"]
    assert "devswarm/issue-7" in [head.name for head in Repo(remote).heads]


def test_fork_pull_request_never_notifies_the_original_project(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    env.queue.create(str(REF))
    github = ForkingGitHub(str(remote), NO_ACCESS, title="Crash, thanks @maintainer see #12")
    make_worker(env, store, github).run_once()

    pull = github.pulls[0]
    for text in (pull["title"], pull["body"]):
        assert "@maintainer" not in text
        assert "#12" not in text
        assert "Closes" not in text
        assert not re.search(r"octo/calc#7(?!`)", text)
    assert "nothing has been sent to the original project" in pull["body"]
    message = Repo(remote).commit("devswarm/issue-7").message
    assert "@maintainer" not in message and "#12" not in message


def test_job_page_offers_sending_the_fix_upstream(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    make_worker(env, store, ForkingGitHub(str(remote), NO_ACCESS)).run_once()
    client = env.client()
    page = client.get(f"/jobs/{job.id}").text
    assert "Fix ready in a fork" in page
    assert "Send to the original project" in page
    assert "https://github.com/octo/calc/compare/main...me:calc:devswarm/issue-7?expand=1" in page
    assert "Fork #8" in client.get("/history").text


def test_own_repo_has_no_upstream_link(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    make_worker(env, store, WorkerGitHub(str(remote))).run_once()
    page = env.client().get(f"/jobs/{job.id}").text
    assert "Pull request opened" in page
    assert "Send to the original project" not in page


def test_leftover_branch_in_the_fork_is_refused_before_spending(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    github = ForkingGitHub(str(remote), NO_ACCESS, fork_has_branch=True)
    make_worker(env, store, github, script=[]).run_once()
    finished = env.queue.get(job.id)
    assert finished is not None and "already exists in me/calc" in (finished.error or "")
    assert store.list_runs() == []


def test_closed_issue_never_causes_a_fork(
    env: Env,
    store: TraceStore,
    remote: Path,  # noqa: F811
) -> None:
    job = env.queue.create(str(REF))
    github = ForkingGitHub(str(remote), NO_ACCESS, state="closed")
    make_worker(env, store, github, script=[]).run_once()
    finished = env.queue.get(job.id)
    assert finished is not None and finished.status == "failed"
    assert github.forked == []


def test_closed_issue_fails_the_job(env: Env, store: TraceStore, remote: Path) -> None:  # noqa: F811
    job = env.queue.create(str(REF))
    make_worker(env, store, WorkerGitHub(str(remote), state="closed"), script=[]).run_once()
    finished = env.queue.get(job.id)
    assert finished is not None and finished.status == "failed"
    assert "not an open issue" in (finished.error or "")


def test_a_crashing_job_does_not_stop_the_worker(env: Env, store: TraceStore, remote: Path) -> None:  # noqa: F811
    job = env.queue.create(str(REF))
    worker = make_worker(env, store, WorkerGitHub(str(remote)), script=[])
    assert worker.run_once() is True
    finished = env.queue.get(job.id)
    assert finished is not None and finished.status == "failed"
    assert "internal error" in (finished.error or "")


def test_token_is_scrubbed_from_job_errors(env: Env, store: TraceStore, remote: Path) -> None:  # noqa: F811
    class Leaky(WorkerGitHub):
        def repo_access(self, ref: IssueRef, branch: str) -> RepoAccess:
            raise GitHubError(f"bad credentials {TOKEN}")

    job = env.queue.create(str(REF))
    make_worker(env, store, Leaky(str(remote)), script=[]).run_once()
    finished = env.queue.get(job.id)
    assert finished is not None and TOKEN not in (finished.error or "")


def test_empty_queue_returns_false(env: Env, store: TraceStore, remote: Path) -> None:  # noqa: F811
    assert make_worker(env, store, WorkerGitHub(str(remote))).run_once() is False


def _seed_running_run(env: Env, store: TraceStore) -> str:
    run_id = store.start_run("Crash on zero\n\nDetails", repo="octo/calc#7")
    step = store.start_step(run_id, "planner", iteration=1)
    store.finish_step(step, "finished")
    from devswarm.llm.types import LLMRequest

    step = store.start_step(run_id, "coder", iteration=1)
    request = LLMRequest(
        model="claude-haiku-5-5", system="s", messages=[{"role": "user", "content": "x"}]
    )
    response = MockLLMClient([fixing_script()[1]]).create(request)
    call = store.record_llm_call(step, request, response)
    store.record_tool_call(
        step,
        call,
        tool_name="edit_file",
        tool_input={"path": "<b>calc.py</b>", "old_str": "a", "new_str": "b"},
        output="edited",
        is_error=False,
        latency_ms=2,
    )
    return run_id


def test_live_view_shows_stage_and_activity(env: Env, store: TraceStore) -> None:
    job = env.queue.create(str(REF))
    env.queue.claim_next()
    env.queue.set_run(job.id, _seed_running_run(env, store))
    page = env.client().get(f"/partials/jobs/{job.id}").text
    assert 'stage-done">' in page
    assert 'stage-active"' in page
    assert "Live activity" in page
    assert "edit_file" in page
    assert "&lt;b&gt;calc.py&lt;/b&gt;" in page
    assert "<b>calc.py" not in page
    assert 'hx-trigger="every 2s"' in page


def test_history_leaves_out_local_and_eval_runs(env: Env, store: TraceStore) -> None:
    run = store.start_run("local task", repo="./some/path")
    store.finish_run(run, "done")
    assert "local task" not in env.client().get("/history").text
    assert "local task" in env.client().get("/runs").text
