"""Render a stored run as a Markdown summary."""

from __future__ import annotations

from devswarm.storage.trace import TraceStore


def render_markdown(store: TraceStore, run_id: str) -> str:
    run = store.get_run(run_id)
    lines = [
        f"# DevSwarm run `{run['id']}`",
        "",
        f"- **Task:** {run['task']}",
        f"- **Status:** {run['status']}",
        f"- **Started:** {run['created_at']}",
        f"- **Finished:** {run['finished_at'] or 'not finished'}",
        f"- **LLM calls:** {run['llm_calls']}",
        f"- **Total cost:** ${run['cost_usd']:.6f}",
    ]
    if run["repo"]:
        lines.insert(3, f"- **Repo:** {run['repo']}")
    if run["error"]:
        lines.append(f"- **Error:** {run['error']}")

    lines += [
        "",
        "## Steps",
        "",
        "| Agent | Iteration | Status | LLM calls | Input | Output"
        " | Cache read | Cache write | Cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for step in store.steps(run_id):
        lines.append(
            f"| {step['agent']} | {step['iteration']} | {step['status']} | {step['llm_calls']}"
            f" | {step['input_tokens']} | {step['output_tokens']} | {step['cache_read_tokens']}"
            f" | {step['cache_write_tokens']} | ${step['cost_usd']:.6f} |"
        )
    return "\n".join(lines) + "\n"
