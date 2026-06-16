# Lean Control Plane And Wait Plan

Date: 2026-06-16

## Goal

Turn `codex-agy-bridge` into a lean control plane for foreground `agy` CLI
workers.

Codex should own task achievement. `agy` should spend Gemini tokens doing
delegated worker tasks. Humans may attach to the same terminal session and steer
the worker, but humans do not own final task completion.

```text
Codex
  owns goal, acceptance criteria, orchestration, result analysis
  sends small task packets to agy
  reads compact transcript/result signals
  decides continue/done/cancel

MCP bridge
  durable session manager
  tmux/terminal attach surface
  input/output event log
  completion/cancel enforcement
  transcript/result summarization

agy CLI
  foreground worker process
  does local reasoning/tool work using Gemini
  emits transcript/events
  accepts human steering or MCP input

Human Terminal
  optional collaborator
  can steer worker live
  does not own final task completion
```

## Core Design

The bridge should behave like a small Omnigent-style meta-harness:

- The `agy` CLI remains the underlying worker harness.
- tmux remains the durable execution substrate.
- MCP remains the control plane.
- Terminal.app is only a human collaboration surface.
- Files remain the source of truth for durable state, events, logs, and results.

The visible tmux pane should run the actual `agy` CLI, not a log reader.

```text
Before:
  tmux pane -> shell wrapper -> background agy + tail terminal-progress.log

After:
  tmux pane -> foreground agy CLI
```

Codex should not continuously chat with `agy`. It should send compact task
packets, then read compact signals and decide whether to accept, redirect,
continue, or cancel.

## Track A: Foreground `agy_start` Control Model

### 1. Split Overloaded Execution Concepts

Current code uses `execution_mode="print" | "interactive"` for too many
concerns:

- CLI mode
- foreground vs headless launch behavior
- whether humans can type into the session
- whether hard timeout applies
- whether interactive queue delivery is enabled

Add explicit fields:

```text
agent_mode: task | conversation
execution_surface: foreground | headless
human_attachable: bool
```

Initial mapping:

```text
agy_start
  agent_mode=task
  execution_surface=foreground
  human_attachable=true

agy_interactive_start
  agent_mode=conversation
  execution_surface=foreground
  human_attachable=true

legacy/headless compatibility
  agent_mode=task
  execution_surface=headless
  human_attachable=false
```

Files:

- `src/codex_agy_bridge/state.py`
- `src/codex_agy_bridge/run_request.py`
- `src/codex_agy_bridge/_orchestrator.py`

### 2. Make `agy_start()` Foreground By Default

`agy_start()` should create a real tmux-backed foreground `agy` CLI session.

The human should be able to attach through:

```text
agy_target_open_terminal(run_id)
```

Optional later parameter:

```text
open_terminal: bool = false
```

Keep headless/background behavior as compatibility, either through an explicit
parameter or a separate compatibility tool. Do not delete the old path until
foreground task mode is live-tested.

Files:

- `src/codex_agy_bridge/server.py`
- `src/codex_agy_bridge/orchestration.py`
- `src/codex_agy_bridge/_orchestrator.py`

### 3. Make Terminal Launch Mode Explicit

Change the execution session interface so terminal behavior is explicit:

```python
terminal.launch(..., execution_surface="foreground")
```

Foreground behavior:

```text
tmux pane -> foreground agy CLI
tmux pipe-pane still captures visible terminal bytes
```

Headless behavior:

```text
tmux pane -> existing wrapper/log-tail behavior
```

Files:

- `src/codex_agy_bridge/terminal.py`
- `src/codex_agy_bridge/execution.py`
- `src/codex_agy_bridge/runner.py`

### 4. Add Lean Task Packet Formatting

Centralize initial worker prompts so Codex sends compact, structured task
packets rather than long free-form conversations.

New file:

```text
src/codex_agy_bridge/task_packet.py
```

Packet shape:

```text
Task:
Acceptance:
Constraints:
Expected output:
Completion marker:
```

Rules:

- Keep the packet short.
- Include the completion marker.
- Tell `agy` to report only the useful result.
- Tell `agy` to ask only if blocked.
- Do not expose bridge implementation details unless necessary.

### 5. Keep MCP-Owned Completion

The supervisor owns lifecycle completion.

When the transcript contains the completion marker:

1. clean the final response
2. write `final-result.txt`
3. mark the Run `completed`
4. emit a terminal session event
5. stop/kill the tmux session

File:

- `src/codex_agy_bridge/supervision.py`

Important compatibility rule:

- Old interactive Runs with empty `completion_marker` must not auto-complete on
  arbitrary responses.

## Track B: Event Log And Transcript Read Model

### 1. Add `session-events.jsonl`

Create a normalized append-only event stream for every Run.

Path:

```text
runs/<run_id>/session-events.jsonl
```

Event examples:

```json
{"event_id":"000000000001","run_id":"...","kind":"run_started","created_at":"..."}
{"event_id":"000000000002","run_id":"...","kind":"mcp_input","origin":"mcp"}
{"event_id":"000000000003","run_id":"...","kind":"human_input","origin":"human"}
{"event_id":"000000000004","run_id":"...","kind":"run_completed","status":"completed"}
```

New file:

```text
src/codex_agy_bridge/session_events.py
```

Responsibilities:

- allocate monotonic event ids per run
- append JSONL events under a file lock
- update a small notification marker after each append
- read events after an event id
- tolerate missing files for old Runs

Suggested interface:

```python
append_event(run_dir, kind, payload) -> dict
latest_event_id(run_dir) -> str | None
read_events(run_dir, after_event_id=None, limit=100) -> list[dict]
```

### 2. Tag MCP-Originated Input

Before MCP sends text into tmux, write a structured event:

```json
{
  "event_id": "000000000042",
  "origin": "mcp",
  "kind": "mcp_input",
  "text_sha256": "..."
}
```

Then inject tagged text into `agy`:

```text
<codex-mcp event_id="000000000042">
...
</codex-mcp>
```

Attribution rule:

- transcript input with matching tag/event id -> `origin=mcp`
- transcript input without tag -> `origin=human`
- malformed/conflicting tag -> `origin=unknown`

Files:

- `src/codex_agy_bridge/interactive_input.py`
- `src/codex_agy_bridge/_orchestrator.py`

### 3. Add Transcript Index

Current public transcript reads go through `core.read_steps()`, which rereads
and reparses the complete `transcript.jsonl` every call.

Keep the raw transcript JSONL as the source of truth, but add an indexed read
model for MCP queries.

New file:

```text
src/codex_agy_bridge/session_index.py
```

Suggested storage:

```text
runs/<run_id>/transcript.index.sqlite
```

Suggested tables:

```text
steps(
  step_index,
  source,
  type,
  status,
  created_at,
  content,
  raw_json
)

events(
  event_id,
  origin,
  kind,
  step_index,
  created_at,
  raw_json
)
```

Optional later:

```text
FTS5 table for content search
```

Use the existing `TranscriptHarvester` logic as the starting point for
incremental ingestion: inode, file head, byte offset, pending partial line.

### 4. Change Public Transcript Reads

`agy_transcript` should read compact records from the index when available.

Fallback behavior:

- if no index exists, read raw transcript JSONL
- if index is corrupt, fall back and report fallback mode
- old Runs should continue working

Files:

- `src/codex_agy_bridge/core.py`
- `src/codex_agy_bridge/_orchestrator.py`
- `src/codex_agy_bridge/orchestration.py`
- `src/codex_agy_bridge/server.py`

### 5. Add Lean MCP Query Tools

Add compact session tools:

```text
agy_events(run_id, after_event_id=None, limit=50)
agy_search(run_id, query, origin=None, type=None, limit=20)
agy_human_inputs(run_id, after_event_id=None)
agy_transcript_read(run_id, offset_bytes=0, max_bytes=65536)
```

Purpose:

- Codex asks for compact events, not full transcripts.
- Raw chunk reads remain available for debugging and recovery.

## Track C: Polling Reduction With `agy_wait`

### Problem

Codex currently has to repeatedly call:

```text
agy_status(run_id)
agy_goal_status(goal_id)
agy_transcript(run_id)
```

That burns turns and tokens when many Runs are active.

### Solution

Add a blocking MCP tool:

```python
agy_wait(
  run_ids: list[str],
  condition: "any_event" | "any_terminal" | "all_terminal" = "any_terminal",
  after_event_id: str | None = None,
  timeout_seconds: int = 900,
) -> dict
```

Codex flow:

```text
start run A, B, C
agy_wait([A, B, C], condition="any_terminal")
analyze returned event/result
agy_wait(remaining, condition="any_terminal")
```

### Notification Marker

Add a tiny per-run marker:

```text
runs/<run_id>/notify.seq
```

After every `session-events.jsonl` append:

1. atomically rewrite `notify.seq` with latest event id
2. update marker mtime

This lets the wait path check a tiny file instead of parsing transcript/state.

### Wait Backend Decision

Do not make a Rust-backed watcher mandatory in v1.

V1:

- implement durable event files
- implement `agy_wait`
- use a simple server-side polling backend over `notify.seq`
- bounded backoff, e.g. 100ms -> 500ms -> 1s max

V2:

- optionally add `watchfiles`
- use it only behind a backend interface
- keep file events as source of truth

Reason:

- The first real problem is Codex polling MCP, not the MCP server checking a
  tiny marker file.
- A blocking `agy_wait` removes model-turn churn immediately.
- `watchfiles` adds dependency and packaging surface.
- Durable filesystem events matter more than the wakeup mechanism.

Suggested interface:

```python
class WaitBackend(Protocol):
    def wait_changed(paths: list[Path], timeout: float) -> bool: ...

class PollingWaitBackend:
    ...

class WatchfilesWaitBackend:
    ...
```

New file:

```text
src/codex_agy_bridge/waiter.py
```

## Event Emission Points

From `RunSupervisor`:

- `_launch()` -> `run_started`
- `_observe_conversation()` -> `transcript_step`
- `_finish()` -> `run_completed`, `run_failed`, or `run_canceled`

From `RunnerOrchestrator`:

- `open_terminal()` -> `terminal_opened`
- `send_text()` -> `mcp_input`
- `cancel()` -> `cancel_requested`

From transcript reconciliation:

- tagged user input -> `mcp_input_observed`
- untagged user input -> `human_input`
- ambiguous input -> `unknown_input`

## TDD Order

1. `append_event` writes `session-events.jsonl` and bumps `notify.seq`.
2. `read_events(after_event_id)` returns only newer events.
3. `RunSupervisor._finish()` emits terminal events.
4. `send_text()` emits tagged `mcp_input`.
5. `open_terminal()` emits `terminal_opened`.
6. `agy_wait(any_terminal)` returns when one Run completes.
7. `agy_wait(all_terminal)` waits for all selected Runs.
8. `agy_wait(timeout)` returns `matched=false`.
9. Old Runs with no event files still return sensible status.
10. `agy_start` persists `execution_surface="foreground"` and
    `agent_mode="task"`.
11. Foreground `agy_start` uses real CLI tmux launch, not log-tail wrapper.
12. Completion marker closes tmux for foreground task Runs.
13. `agy_transcript` uses index when available.
14. Index fallback to raw transcript works.
15. `agy_search` and `agy_events` return bounded compact records.
16. Live test: start 3 `agy_start` foreground Runs, open terminals, steer one
    manually, send MCP input to another, verify attribution and completion.

## Milestones

### Milestone 1: Durable Events And Wait

- `session_events.py`
- `notify.seq`
- event emission for start/finish/cancel/input/open-terminal
- `agy_wait`

This directly solves Codex polling.

### Milestone 2: Foreground `agy_start`

- explicit `agent_mode`
- explicit `execution_surface`
- foreground launch by default for `agy_start`
- headless compatibility path retained

### Milestone 3: MCP Input Tagging And Attribution

- tagged MCP envelopes
- event id correlation
- human/unknown attribution

### Milestone 4: Transcript Index And Search

- `session_index.py`
- indexed compact transcript reads
- `agy_events`
- `agy_search`
- `agy_human_inputs`
- `agy_transcript_read`

### Milestone 5: Live Validation

- start 3 parallel foreground `agy_start` Runs
- open all terminals
- use `agy_wait` instead of status polling
- verify one human input, one MCP input, one normal completion
- verify tmux closes after completion marker

## Non-Goals For First Pass

- Full Omnigent clone.
- Mandatory external file watcher dependency.
- Replacing tmux.
- Replacing filesystem state with a database.
- Perfect human/MCP attribution before transcript events exist.
- Cross-machine distributed scheduling.

## Open Questions

1. Should `agy_start` auto-open Terminal by default, or should it only create an
   attachable foreground session?
2. Should headless mode remain exposed as `agy_start(..., execution_surface="headless")`
   or as a separate compatibility tool?
3. Should `agy_wait` support goal ids directly, or only run ids in v1?
4. What exact MCP input tag format survives Antigravity transcript recording most
   reliably?
5. Should `watchfiles` be an optional extra dependency later, e.g.
   `codex-agy-bridge[watch]`?

