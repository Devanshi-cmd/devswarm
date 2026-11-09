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


def pr_title(ref: IssueRef, issue_title: str) -> str:
    return f"Fix #{ref.number}: {issue_title}"[:_MAX_TITLE_CHARS]


def _fenced(text: str) -> str:
    # Longer than any run of tildes in the text, so output can't close the block early.
    longest = max((len(run) for run in re.findall(r"~+", text)), default=0)
    fence = "~" * max(4, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def render_pr_body(ref: IssueRef, summary: RunSummary, cost_usd: float) -> str:
    plan = format_plan(summary.plan) if summary.plan else "(no plan recorded)"

    if summary.tests is None:
        tests = "Not run."
    else:
        verdict = "Passed" if summary.tests.passed else "Failed"
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

    body = (
        f"Closes #{ref.number}\n\n"
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
    return clip(body, _MAX_BODY_HEAD, _MAX_BODY_TAIL)
