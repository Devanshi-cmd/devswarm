"""GitHub access: issue references, tokens, and the API calls DevSwarm needs."""

from __future__ import annotations

import base64
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from github import Auth, Github, GithubException, UnknownObjectException
from github.AuthenticatedUser import AuthenticatedUser
from github.Repository import Repository

from devswarm.config import get_secret

_ISSUE_REF = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
_ISSUE_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)"
    r"(?:[/?#].*)?$"
)
_GH_TIMEOUT_SECONDS = 15
_FORK_READY_ATTEMPTS = 30
_FORK_READY_DELAY_SECONDS = 2.0


class GitHubError(Exception):
    """A GitHub request failed, or the issue cannot be worked on."""


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.full_name}#{self.number}"


@dataclass(frozen=True)
class IssueDetails:
    title: str
    body: str
    url: str
    state: str
    is_pull_request: bool
    default_branch: str


@dataclass(frozen=True)
class RepoAccess:
    private: bool
    can_push: bool
    size_kb: int
    branch_exists: bool
    language: str | None = None


class GitHubClient(Protocol):
    def get_issue(self, ref: IssueRef) -> IssueDetails: ...
    def repo_access(self, ref: IssueRef, branch: str) -> RepoAccess: ...
    def ensure_fork(self, ref: IssueRef, default_branch: str) -> str: ...
    def branch_exists(self, full_name: str, branch: str) -> bool: ...
    def clone_url(self, ref: IssueRef) -> str: ...
    def create_pull(self, ref: IssueRef, *, title: str, body: str, head: str, base: str) -> str: ...


def parse_issue_ref(text: str) -> IssueRef:
    match = _ISSUE_REF.match(text)
    if match is None or set(match[1]) == {"."} or set(match[2]) == {"."}:
        raise GitHubError(f"expected an issue like owner/repo#123, got {text!r}")
    return IssueRef(match[1], match[2], int(match[3]))


def parse_issue_url(text: str) -> IssueRef:
    """Accept a full github.com issue link, or the short owner/repo#123 form."""
    match = _ISSUE_URL.match(text.strip())
    if match is None:
        return parse_issue_ref(text.strip())
    return parse_issue_ref(f"{match[1]}/{match[2]}#{match[3]}")


def resolve_github_token() -> str | None:
    """Use GITHUB_TOKEN if set, otherwise the token from an existing ``gh`` login."""
    token = get_secret("GITHUB_TOKEN")
    if token is not None:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def git_auth_env(token: str) -> dict[str, str]:
    """Environment that authenticates git for one command.

    Putting the token in the clone URL would save it in .git/config, which agents can
    read inside the sandbox, and on the command line it would be visible to ``ps``.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        "GIT_TERMINAL_PROMPT": "0",
    }


class PyGithubClient:
    def __init__(self, token: str) -> None:
        self._github = Github(auth=Auth.Token(token))

    def get_issue(self, ref: IssueRef) -> IssueDetails:
        try:
            repository = self._github.get_repo(ref.full_name)
            issue = repository.get_issue(ref.number)
        except GithubException as exc:
            raise GitHubError(f"could not read {ref}: {_message(exc)}") from exc
        return IssueDetails(
            title=issue.title,
            body=issue.body or "",
            url=issue.html_url,
            state=issue.state,
            is_pull_request=issue.pull_request is not None,
            default_branch=repository.default_branch,
        )

    def repo_access(self, ref: IssueRef, branch: str) -> RepoAccess:
        try:
            repository = self._github.get_repo(ref.full_name)
            try:
                repository.get_branch(branch)
                branch_exists = True
            except UnknownObjectException:
                branch_exists = False
        except GithubException as exc:
            raise GitHubError(f"could not read {ref.full_name}: {_message(exc)}") from exc
        return RepoAccess(
            private=repository.private,
            can_push=repository.permissions.push,
            size_kb=repository.size,
            branch_exists=branch_exists,
            language=repository.language,
        )

    def ensure_fork(self, ref: IssueRef, default_branch: str) -> str:
        """Fork into this account, or reuse the existing fork, and bring it up to date."""
        try:
            upstream = self._github.get_repo(ref.full_name)
            account = self._github.get_user()
            if not isinstance(account, AuthenticatedUser):
                raise GitHubError("forking needs an authenticated account")
            fork = account.create_fork(upstream)
            if not fork.fork or fork.parent.full_name.lower() != ref.full_name.lower():
                raise GitHubError(f"{fork.full_name} exists but is not a fork of {ref.full_name}")
            self._wait_until_ready(fork, default_branch)
            fork.merge_upstream(default_branch)
        except GithubException as exc:
            raise GitHubError(f"could not fork {ref.full_name}: {_message(exc)}") from exc
        return fork.full_name

    @staticmethod
    def _wait_until_ready(fork: Repository, branch: str) -> None:
        # GitHub creates forks in the background, so a new one can briefly be empty.
        for _ in range(_FORK_READY_ATTEMPTS):
            try:
                fork.get_branch(branch)
                return
            except GithubException:
                time.sleep(_FORK_READY_DELAY_SECONDS)
        raise GitHubError(f"{fork.full_name} was not ready in time")

    def branch_exists(self, full_name: str, branch: str) -> bool:
        try:
            self._github.get_repo(full_name).get_branch(branch)
        except UnknownObjectException:
            return False
        except GithubException as exc:
            raise GitHubError(f"could not read {full_name}: {_message(exc)}") from exc
        return True

    def clone_url(self, ref: IssueRef) -> str:
        return f"https://github.com/{ref.full_name}.git"

    def create_pull(self, ref: IssueRef, *, title: str, body: str, head: str, base: str) -> str:
        try:
            pull = self._github.get_repo(ref.full_name).create_pull(
                base=base, head=head, title=title, body=body
            )
        except GithubException as exc:
            raise GitHubError(f"could not open the pull request: {_message(exc)}") from exc
        return pull.html_url


def _message(exc: GithubException) -> str:
    data = exc.data if isinstance(exc.data, dict) else {}
    return f"HTTP {exc.status}: {data.get('message', 'request failed')}"
