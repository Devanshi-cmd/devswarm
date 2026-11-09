# Recording the demo

Everything here uses real API calls. A full run costs about half a cent.

## Before you record

1. Open a new terminal, so your `docker` group membership applies.
2. Run `uv run devswarm sandbox-check --repo .` and confirm every line says `pass`.
3. Pick the repository for the video. DevSwarm needs write access to it, so copy a small open-source project with a real bug into your own account, and open an issue there describing the bug.
4. Use a fresh issue. A second run on the same issue fails if `devswarm/issue-N` has diverged, so delete that branch before retrying.

## The 90-second script

| Time | Do this | Say this |
|---|---|---|
| 0:00 | Show the issue on GitHub | "This is a real bug report. Nobody is going to touch the code." |
| 0:10 | `scripts/demo.sh owner/repo#1` | "One command: four agents, and everything they run stays in a locked-down container." |
| 0:20 | Let PLAN and CODE scroll | "The Planner reads the code. The Coder fixes it. Its commands have no network and no secrets." |
| 0:45 | Let TEST and REVIEW scroll | "The Tester writes new tests. DevSwarm runs the suite itself, so the model can't just claim it passed." |
| 1:00 | Open the pull request link | "Here's the PR: the plan, the tests, the review verdict and the total cost." |
| 1:15 | Open the dashboard run page | "And every prompt, tool call and cent is recorded." |

To rehearse without opening a PR, use `scripts/demo.sh owner/repo#1 --dry-run`.

## Making a GIF

Record the terminal with [asciinema](https://asciinema.org), then convert it with [agg](https://github.com/asciinema/agg):

```bash
asciinema rec demo.cast -c "scripts/demo.sh owner/repo#1"
agg --cols 110 --rows 32 --speed 2 demo.cast docs/images/demo.gif
```

Check the recording before sharing it. DevSwarm hides secrets in its own output, but it can't see the rest of your screen.
