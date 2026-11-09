"""Run queued jobs, one at a time. This process holds the API key and GitHub token."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from git.exc import GitCommandError

from devswarm.github.client import GitHubClient, GitHubError, IssueRef, parse_issue_ref
from devswarm.github.local import WorkspaceError
from devswarm.jobs.queue import Job, JobQueue
from devswarm.orchestrator.state_machine import State
from devswarm.pipeline import RunOptions, run_issue
from devswarm.sandbox.base import SandboxError
from devswarm.storage.redact import Redactor

logger = logging.getLogger(__name__)

_POLL_SECONDS = 2.0
_MAX_REPO_KB = 200_000


def preflight(github: GitHubClient, ref: IssueRef) -> str | None:
    """Return why this issue can't be worked on, before any money is spent."""
    access = github.repo_access(ref, f"devswarm/issue-{ref.number}")
    if access.private:
        # Traces are shown publicly, so a private repo's code would be exposed.
        return "only public repositories are supported, because run details are public"
    if not access.can_push:
        return f"this account has no write access to {ref.full_name}"
    if access.size_kb > _MAX_REPO_KB:
        return f"{ref.full_name} is too large for this service"
    if access.branch_exists:
        return f"branch devswarm/issue-{ref.number} already exists; delete it to run this again"
    return None


class Worker:
    def __init__(
        self,
        *,
        queue: JobQueue,
        github: GitHubClient,
        token: str,
        options: RunOptions,
        redactor: Redactor,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._queue = queue
        self._github = github
        self._token = token
        self._options = options
        self._redactor = redactor
        self._sleep = sleep

    def run_forever(self) -> None:
        interrupted = self._queue.fail_interrupted()
        if interrupted:
            logger.warning("marked %d interrupted job(s) as failed", interrupted)
        while True:
            if not self.run_once():
                self._sleep(_POLL_SECONDS)

    def run_once(self) -> bool:
        """Process one job. Returns False when the queue is empty."""
        job = self._queue.claim_next()
        if job is None:
            return False
        try:
            self._process(job)
        except (GitHubError, SandboxError, WorkspaceError, GitCommandError) as exc:
            self._fail(job, str(exc))
        except Exception as exc:
            # One bad job must not take down the worker for everyone queued behind it.
            logger.exception("job %s crashed", job.id)
            self._fail(job, f"internal error: {type(exc).__name__}")
        except BaseException:
            self._fail(job, "the worker was stopped during this job")
            raise
        return True

    def _fail(self, job: Job, reason: str) -> None:
        self._queue.finish(job.id, "failed", error=self._redactor.text(reason))

    def _process(self, job: Job) -> None:
        ref = parse_issue_ref(job.issue_ref)
        reason = preflight(self._github, ref)
        if reason is not None:
            self._fail(job, reason)
            return

        outcome = run_issue(
            ref=ref,
            github=self._github,
            token=self._token,
            dry_run=False,
            options=self._options,
            redactor=self._redactor,
            on_run_started=lambda run_id: self._queue.set_run(job.id, run_id),
        )
        if outcome.result.status is State.DONE and outcome.pr_url:
            self._queue.finish(job.id, "done", pr_url=outcome.pr_url)
        else:
            self._fail(job, outcome.result.reason or "the run did not finish")
