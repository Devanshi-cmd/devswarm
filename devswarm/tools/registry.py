"""The full set of tools. Each agent receives a subset through ``ToolRegistry.only``."""

from __future__ import annotations

from devswarm.tools.base import ToolRegistry
from devswarm.tools.commands import GIT_DIFF, RUN_COMMAND, SEARCH_CODE
from devswarm.tools.files import CREATE_FILE, EDIT_FILE, LIST_DIR, READ_FILE


def default_registry() -> ToolRegistry:
    return ToolRegistry(
        [READ_FILE, LIST_DIR, SEARCH_CODE, EDIT_FILE, CREATE_FILE, RUN_COMMAND, GIT_DIFF]
    )
