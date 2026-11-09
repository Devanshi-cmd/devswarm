"""Write the pull request title and body from a finished run."""

from __future__ import annotations

import re

from devswarm.agents.planner import format_plan
from devswarm.github.client import IssueRef
from devswarm.orchestrator.engine import RunSummary
from devswarm.sandbox.output import clip

_MAX_TITLE_CHARS = 200
_TEST_OUTPUT_HEAD = 500
_TEST_OUTPUT_TAIL = 2_500
_MAX_BODY_HEAD = 50_000
_MAX_BODY_TAIL = 10_000  # GitHub rejects bodies over 65,536 characters.
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
_ISSUE_NUMBER = re.compile(r"#(?=[0-9])")


def pr_title(ref: IssueRef, issue_title: str, *, in_fork: bool = False) -> str:
    if in_fork:
        text = f"Fix issue {ref.number} from {ref.full_name}: {issue_title}"
        return quiet_references(text)[:_MAX_TITLE_CHARS]
    return f"Fix #{ref.number}: {issue_title}"[:_MAX_TITLE_CHARS]


def quiet_references(text: str) -> str:
    """Stop GitHub from turning text into notifications on someone else's project.

    A pull request in a fork that mentions @someone, or links an upstream issue, would
    ping those people and add a note to the issue, though nobody chose to contact them.
    """
    text = _MENTION.sub("@\u200b", text)
    text = _ISSUE_NUMBER.sub("#\u200b", text)
    return text.replace("github.com/", "github.com\u200b/")


def upstream_compare_url(ref: IssueRef, fork: str, base: str, branch: str) -> str:
    """A link that opens GitHub's own form for proposing the fix to the original project."""
    owner, repo = fork.split("/")
    return f"https://github.com/{ref.full_name}/compare/{base}...{owner}:{repo}:{branch}?expand=1"


def _fenced(text: str) -> str:
    # Longer than any run of tildes in the text, so output can't close the block early.
    longest = max((len(run) for run in re.findall(r"~+", text)), default=0)
    fence = "~" * max(4, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def render_pr_body(
    ref: IssueRef, summary: RunSummary, cost_usd: float, *, in_fork: bool = False
) -> str:
    plan = format_plan(summary.plan) if summary.plan else "(no plan recorded)"

    if summary.tests is None:
        tests = "Not run."
    else:
        verdict = "Passed" if summary.tests.passed else "Failed"
        known = len(summary.tests.known_failures)
        if known:
            verdict = f"No new failures ({known} already failing before this change)"
        output = clip(summary.tests.output.strip(), _TEST_OUTPUT_HEAD, _TEST_OUTPUT_TAIL)
        tests = (
            f"**{verdict}** (exit code {summary.tests.exit_code}), "
            f"from DevSwarm's own run of the suite.\n\n{_fenced(output)}"
        )

    if summary.review is None:
        review = "The Reviewer was turned off for this run."
    else:
        verdict = "Approved" if summary.review.verdict == "approve" else "Changes requested"
        review = f"**{verdict}.** {summary.review.summary}"

    details = (
        f"## Plan\n\n{plan}\n\n"
        f"## Changes\n\n{summary.coder_summary}\n\n"
        f"## Test results\n\n{tests}\n\n"
        f"## Reviewer verdict\n\n{review}\n\n"
        f"## Run details\n\n"
        f"- Iterations: {summary.iterations}\n"
        f"- Re-plans: {summary.replans}\n"
        f"- Total cost: ${cost_usd:.4f}\n\n"
        "---\nOpened automatically by DevSwarm.\n"
    )
    if in_fork:
        # A code span, because plain owner/repo#7 would add a note to the upstream issue.
        intro = (
            f"Proposed fix for issue {ref.number} in `{ref.full_name}`. This pull request is "
            "inside a fork, and nothing has been sent to the original project.\n\n"
        )
        return clip(intro + quiet_references(details), _MAX_BODY_HEAD, _MAX_BODY_TAIL)
    return clip(f"Closes #{ref.number}\n\n{details}", _MAX_BODY_HEAD, _MAX_BODY_TAIL)
