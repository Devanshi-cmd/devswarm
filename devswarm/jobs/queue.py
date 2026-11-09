"""A small job queue in its own SQLite file.

It is separate from the trace database so the web process can write here while its
connection to the traces stays read-only.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    issue_ref   TEXT NOT NULL,
    status      TEXT NOT NULL,
    run_id      TEXT,
    pr_url      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""

_ACTIVE = ("queued", "running")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Job:
    id: str
    issue_ref: str
    status: str
    run_id: str | None
    pr_url: str | None
    error: str | None
    created_at: str
    finished_at: str | None

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE


def _job(row: sqlite3.Row) -> Job:
    return Job(**dict(row))


class JobQueue:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # One connection per operation, because the web server handles requests on threads.
        db = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def create(self, issue_ref: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs (id, issue_ref, status, created_at) VALUES (?, ?, 'queued', ?)",
                (job_id, issue_ref, _now()),
            )
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> Job | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def active(self) -> list[Job]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running')"
                " ORDER BY created_at, rowid"
            ).fetchall()
        return [_job(row) for row in rows]

    def find_active(self, issue_ref: str) -> Job | None:
        return next((job for job in self.active() if job.issue_ref == issue_ref), None)

    def position(self, job: Job) -> int:
        """How many jobs must start or finish before this one, plus one."""
        ids = [item.id for item in self.active()]
        return ids.index(job.id) + 1 if job.id in ids else 0

    def count_last_day(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with self._connect() as db:
            count: int = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE created_at >= ?", (cutoff,)
            ).fetchone()[0]
        return count

    def claim_next(self) -> Job | None:
        with self._connect() as db:
            # IMMEDIATE takes the write lock first, so two workers can't claim the same job.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (row["id"],))
            db.execute("COMMIT")
        return self.get(row["id"])

    def set_run(self, job_id: str, run_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET run_id = ? WHERE id = ?", (run_id, job_id))

    def finish(
        self, job_id: str, status: str, *, pr_url: str | None = None, error: str | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = ?, pr_url = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, pr_url, error, _now(), job_id),
            )

    def fail_interrupted(self) -> int:
        """Mark jobs left running by a worker that died, so they don't show as live forever."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ?"
                " WHERE status = 'running'",
                ("the worker restarted during this job", _now()),
            )
        return cursor.rowcount
