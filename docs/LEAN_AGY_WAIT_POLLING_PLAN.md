# Lean Agy Wait Polling Plan

Date: 2026-06-16

## Implementation Status

V1 is implemented for durable sparse per-run events, `agy_wait`, action and
terminal lifecycle emission, and `agy_start` notification metadata. MCP resource
subscription and push-style `notifications/resources/updated` remain a later
optimization; `agy_wait` is the reliable wake mechanism for current MCP hosts.

## Goal

Stop Codex from repeatedly polling `agy_status`, `agy_goal_status`, and
`agy_transcript` just to discover that work finished or needs attention.

The bridge should emit sparse, durable Run notifications and expose a blocking
MCP wait primitive as the compatibility path for clients that do not wake the
model on MCP notifications:

```text
Codex starts one or more Runs
Codex is told which Run notification resource to watch
or Codex calls agy_wait(...)
MCP blocks until useful Run state changes when direct notifications are not enough
MCP returns compact notification/status records
Codex analyzes and decides continue/done/cancel
```

This solves model-turn churn first. It does not require replacing filesystem
state, tmux, or the existing runner process model.

## Current Problem

There are two kinds of polling today:

1. Codex-to-MCP polling
   - Codex repeatedly calls `agy_status`, `agy_goal_status`, or
     `agy_transcript`.
   - This burns turns and tokens when many Runs are active.

2. Bridge-internal polling
   - `RunSupervisor` loops while the tmux session is alive.
   - It checks cancel files, transcript changes, completion markers, and tmux
     liveness, then sleeps.

The first problem is more important. Even if the server internally waits with a
small fallback sleep, Codex should either receive an MCP notification or make one
blocking wait call instead of many status calls.

## Design Principle

Use durable filesystem notification events as the contract.

Optional OS file watching can be added later, but it must not be the source of
truth.

```text
runner/supervisor writes durable event
event append bumps tiny notify marker
MCP resource notification or wait tool watches markers
Codex wakes only when useful work happened
```

Notifications must be sparse and semantically meaningful. Do not emit one
notification per transcript step by default. Transcript records are data for
inspection and indexing; notification events are control-plane signals.

## Notification Delivery Options

MCP has a resource subscription pattern: a server can expose resources and send
`notifications/resources/updated` when subscribed resource content changes.
Progress notifications also exist for long-running requests. In practice,
client support and model wakeup behavior vary by host.

Use a layered approach:

1. Durable file-backed notification events are the source of truth.
2. MCP resources expose the latest notification stream where supported.
3. `agy_wait` blocks on the same files for clients that cannot reliably wake on
   resource-update notifications.
4. Direct filesystem watching by Codex is a fallback/debug path, not the
   primary contract.

The `agy_start` docstring should tell Codex about the notification resource and
the wait tool:

```text
This Run emits sparse lifecycle notifications. Watch
agy-run://{run_id}/notifications when your client supports MCP resource
subscriptions, or call agy_wait(run_ids=[...]) to block until a terminal or
attention event.
```

`agy_start` cannot itself guarantee an unsolicited model turn. It can return the
resource URI and document the expected workflow. The MCP host decides whether
resource notifications wake the agent automatically.

## Storage Contract

Add per-run files:

```text
runs/<run_id>/
  state.json
  session-events.jsonl
  notify.seq
```

`session-events.jsonl` is append-only and durable:

```json
{"event_id":"000000000001","run_id":"...","kind":"run_started","created_at":"..."}
{"event_id":"000000000002","run_id":"...","kind":"needs_attention","reason":"provider_auth"}
{"event_id":"000000000003","run_id":"...","kind":"run_completed","status":"completed"}
```

`notify.seq` contains the latest event id. It is atomically rewritten after each
event append. The wait path can check this tiny file instead of reading
`state.json` or parsing transcripts.

## New Module: `session_events.py`

Responsibilities:

- allocate monotonic event ids per Run
- append JSONL events under a file lock
- atomically update `notify.seq`
- read events after an event id
- return latest event id
- tolerate missing event files for old Runs

Suggested interface:

```python
append_event(run_dir: Path, kind: str, payload: dict) -> dict
latest_event_id(run_dir: Path) -> str | None
read_events(
    run_dir: Path,
    after_event_id: str | None = None,
    limit: int = 100,
) -> list[dict]
```

Implementation notes:

- Use the Run directory as the event boundary.
- Use `FileLock(run_dir / "session-events.lock")`.
- Write each JSONL event in one append.
- Rewrite `notify.seq` with `atomic_write_json` or an equivalent atomic text
  write.
- Event ids can be zero-padded decimal strings.
- Do not require SQLite for this slice.

## Notification Event Kinds

Emit at least:

```text
run_started
mcp_input
terminal_opened
cancel_requested
needs_attention
result_ready
run_completed
run_failed
run_canceled
provider_health_changed
```

Later attribution events:

```text
mcp_input_observed
human_input
unknown_input
```

Do not emit `transcript_step` as a default notification. It is too noisy for the
control plane. If transcript indexing later wants per-step ingestion, keep it in
the transcript index, not the notification stream. If a transcript record needs
Codex action, emit a higher-level event such as:

```text
needs_attention
result_ready
blocked
acceptance_check_required
human_intervention_observed
```

## Event Emission Points

From `RunSupervisor`:

- `_launch()` -> `run_started`
- `_observe_conversation()` -> only sparse derived events such as
  `needs_attention`, `provider_health_changed`, or `result_ready`
- `_finish()` -> `run_completed`, `run_failed`, or `run_canceled`

From `RunnerOrchestrator`:

- `open_terminal()` -> `terminal_opened`
- `send_text()` -> `mcp_input`
- `cancel()` -> `cancel_requested`

Keep event payloads compact. Do not write full transcript content into every
event. Store identifiers and small summaries; raw transcript remains separate.

## MCP Resources

Expose notification resources so clients that support subscriptions can receive
resource-update notifications.

Suggested resource URI:

```text
agy-run://{run_id}/notifications
```

Resource content should be compact:

```json
{
  "run_id": "...",
  "latest_event_id": "000000000003",
  "latest_terminal_event": {
    "kind": "run_completed",
    "status": "completed"
  },
  "unread_attention": []
}
```

Optional resource:

```text
agy-goal://{goal_id}/notifications
```

This can aggregate latest terminal/attention events for all target Runs in a
goal.

Do not require Codex to watch raw files directly. Raw files remain inspectable
for debugging, but MCP resources and tools should be the official interface.

## New Module: `waiter.py`

Responsibilities:

- wait for events from one or more Runs
- support terminal-state waits
- support timeout without raising by default
- read compact status only when returning
- avoid full transcript parsing in the wait loop

Suggested interface:

```python
wait_for_runs(
    run_dirs: dict[str, Path],
    condition: str,
    after_event_id: str | None,
    timeout_seconds: int,
) -> dict
```

Conditions:

```text
any_event
any_attention
any_terminal
all_terminal
```

Semantics:

- `any_event`: return when any selected Run has an event newer than
  `after_event_id`.
- `any_attention`: return when any selected Run emits an event requiring Codex
  action, such as `needs_attention`, `result_ready`, `run_failed`, or
  `run_completed`.
- `any_terminal`: return when any selected Run emits `run_completed`,
  `run_failed`, or `run_canceled`.
- `all_terminal`: return when every selected Run is terminal.

## MCP Tool: `agy_wait`

Add:

```python
agy_wait(
    run_ids: list[str],
    condition: "any_event" | "any_attention" | "any_terminal" | "all_terminal" = "any_attention",
    after_event_id: str | None = None,
    timeout_seconds: int = 900,
) -> dict
```

Return shape:

```json
{
  "condition": "any_terminal",
  "matched": true,
  "events": [
    {
      "event_id": "000000000003",
      "run_id": "run-1",
      "kind": "run_completed",
      "status": "completed"
    }
  ],
  "runs": {
    "run-1": {
      "status": "completed",
      "latest_event_id": "000000000003"
    },
    "run-2": {
      "status": "running",
      "latest_event_id": "000000000001"
    }
  }
}
```

Timeout return:

```json
{
  "condition": "any_terminal",
  "matched": false,
  "events": [],
  "runs": {
    "run-1": {"status": "running", "latest_event_id": "000000000001"}
  }
}
```

Timeout should not be an error unless invalid arguments were supplied.

Why keep `agy_wait` if MCP notifications/resources exist?

- Many MCP clients expose tools reliably but do not reliably wake the model on
  resource-update notifications.
- A blocking tool is easier for Codex to reason about today.
- It gives one call per batch of Runs instead of repeated status polling.
- It can be removed or de-emphasized later if host support for resource
  subscriptions becomes strong enough.

## Wait Backend Decision

Do not make a Rust-backed watcher mandatory in v1.

V1 backend:

- simple server-side wait loop
- read only `notify.seq` mtimes/contents
- bounded backoff
- no extra dependency

Suggested backoff:

```text
0.1s for the first second
0.5s while active
1.0s max fallback interval
```

Why this is enough for v1:

- It removes Codex/model polling immediately.
- It avoids packaging and optional dependency complexity.
- The server checks tiny marker files, not transcripts.
- Durable filesystem events are the real contract.

V2 optional backend:

- Add `watchfiles` behind an adapter.
- Treat it as a wakeup optimization only.
- Keep the same `session-events.jsonl` and `notify.seq` contract.

Suggested adapter:

```python
class WaitBackend(Protocol):
    def wait_changed(self, paths: list[Path], timeout: float) -> bool: ...

class PollingWaitBackend:
    ...

class WatchfilesWaitBackend:
    ...
```

Optional dependency shape:

```text
codex-agy-bridge[watch]
```

Fallback:

```python
try:
    import watchfiles
except ImportError:
    backend = PollingWaitBackend()
```

## Integration Points

Files to touch:

- `src/codex_agy_bridge/session_events.py`
- `src/codex_agy_bridge/waiter.py`
- `src/codex_agy_bridge/supervision.py`
- `src/codex_agy_bridge/_orchestrator.py`
- `src/codex_agy_bridge/orchestration.py`
- `src/codex_agy_bridge/server.py`
- `tests/test_session_events.py`
- `tests/test_waiter.py`
- `tests/test_supervision.py`
- `tests/test_orchestration.py`
- `tests/test_server.py`

Public API layers:

- `RunnerOrchestrator.wait(...)`
- `orchestration.wait(...)`
- MCP tool `agy_wait(...)`

## TDD Plan

1. `append_event` creates `session-events.jsonl`.
2. `append_event` bumps `notify.seq`.
3. `read_events(after_event_id)` returns only newer events.
4. Missing event files return empty results for old Runs.
5. `RunSupervisor._launch()` emits `run_started`.
6. `RunSupervisor` does not emit per-transcript-step notifications by default.
7. `RunSupervisor._finish(status="completed")` emits `run_completed`.
8. `RunSupervisor._finish(status="failed")` emits `run_failed`.
9. `RunSupervisor._finish(status="canceled")` emits `run_canceled`.
10. `RunnerOrchestrator.cancel()` emits `cancel_requested`.
11. `RunnerOrchestrator.open_terminal()` emits `terminal_opened`.
12. `RunnerOrchestrator.send_text()` emits `mcp_input`.
13. `wait_for_runs(any_event)` returns after a newer event appears.
14. `wait_for_runs(any_attention)` ignores ordinary events such as
    `terminal_opened`.
15. `wait_for_runs(any_terminal)` ignores non-terminal events.
16. `wait_for_runs(all_terminal)` waits until every selected Run has terminal
    event/state.
17. `wait_for_runs(timeout)` returns `matched=false`.
18. `agy_wait` rejects empty `run_ids`.
19. `agy_wait` caps timeout to a safe maximum.
20. `agy_wait` returns compact statuses and latest event ids.
21. `agy_start` returns notification resource metadata.
22. Existing `agy_status` behavior remains unchanged.
23. Full non-live suite remains green.

## Suggested Implementation Order

### Slice 1: Durable Events

- Add `session_events.py`.
- Add unit tests.
- Emit terminal events from supervisor finish.

### Slice 2: Event Emission Coverage

- Emit start, cancel, terminal-opened, and MCP-input events.
- Keep payloads compact.

### Slice 3: Waiter

- Add polling `WaitBackend`.
- Add `wait_for_runs`.
- Test all wait conditions with fake clock/sleeper where possible.

### Slice 4: MCP Tool

- Add `RunnerOrchestrator.wait`.
- Add `orchestration.wait`.
- Add `server.agy_wait`.
- Add notification resource URI metadata to `agy_start` responses.
- Update MCP instructions to prefer resource notifications or `agy_wait` over
  status polling.

### Slice 5: Live Validation

- Start three Runs.
- Call `agy_wait(..., condition="any_terminal")`.
- Confirm Codex does not need repeated status calls.
- Confirm event files are inspectable on disk.
- Confirm no per-transcript-step notification spam.

## Non-Goals For V1

- Mandatory `watchfiles` dependency.
- SQLite transcript index.
- Full transcript search.
- Perfect human/MCP attribution.
- Replacing `agy_status`.
- Replacing supervisor loop entirely.
- Cross-machine notifications.
- Guaranteed unsolicited model wakeup across every MCP host.

## Open Questions

1. Should `agy_wait` accept `goal_id` directly, or only `run_ids` in v1?
2. Should `after_event_id` be global across Runs or per-Run?
   - V1 recommendation: accept one scalar for simplicity, return per-Run latest
     ids.
3. Should transcript-step events include content snippets?
   - V1 recommendation: no transcript-step notification events at all. Keep
     transcript details in transcript APIs/indexes.
4. Should timeout return HTTP/MCP error or `matched=false`?
   - V1 recommendation: `matched=false`.
5. Should the waiter finalize stale active Runs the way `agy_status` currently
   reconciles dead runners?
   - V1 recommendation: no. Keep reconciliation in `agy_status`/janitor first,
     then revisit.
6. Should `agy_start` return only an `agy-run://.../notifications` resource URI,
   or also an absolute path to `notify.seq` for local debugging?
   - V1 recommendation: return the resource URI in normal responses and expose
     raw paths only in non-compact status/debug output.
