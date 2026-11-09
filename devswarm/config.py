"""Run configuration and secret loading."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, get_args

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from devswarm.llm.pricing import PRICE_TABLE
from devswarm.llm.types import Effort

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_DB_PATH = Path(".devswarm/devswarm.sqlite3")

AgentName = Literal["planner", "coder", "tester", "reviewer"]
AGENT_NAMES: tuple[AgentName, ...] = get_args(AgentName)

# .env ships with values like "your-github-token-here". Treating those as unset
# means a missing key fails clearly instead of reaching the API as a bad credential.
_PLACEHOLDER_PREFIX = "your-"


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str = DEFAULT_MODEL
    effort: Effort = "medium"
    max_tokens: int = 16_000
    max_turns: int = 30


class DevSwarmConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    planner: AgentConfig = AgentConfig(max_turns=15)
    coder: AgentConfig = AgentConfig(max_turns=30)
    tester: AgentConfig = AgentConfig(max_turns=20)
    reviewer: AgentConfig = AgentConfig(max_turns=10)

    def for_agent(self, name: AgentName) -> AgentConfig:
        config: AgentConfig = getattr(self, name)
        return config

    def with_overrides(
        self, models: dict[AgentName, str], efforts: dict[AgentName, Effort]
    ) -> DevSwarmConfig:
        changes: dict[str, AgentConfig] = {}
        for name in AGENT_NAMES:
            update: dict[str, str] = {}
            if name in models:
                if models[name] not in PRICE_TABLE:
                    # Without a price the run's dollar budget could not be enforced.
                    raise ValueError(f"no price table entry for model {models[name]!r}")
                update["model"] = models[name]
            if name in efforts:
                update["effort"] = efforts[name]
            if update:
                changes[name] = self.for_agent(name).model_copy(update=update)
        return self.model_copy(update=changes)


def parse_agent_overrides(pairs: Iterable[str]) -> dict[AgentName, str]:
    """Turn CLI values like ``reviewer=claude-sonnet-5-5`` into a dict keyed by agent."""
    overrides: dict[AgentName, str] = {}
    for pair in pairs:
        agent, separator, value = pair.partition("=")
        matched = next((name for name in AGENT_NAMES if name == agent), None)
        if not separator or not value or matched is None:
            raise ValueError(f"expected <agent>=<value> with agent in {AGENT_NAMES}, got {pair!r}")
        overrides[matched] = value
    return overrides


def parse_effort_overrides(pairs: Iterable[str]) -> dict[AgentName, Effort]:
    efforts: dict[AgentName, Effort] = {}
    for agent, value in parse_agent_overrides(pairs).items():
        matched = next((level for level in get_args(Effort) if level == value), None)
        if matched is None:
            raise ValueError(f"unknown effort {value!r}; choose from {get_args(Effort)}")
        efforts[agent] = matched
    return efforts


def load_env(path: Path = Path(".env")) -> None:
    """Load ``.env`` without replacing variables already set in the shell."""
    load_dotenv(path, override=False)


def is_placeholder(value: str) -> bool:
    return not value or value.startswith(_PLACEHOLDER_PREFIX)


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "")
    return None if is_placeholder(value) else value


def db_path() -> Path:
    return Path(os.environ.get("DEVSWARM_DB", str(DEFAULT_DB_PATH)))
