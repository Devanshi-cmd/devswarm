# DevSwarm

An autonomous multi-agent software engineer: give it a GitHub issue and it produces a tested fix as a pull request.

Work in progress. The full README arrives with the final milestone.

```bash
uv sync
uv run devswarm smoke
uv run pytest && uv run ruff check && uv run mypy devswarm
```
