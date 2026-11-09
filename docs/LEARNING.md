# Learning notes

Each decision records what we chose, why, what the alternatives were, and what would break if we did it differently.

## M1: Skeleton

### A `Protocol` between the agents and the LLM

- **What:** Agents depend on `LLMClient`, an interface with one method: `create(request) -> response`. `AnthropicLLMClient` and `MockLLMClient` both satisfy it.
- **Why:** The whole test suite can run offline and free, and tests can script exact model behaviour such as a refusal.
- **Alternatives:** Patching the `anthropic` module in each test, or spending real money in tests.
- **What would break:** With patches, tests are tied to how the SDK is called internally, so a harmless refactor breaks them. With real calls, CI costs money and fails at random.

### Testing the real client through a fake HTTP transport

- **What:** `test_llm_client.py` builds a real SDK client over `httpx2.MockTransport`, so only the network is fake.
- **Why:** It checks what actually gets sent, such as the `cache_control` and `thinking` fields, and how real SDK exceptions are raised for 429 and 529.
- **Alternatives:** A fake SDK object.
- **What would break:** A fake SDK would accept any arguments, so a malformed request would pass the tests and fail against the API.
- **Limit:** This proves the request has the shape we intended. Only `devswarm smoke --live` proves the API accepts it.

### Our own retry loop, with SDK retries off

- **What:** `max_retries=0` on the SDK. Our loop retries 429, 5xx and connection errors, uses exponential backoff with full jitter, and honours `Retry-After`.
- **Why:** Every retry appears in the trace, and you can read the whole algorithm in about 30 lines.
- **Alternatives:** The SDK's built-in retries (default 2).
- **What would break:** With both on, retries multiply (6 × 3 attempts), and those inside the SDK are invisible to the trace. Without jitter, callers rate-limited at the same moment all retry at the same moment and collide again.
- **Not retried:** 400, 401 and 404. The same request will fail the same way, so retrying only wastes time.

### Prompt caching at two levels

- **What:** An explicit breakpoint on the system prompt, plus top-level `cache_control`, which moves forward as the conversation grows.
- **Why:** Each turn resends the whole conversation. Cached text costs 0.1× the input price to re-read, and 1.25× once to write.
- **Alternatives:** Caching only system and tools, which leaves the growing history at full price; or managing message-level markers by hand.
- **What would break:** Anything that changes the prefix, like a timestamp in the system prompt, silently removes the saving. No error appears, only `cache_read_input_tokens` staying at 0, which is why `devswarm smoke` checks it.

### Append-only conversations

- **What:** A message is never edited or removed once sent. Thinking blocks go back to the API unchanged. Long tool output must be cut down when it arrives.
- **Why:** The API returns a 400 if thinking blocks are replayed after earlier history changed. Editing also invalidates the cache from that point on.
- **Alternatives:** Pruning old tool results to save context.
- **What would break:** Pruning can fail the request outright. `MockLLMClient` returns the same 400, so a bug like this shows up in offline tests.

### The trace stores message deltas

- **What:** Each `llm_calls` row stores only the messages that are new since the previous call. `reconstruct_messages` rebuilds the full list.
- **Why:** The full list grows every turn, so storing it per call is quadratic. A 30-turn agent would store the first message 30 times.
- **Alternatives:** Store every full prompt, which is simple but bloats the database quickly.
- **What would break:** This depends on append-only history. If history could be edited, concatenating the deltas would give the wrong conversation.

### Redact on the way into storage

- **What:** `TraceStore` passes all text through `Redactor` before writing it. A logging filter does the same on the log handler.
- **Why:** Agents read repos and issues, which may contain secrets. The database and logs may later be shared or shown in a demo video.
- **Alternatives:** Redact when displaying, which leaves the secret in the database file.
- **What would break:** Any new display path, or anyone opening the file directly, would expose the secret. The test reads the raw database bytes for this reason.

### Cost accounting

- **What:** A per-model `PRICE_TABLE`. An unknown model raises an error, and a request over a model's long-context threshold is priced entirely at the higher rate.
- **Why:** The `$1.00` budget is only as accurate as this table.
- **Alternatives:** Return a cost of 0 for unknown models, or assume cache reads are always 0.1× input.
- **What would break:** A zero cost would let a run ignore its budget. The 0.1× assumption is wrong for Opus 5.5, where reads are 0.05×.
- **Assumption:** We count cached and uncached input toward the 100K threshold. That is the cautious reading, because the docs we had only say "prompt length".

## M2: Tools and sandbox

### Calling the docker CLI, not a Python Docker library

- **What:** `subprocess` runs `docker run` and `docker exec` with argument lists, never a shell string on the host.
- **Why:** Every isolation flag sits in one readable list, and timeouts are simple.
- **Alternatives:** The `docker` Python package, whose `exec_run` has no timeout.
- **What would break:** A hung command would hang the whole run.

### A timeout kills the whole container

- **What:** When a command times out, the container is removed and replaced. The repo is a mount, so no work is lost.
- **Why:** Killing the `docker exec` client leaves the command running inside, along with anything it started in the background.
- **Alternatives:** Running `timeout` inside the container, which misses background children.
- **What would break:** A leftover process could keep changing files during later steps.

### `.git` is mounted read-only

- **What:** The agent can read `.git` from inside the container but not change it. `git_diff` uses a private index and object directory in `/tmp`.
- **Why:** Host git later commits this repo in M5. A hook or config planted in `.git` would then run on the host, outside the sandbox.
- **Alternatives:** Trusting the agent, or scanning `.git` before committing.
- **What would break:** Any command in the sandbox could turn into code execution on the host.

### git and ripgrep run inside the sandbox

- **What:** `search_code` and `git_diff` go through `sandbox.exec`, even though they look harmless.
- **Why:** A repo's git config can name programs for git to run, so `git diff` on an untrusted repo is not read-only.
- **Alternatives:** Running them on the host, which is simpler and faster.
- **What would break:** Rule 1, no command ever runs on the host.

### Opening files one path component at a time

- **What:** File tools run on the host, so they check the path and then open each component with `O_NOFOLLOW` relative to the previous directory. Files must be regular files, and `O_NONBLOCK` stops a FIFO from hanging.
- **Why:** The agent can change the folder from inside the container, so a path that was safe when checked can become a symlink before it is opened.
- **Alternatives:** `Path.resolve()` and a prefix check alone.
- **What would break:** That leaves a time-of-check to time-of-use race, and an escaping symlink could reach any file on the host.

### Output keeps the head and tail, with bounded memory

- **What:** Each stream keeps its first 2,000 and last 8,000 bytes and reports how many were dropped.
- **Why:** The error is usually at the end of a log, and the command's setup at the start.
- **Alternatives:** Keeping only the start, or capturing everything and cutting afterwards.
- **What would break:** A head-only cut hides the failure. Capturing everything lets `yes` exhaust host memory.

### Only named dependencies are installed, in a separate build phase

- **What:** The image build is the only step with network. It installs requirements that are plain package names, and never copies the repo in.
- **Why:** Lines such as `--index-url`, `git+https://` or `-r ../x` could fetch or run something unexpected.
- **Alternatives:** `pip install .` in the build, which runs the repo's own build code with network on.
- **What would break:** Untrusted repo code would run while the network is open.
- **Limit:** Named packages can still run their own install scripts during the build, and that phase has network. No secrets or repo files are present.

## M3: Single agent

### The loop checks the stop reason before touching the content

- **What:** `refusal` and `max_tokens` return before any tool runs.
- **Why:** A response cut off by `max_tokens` can hold an incomplete tool call.
- **Alternatives:** Running whatever `tool_use` blocks appear.
- **What would break:** A half-written `edit_file` could damage a file.

### `finish` is a tool with `tool_choice: auto`, plus a nudge

- **What:** The agent is told to call `finish`. If it stops without doing so, it is reminded at most twice.
- **Why:** Forced `tool_choice` returns a 400 on Sonnet 5.5 and Opus 5.5, and skips thinking on Haiku.
- **Alternatives:** Forcing the tool, or parsing JSON from the final text.
- **What would break:** Swapping in a stronger Reviewer would fail on every request.

### DevSwarm runs the tests itself

- **What:** After the Coder finishes, the suite runs in the sandbox and the exit code decides the outcome.
- **Why:** A model saying "all tests pass" is not evidence.
- **Alternatives:** Trusting the agent's summary.
- **What would break:** A run could report success with broken code.

### Agents work on a private copy

- **What:** A run clones or copies the repo under `.devswarm/workspaces/`. The result returns as a branch.
- **Why:** A failed or hostile run can't damage the user's checkout. A clone also carries no hooks.
- **Alternatives:** Working in place on a new branch.
- **What would break:** A run would overwrite uncommitted work, and could leave the checkout in a broken state.

### Untrusted text sits in randomly labelled blocks

- **What:** Task text and summaries go in `<untrusted_data id="random">` blocks in the user message.
- **Why:** The system prompt tells the model these blocks are data, and an unguessable id stops text from closing its own block.
- **Alternatives:** A fixed delimiter, or a random id in the system prompt.
- **What would break:** A fixed delimiter can be spoofed by the issue text. A random id in the system prompt changes the prefix, so nothing would ever be cached.
- **Limit:** This lowers the risk of prompt injection but cannot remove it. The real protection is that a compromised agent still has no network, no secrets and no way to reach the host.
