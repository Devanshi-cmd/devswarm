"""The dashboard's routes.

This process holds no API key or GitHub token. It reads the traces through a read-only
connection, and its only write is adding a job to the queue for the worker to pick up.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from devswarm.dashboard import views
from devswarm.github.client import GitHubError, parse_issue_url
from devswarm.jobs.queue import JobQueue
from devswarm.storage.trace import RunNotFoundError, TraceStore

_PACKAGE = Path(__file__).parent
_MAX_FORM_BYTES = 4_096
_RECENT_FIXES = 5

# Traces hold untrusted repository and model text. Escaping is the main defence; this
# policy means a mistake there still can't run injected script.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True)
class SubmissionLimits:
    """Each job spends API credits, so public submissions need firm limits."""

    max_active: int = 3
    max_per_day: int = 10


def create_app(
    db_path: Path,
    work_root: Path,
    allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost"),
    *,
    jobs_path: Path | None = None,
    access_code: str | None = None,
    limits: SubmissionLimits | None = None,
) -> FastAPI:
    app = FastAPI(title="DevSwarm", docs_url=None, redoc_url=None, openapi_url=None)
    # Rejecting unknown Host headers stops a malicious website from reading the
    # dashboard through DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=_PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=_PACKAGE / "templates")
    queue = JobQueue(jobs_path) if jobs_path else None
    submission_limits = limits or SubmissionLimits()

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    # Registered on Starlette's base class, which unknown URLs raise, so they get it too.
    @app.exception_handler(StarletteHTTPException)
    async def error_page(request: Request, error: StarletteHTTPException) -> Response:
        # Live-update partials keep a bare body; full pages get the dashboard's look.
        if request.url.path.startswith("/partials/"):
            return Response(str(error.detail), status_code=error.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": error.status_code, "message": str(error.detail).capitalize()},
            status_code=error.status_code,
        )

    @contextmanager
    def open_store() -> Iterator[TraceStore]:
        with TraceStore(db_path, read_only=True) as store:
            yield store

    def run_or_404(store: TraceStore, run_id: str) -> views.RunView:
        try:
            return views.run_view(store, run_id, work_root)
        except RunNotFoundError:
            raise HTTPException(status_code=404, detail="run not found") from None

    def history_items() -> list[views.HistoryItem]:
        if not db_path.exists():
            return []
        with open_store() as store:
            return views.history(store, work_root)

    def active_context() -> dict[str, object]:
        jobs = views.active_jobs(queue) if queue else []
        return {"jobs": jobs}

    def showcase(items: list[views.HistoryItem]) -> views.RunView | None:
        """The latest run that opened a pull request, shown beside the form."""
        item = next((i for i in items if i.pr_url and i.status.kind == "good"), None)
        if item is None:
            return None
        with open_store() as store:
            return views.run_view(store, item.run_id, work_root)

    def home_context(error: str | None = None, issue_url: str = "") -> dict[str, object]:
        items = history_items()
        return {
            "showcase": showcase(items),
            "submissions_enabled": queue is not None and bool(access_code),
            "error": error,
            "issue_url": issue_url,
            "stats": views.history_stats(items),
            "recent": items[:_RECENT_FIXES],
            **active_context(),
        }

    @app.get("/")
    def home(request: Request) -> Response:
        return templates.TemplateResponse(request, "home.html", home_context())

    @app.get("/partials/active")
    def active_partial(request: Request) -> Response:
        return templates.TemplateResponse(request, "_active_jobs.html", active_context())

    @app.post("/jobs")
    async def submit_job(request: Request) -> Response:
        body = await request.body()
        if len(body) > _MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="form too large")
        form = parse_qs(body.decode("utf-8", "replace"))
        issue_url = form.get("issue_url", [""])[0].strip()
        submitted_code = form.get("access_code", [""])[0]

        def refuse(status: int, message: str) -> Response:
            return templates.TemplateResponse(
                request, "home.html", home_context(message, issue_url), status_code=status
            )

        if queue is None or not access_code:
            return refuse(503, "Submissions are turned off on this server.")
        # compare_digest takes the same time however much of the code is right.
        if not secrets.compare_digest(submitted_code.encode(), access_code.encode()):
            return refuse(403, "That access code is not correct.")
        try:
            ref = parse_issue_url(issue_url)
        except GitHubError:
            return refuse(400, "Enter a link like https://github.com/owner/repo/issues/123.")

        existing = queue.find_active(str(ref))
        if existing is not None:
            return RedirectResponse(f"/jobs/{existing.id}", status_code=303)
        if len(queue.active()) >= submission_limits.max_active:
            return refuse(429, "The queue is full. Please try again in a few minutes.")
        if queue.count_last_day() >= submission_limits.max_per_day:
            return refuse(429, "The daily limit for this server has been reached.")
        return RedirectResponse(f"/jobs/{queue.create(str(ref)).id}", status_code=303)

    def job_context(job_id: str) -> dict[str, object]:
        job = queue.get(job_id) if queue else None
        if job is None or queue is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.run_id and db_path.exists():
            with open_store() as store:
                return {"job": views.job_view(job, queue, store, work_root)}
        return {"job": views.job_view(job, queue, None, work_root)}

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str) -> Response:
        return templates.TemplateResponse(request, "job.html", job_context(job_id))

    @app.get("/partials/jobs/{job_id}")
    def job_partial(request: Request, job_id: str) -> Response:
        return templates.TemplateResponse(request, "_job_live.html", job_context(job_id))

    @app.get("/how-it-works")
    def how_it_works_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "how_it_works.html", {})

    @app.get("/history")
    def history_page(request: Request) -> Response:
        items = history_items()
        context = {"items": items, "stats": views.history_stats(items)}
        return templates.TemplateResponse(request, "history.html", context)

    def list_context() -> dict[str, object]:
        if not db_path.exists():
            return {"runs": [], "polling": False}
        with open_store() as store:
            runs = views.run_list(store)
        return {"runs": runs, "polling": any(run.status.kind == "running" for run in runs)}

    @app.get("/runs")
    def runs_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "runs.html", list_context())

    @app.get("/partials/runs")
    def runs_partial(request: Request) -> Response:
        return templates.TemplateResponse(request, "_runs_table.html", list_context())

    def run_context(run_id: str) -> dict[str, object]:
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="run not found")
        with open_store() as store:
            return {"run": run_or_404(store, run_id)}

    @app.get("/runs/{run_id}")
    def run_page(request: Request, run_id: str) -> Response:
        return templates.TemplateResponse(request, "run.html", run_context(run_id))

    @app.get("/partials/runs/{run_id}")
    def run_partial(request: Request, run_id: str) -> Response:
        return templates.TemplateResponse(request, "_run_live.html", run_context(run_id))

    @app.get("/partials/steps/{step_id}")
    def step_partial(request: Request, step_id: int) -> Response:
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="step not found")
        with open_store() as store:
            turns = views.step_turns(store, step_id)
        return templates.TemplateResponse(request, "_step_detail.html", {"turns": turns})

    return app
