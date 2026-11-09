"""The dashboard's routes. It only reads the trace database, and never writes to it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from devswarm.dashboard import views
from devswarm.storage.trace import RunNotFoundError, TraceStore

_PACKAGE = Path(__file__).parent

# Traces hold untrusted repository and model text. Escaping is the main defence; this
# policy means a mistake there still can't run injected script.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_app(
    db_path: Path,
    work_root: Path,
    allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost"),
) -> FastAPI:
    app = FastAPI(title="DevSwarm", docs_url=None, redoc_url=None, openapi_url=None)
    # Rejecting unknown Host headers stops a malicious website from reading the
    # dashboard through DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=_PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=_PACKAGE / "templates")

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @contextmanager
    def open_store() -> Iterator[TraceStore]:
        with TraceStore(db_path, read_only=True) as store:
            yield store

    def run_or_404(store: TraceStore, run_id: str) -> views.RunView:
        try:
            return views.run_view(store, run_id, work_root)
        except RunNotFoundError:
            raise HTTPException(status_code=404, detail="run not found") from None

    def list_context() -> dict[str, object]:
        if not db_path.exists():
            return {"runs": [], "polling": False}
        with open_store() as store:
            runs = views.run_list(store)
        return {"runs": runs, "polling": any(run.status.kind == "running" for run in runs)}

    @app.get("/")
    def index(request: Request) -> Response:
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
