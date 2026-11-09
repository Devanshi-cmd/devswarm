"""SQLite storage for runs, steps, LLM calls and tool calls.

Everything is redacted on the way in, so no secret ever reaches the database.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from devswarm.llm.types import LLMError, LLMRequest, LLMResponse, to_jsonable
from devswarm.storage.redact import Redactor

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    task        TEXT NOT NULL,
    repo        TEXT,
    status      TEXT NOT NULL,
    config_json TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    agent       TEXT NOT NULL,
    iteration   INTEGER NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs(id),
    step_id           INTEGER NOT NULL REFERENCES steps(id),
    model             TEXT NOT NULL,
    effort            TEXT NOT NULL,
    system_prompt     TEXT NOT NULL,
    tool_names_json   TEXT NOT NULL,
    message_count     INTEGER NOT NULL,
    new_messages_json TEXT NOT NULL,
    response_json     TEXT,
    stop_reason       TEXT,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    retries_json      TEXT NOT NULL,
    error             TEXT,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    step_id     INTEGER NOT NULL REFERENCES steps(id),
    llm_call_id INTEGER NOT NULL REFERENCES llm_calls(id),
    tool_name   TEXT NOT NULL,
    input_json  TEXT NOT NULL,
    output      TEXT NOT NULL,
    is_error    INTEGER NOT NULL,
    latency_ms  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_step ON llm_calls(step_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_step ON tool_calls(step_id);
"""

_USAGE_COLUMNS = """
    COUNT(l.id) AS llm_calls,
    COALESCE(SUM(l.input_tokens), 0) AS input_tokens,
    COALESCE(SUM(l.output_tokens), 0) AS output_tokens,
    COALESCE(SUM(l.cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(l.cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(l.cost_usd), 0) AS cost_usd
"""


class RunNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TraceStore:
    def __init__(self, path: Path, redactor: Redactor | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        # WAL lets the dashboard read a run while the pipeline is still writing it.
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)
        self._redactor = redactor or Redactor()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _dumps(self, value: Any) -> str:
        return json.dumps(self._redactor.value(to_jsonable(value)))

    def _text(self, value: str | None) -> str | None:
        return None if value is None else self._redactor.text(value)

    def start_run(
        self, task: str, *, repo: str | None = None, config: dict[str, Any] | None = None
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._db:
            self._db.execute(
                "INSERT INTO runs (id, task, repo, status, config_json, created_at)"
                " VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, self._text(task), self._text(repo), self._dumps(config or {}), _now()),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, *, error: str | None = None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, self._text(error), _now(), run_id),
            )

    def start_step(self, run_id: str, agent: str, *, iteration: int = 0) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO steps (run_id, agent, iteration, status, started_at)"
                " VALUES (?, ?, ?, 'running', ?)",
                (run_id, agent, iteration, _now()),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def finish_step(self, step_id: int, status: str, *, summary: str | None = None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE steps SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
                (status, self._text(summary), _now(), step_id),
            )

    def _new_messages(self, step_id: int, request: LLMRequest) -> list[Any]:
        # Conversations are append-only, so each row stores only what is new since the
        # previous call. Storing the full list per turn would grow quadratically.
        row = self._db.execute(
            "SELECT message_count FROM llm_calls WHERE step_id = ? ORDER BY id DESC LIMIT 1",
            (step_id,),
        ).fetchone()
        return list(request.messages)[row["message_count"] if row else 0 :]

    def _run_id_for_step(self, step_id: int) -> str:
        run_id: str = self._db.execute(
            "SELECT run_id FROM steps WHERE id = ?", (step_id,)
        ).fetchone()["run_id"]
        return run_id

    def record_llm_call(self, step_id: int, request: LLMRequest, response: LLMResponse) -> int:
        usage = response.usage
        return self._insert_llm_call(
            step_id,
            request,
            {
                "response_json": self._dumps(response.content),
                "stop_reason": response.stop_reason,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_input_tokens,
                "cache_write_tokens": usage.cache_creation_input_tokens,
                "cost_usd": response.cost_usd,
                "latency_ms": response.latency_ms,
                "retries_json": self._dumps([asdict(retry) for retry in response.retries]),
            },
        )

    def record_llm_error(self, step_id: int, request: LLMRequest, error: LLMError) -> int:
        return self._insert_llm_call(
            step_id,
            request,
            {
                "error": self._text(str(error)),
                "retries_json": self._dumps([asdict(retry) for retry in error.retries]),
            },
        )

    def _insert_llm_call(self, step_id: int, request: LLMRequest, fields: dict[str, Any]) -> int:
        row: dict[str, Any] = {
            "run_id": self._run_id_for_step(step_id),
            "step_id": step_id,
            "model": request.model,
            "effort": request.effort,
            "system_prompt": self._text(request.system),
            "tool_names_json": json.dumps([tool["name"] for tool in request.tools]),
            "message_count": len(request.messages),
            "new_messages_json": self._dumps(self._new_messages(step_id, request)),
            "created_at": _now(),
            **fields,
        }
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        with self._db:
            cursor = self._db.execute(
                f"INSERT INTO llm_calls ({columns}) VALUES ({placeholders})", tuple(row.values())
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def record_tool_call(
        self,
        step_id: int,
        llm_call_id: int,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        output: str,
        is_error: bool,
        latency_ms: int,
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO tool_calls (run_id, step_id, llm_call_id, tool_name, input_json,"
                " output, is_error, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._run_id_for_step(step_id),
                    step_id,
                    llm_call_id,
                    tool_name,
                    self._dumps(tool_input),
                    self._text(output),
                    int(is_error),
                    latency_ms,
                    _now(),
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def list_runs(self) -> list[sqlite3.Row]:
        return self._db.execute(
            f"SELECT r.*, {_USAGE_COLUMNS} FROM runs r"
            " LEFT JOIN llm_calls l ON l.run_id = r.id"
            " GROUP BY r.id ORDER BY r.created_at DESC, r.rowid DESC"
        ).fetchall()

    def get_run(self, run_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self._db.execute(
            f"SELECT r.*, {_USAGE_COLUMNS} FROM runs r"
            " LEFT JOIN llm_calls l ON l.run_id = r.id"
            " WHERE r.id = ? GROUP BY r.id",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return row

    def steps(self, run_id: str) -> list[sqlite3.Row]:
        return self._db.execute(
            f"SELECT s.*, {_USAGE_COLUMNS} FROM steps s"
            " LEFT JOIN llm_calls l ON l.step_id = s.id"
            " WHERE s.run_id = ? GROUP BY s.id ORDER BY s.id",
            (run_id,),
        ).fetchall()

    def llm_calls(self, step_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM llm_calls WHERE step_id = ? ORDER BY id", (step_id,)
        ).fetchall()

    def tool_calls(self, step_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM tool_calls WHERE step_id = ? ORDER BY id", (step_id,)
        ).fetchall()

    def reconstruct_messages(self, step_id: int) -> list[Any]:
        """Rebuild the last request's full message list from the per-call deltas."""
        messages: list[Any] = []
        for call in self.llm_calls(step_id):
            messages.extend(json.loads(call["new_messages_json"]))
        return messages
