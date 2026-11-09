#!/usr/bin/env bash
# Run DevSwarm on a GitHub issue, then start the dashboard. See docs/DEMO.md.
# Usage: scripts/demo.sh owner/repo#123 [--dry-run]
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: scripts/demo.sh owner/repo#123 [--dry-run]" >&2
  exit 2
fi

issue="$1"
shift

uv run devswarm run --issue "$issue" "$@"
echo
echo "Starting the dashboard. Press Ctrl+C to stop."
uv run devswarm dashboard
