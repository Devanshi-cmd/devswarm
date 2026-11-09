"""Run queued jobs, one at a time. This process holds the API key and GitHub token."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from git.exc import GitCommandError

from devswarm.github.client import (
    GitHubClient,
    GitHubError,
    IssueRef,
    RepoAccess,
    parse_issue_ref,
)
from devswarm.github.local import WorkspaceError
from devswarm.jobs.queue import Job, JobQueue
from devswarm.orchestrator.state_machine import State
from devswarm.pipeline import RunOptions, run_issue
from devswarm.sandbox.base import SandboxError
from devswarm.sandbox.images import SUPPORTED_GITHUB_LANGUAGES, SUPPORTED_LANGUAGE_NAMES
from devswarm.storage.redact import Redactor

logger = logging.getLogger(__name__)

_POLL_SECONDS = 2.0
_MAX_REPO_KB = 200_000


def refusal(access: RepoAccess, ref: IssueRef) -> str | None:
    """Return why this repository can't be worked on, before anything is forked or spent."""
    if access.private:
        # Traces are shown publicly, so a private repo's code would be exposed.
        return "only public repositories are supported, because run details are public"
    if access.size_kb > _MAX_REPO_KB:
        return f"{ref.full_name} is too large for this service"
    # GitHub reports None for very small repositories, which the sandbox can still detect.
    if access.language is not None and access.language not in SUPPORTED_GITHUB_LANGUAGES:
        return f"{ref.full_name} is {access.language}; supported: {SUPPORTED_LANGUAGE_NAMES}"
    return None


def choose_fork(github: GitHubClient, ref: IssueRef, default_branch: str) -> str | None:
    """Work out where the fix goes. Returns a fork's name, or None to use the repo itself.

    Raises GitHubError when the issue can't be worked on.
    """
    branch = f"devswarm/issue-{ref.number}"
    access = github.repo_access(ref, branch)
    reason = refusal(access, ref)
    if reason is not None:
        raise GitHubError(reason)
    leftover = f"branch {branch} already exists in %s; delete it to run this again"
    if access.can_push:
        if access.branch_exists:
            raise GitHubError(leftover % ref.full_name)
        return None
    fork = github.ensure_fork(ref, default_branch)
    if github.branch_exists(fork, branch):
        raise GitHubError(leftover % fork)
    return fork


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
        issue = self._github.get_issue(ref)
        # Checked here as well as in run_issue, so a closed issue never causes a fork.
        if issue.is_pull_request or issue.state != "open":
            raise GitHubError(f"{ref} is not an open issue")
        fork = choose_fork(self._github, ref, issue.default_branch)

        outcome = run_issue(
            ref=ref,
            github=self._github,
            token=self._token,
            dry_run=False,
            options=self._options,
            redactor=self._redactor,
            on_run_started=lambda run_id: self._queue.set_run(job.id, run_id),
            fork=fork,
        )
        if outcome.result.status is State.DONE and outcome.pr_url:
            self._queue.finish(job.id, "done", pr_url=outcome.pr_url)
        else:
            self._fail(job, outcome.result.reason or "the run did not finish")
