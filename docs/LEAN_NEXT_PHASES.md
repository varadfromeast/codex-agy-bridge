# Lean Next Phases Brief

Date: 2026-06-16

## What Was Achieved

### Phase 1: Lifecycle Correctness

- Non-zero Antigravity exits with partial responses now fail instead of being reported as completed.
- Explicit completion markers no longer wait for the old 150-second stability window.
- Stale-supervisor cleanup now stops the persisted tmux session instead of only marking the Run failed.
- `cancel_requested` reconciliation now resolves to `canceled`, not infrastructure failure.
- Blank workspaces are rejected before `Path("").resolve()` can fall back to the server cwd.
- Continuation conversation IDs are validated as single safe path segments before state creation.
- Baseline mypy failures were fixed.

Verification at the time:

- Non-live pytest suite passed.
- Ruff passed.
- Mypy passed.
- A small live MCP run completed successfully through the local repo server.

### Phase 2: Large Result Access, First Slice

- `agy_result` now returns compact result metadata rather than an unbounded full result.
- Completed results are persisted as `final-result.txt` in the Run directory.
- `agy_result_read(run_id, offset_bytes, max_bytes)` was added as a stateless byte-offset chunk reader.
- Concurrent reads are simple: each call opens the immutable file, seeks to its own offset, reads a bounded chunk, and closes.
- The local Codex MCP config now points `codex-agy-bridge` at this repo checkout instead of the PyPI package.

Live check:

- Started a real run through the repo MCP server.
- Result was `LIVE_OK`.
- `agy_result` returned preview and artifact metadata.
- `agy_result_read` returned the result chunk correctly.

## Remaining Phase 2 Work: Complete Large Results

Keep this phase small and boring.

1. Add result metadata sidecar:
   - `final-result.json`
   - byte count
   - SHA-256
   - created timestamp
   - source conversation ID

2. Harden `agy_result_read`:
   - explicit tests for negative offsets
   - excessive `max_bytes` cap
   - offset beyond EOF
   - missing artifact
   - failed/non-terminal runs
   - UTF-8 boundary behavior

3. Add transcript chunk reading:
   - likely `agy_transcript_read(run_id, offset_bytes=0, max_bytes=65536)`
   - read raw transcript JSONL as bytes
   - keep it stateless like `agy_result_read`

4. Optional MCP resource URIs:
   - `agy-result://{run_id}/final`
   - `agy-transcript://{run_id}/full`
   - Useful for clients that understand MCP resources, but not required for the simple chunk-reader path.

## Phase 3: Durable Scheduling

Problem: goal targets are currently started immediately or rejected when capacity is full. That does not match the desired 100-target workflow.

Needed:

- Separate admitted target count from actively running process count.
- Add durable queued target records.
- Add a dispatcher that starts queued targets when capacity frees up.
- Define fairness across goals.
- Make queued target cancellation explicit.
- Preserve restart recovery: queued targets should survive MCP/server restart.

Initial design direction:

- Keep `AGY_BRIDGE_MAX_TARGETS` for admitted work.
- Add `AGY_BRIDGE_MAX_ACTIVE` for simultaneously executing runs.
- Keep active concurrency conservative until stress tests prove higher values are safe.

## Phase 4: Performance

Problem: raising capacity exposes expensive polling and transcript reads.

Needed:

- Avoid full transcript parsing on repeated status/result calls.
- Reduce per-run `tmux has-session` subprocess polling.
- Keep janitor scans off the hot create-run path where possible.
- Add basic diagnostics for:
  - active runs
  - queued targets
  - active tmux sessions
  - process count
  - polling rate
  - transcript read volume

Stress gates:

- Test at 10, 25, 50, and 100 active/admitted runs.
- Track start latency, status latency, CPU, memory, process count, cancellation time, and orphan tmux sessions.

## Phase 5: MCP Usability

Problem: the bridge works, but agents need clearer affordances.

Needed:

- Make terminal state explicit in start/status responses:
  - persistent tmux exists
  - visible Terminal.app is not opened by default
  - use `agy_target_open_terminal`

- Add optional start-time terminal opening if desired.
- Add result previews and artifact references to goal status.
- Improve diagnostics for source checkout mismatch.
- Document permission layers clearly:
  - host Codex permissions
  - MCP bridge process permissions
  - Antigravity sandbox flag
  - Antigravity permission-skip flag

## Suggested Next Step

Finish the remaining Phase 2 hardening first:

1. Add metadata sidecar and SHA-256.
2. Add edge-case tests for `agy_result_read`.
3. Add `agy_transcript_read` using the same stateless byte-offset model.

Do not start durable scheduling until the large-result contract is stable.
