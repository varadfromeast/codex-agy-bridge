# Z.ai Review Remaining Work Report

Date: 2026-06-17

Source: Z.ai shared review, "Deep Code Review: MCP Server Analysis"

This report tracks the review items that remain after the first remediation
pass. The first pass fixed the highest-risk runtime and safety issues around
timeouts, provider-health ordering, completion fallback, repeated stall events,
evolving attention, waiter backoff, janitor result preservation, cancel grace,
status reaper session kill guards, input size caps, dangerous permission flag
honoring, identifier tightening, AppleScript escaping, capability-cache locking,
supervisor traceback logging, and tmux process-tree mitigation.

## Remaining High-Value Work

### 1. Consolidate Dual State-Write Paths

Original concern: `core.update_state` and `DiskRunStore.update_run` both mutate
`state.json` and active-run sentinels.

Current state: Partially mitigated, not fully solved. The existing code now has
tests around active sentinel consistency, but the architecture still has two
write paths.

Remaining implementation:

- Move runner-side state mutation behind `RunStore`, or make
  `core.update_state` a thin compatibility shim over `DiskRunStore`.
- Move goal load/update helpers into the store layer as well.
- Keep `core.py` focused on pure helpers: identifiers, atomic JSON, time,
  transcript parsing, and provider-health classification.
- Add regression tests proving terminal transitions remove active sentinels
  through both MCP and runner paths.

Suggested priority: high. This is the main remaining correctness/drift risk.

### 2. Split Stringly-Typed MCP Tools

Original concern: `agy_goal(action=...)`, `agy_admin(action=...)`, and
`agy_run_observe(view=...)` hide distinct operations behind string parameters.


Current state: Not implemented.

Remaining implementation:

- Split goal actions into explicit tools such as `agy_goal_create`,
  `agy_goal_start_target`, `agy_goal_status`, `agy_goal_list`, and
  `agy_goal_cancel`.
- Split admin actions into explicit tools such as `agy_admin_doctor`,
  `agy_admin_models`, `agy_admin_plugins`, `agy_admin_validate_plugin`,
  `agy_admin_changelog`, and possibly `agy_admin_metrics`.
- Split observe views into dedicated tools or add thin wrappers for
  `status`, `transcript`, `terminal`, and `full`.
- Keep old multiplexed tools temporarily as compatibility aliases if existing
  clients rely on them.
- Update MCP stdio tests and tool documentation.

Suggested priority: high for client ergonomics, medium for runtime safety.

### 3. Add Command Registry After Tool Split

Original concern: once tool actions are split, dispatch should become an
explicit command registry.
Search online how resgistry of tools works on a proper MCP.

Current state: Not implemented.

Remaining implementation:

- Introduce command objects or handler functions with one handler per tool
  action.
- Make dispatch tables the source of truth for validation and routing.
- Unit test each command independently from the MCP adapter.

Suggested priority: medium, best done together with the MCP tool split.

### 4. Make Completion Detection a Strategy

Original concern: `_completion_is_stable` is one hardcoded strategy.

Current state: Partially handled. The first pass added a fallback for stable
`PLANNER_RESPONSE` records with `status=DONE`, while keeping marker detection as
the fast path. It did not extract the behavior into a pluggable strategy.

Remaining implementation:

- Extract a `CompletionDetector` protocol or strategy object.
- Provide at least marker-based and transcript-DONE strategies.
- Inject the detector into `RunSupervisor`.
- Move completion tests from private method assertions to strategy-level tests.

Suggested priority: medium. Useful for testability and future CLI transcript
format changes.

### 5. Replace Polling With Event Fanout or File Watchers

Original concern: consumers poll event files, which adds latency and repeated
file reads.

Current state: Partially mitigated. Waiter backoff now works and active
attention has an `attention.state.json` projection, but event consumption is
still polling based.

Remaining implementation:

- Add an in-process pub/sub fanout inside the MCP server.
- Bridge runner file appends into that fanout with filesystem notifications.
- On macOS, evaluate `watchdog`/FSEvents for low-latency event wakeups.
- Keep durable JSONL as the source of truth so waits remain resumable.
- Add tests for missed notifications and fallback polling.

Suggested priority: medium. This is mainly latency and scale work now.

### 6. Add Per-Request-Key Start Locks

Original concern: `create_run` uses one global `start.lock`, so unrelated run
starts serialize.

Current state: Not implemented. The current global lock is correct but coarse.

Remaining implementation:

- Derive a lock path from `request_key`, such as `start-<hash>.lock`.
- Keep a small global critical section only for global parallel-limit checks if
  needed.
- Preserve duplicate suppression for identical requests.
- Add concurrency tests showing unrelated starts can spawn concurrently while
  identical starts dedupe.

Suggested priority: medium. Useful once high parallel launch pressure matters.

### 7. Split Read Model From Command Handler

Original concern: read-only operations and mutations both live in
`RunnerOrchestrator`.

Current state: Not implemented.

Remaining implementation:

- Introduce a `RunReadModel` for `status`, `observe`, transcript reads, terminal
  snapshots, and result metadata.
- Keep mutations in a command handler or lifecycle service.
- Consider a materialized SQLite read model updated from session events if
  observing many runs becomes slow.
- Add tests proving read paths have no side effects.

Suggested priority: medium to low until observe/status scale becomes painful.

### 8. Remove Config/Import Cycle Smell

Original concern: lazy imports of `orchestration` inside orchestrator paths point
to a config ownership problem.

Current state: Not implemented.

Remaining implementation:

- Move `STATE_ROOT`, `AGY_ROOT`, and related environment-derived paths into a
  dependency-light `config.py`.
- Have `core`, `orchestration`, `runner`, and diagnostics import config instead
  of each other.
- Add tests for environment override behavior.

Suggested priority: medium. It is enabling architecture cleanup more than an
immediate runtime bug.

### 9. Structured Observability

Original concern: logs, metrics, tracing, and event versioning are too sparse.

Current state: Partially handled. Supervisor exceptions now write
`supervisor-traceback.log`. The rest is not implemented.

Remaining implementation:

- Add structured Python logging, preferably JSON, for MCP and runner processes.
- Define counters for runs created, completed, failed by reason, spawn failures,
  janitor reaps, wait timeouts, and input delivery failures.
- Expose metrics through an admin tool or optional local endpoint.
- Add OpenTelemetry spans for MCP tool call, runner lifecycle, tmux launch, and
  Antigravity CLI execution.
- Add `bridge_version` to session events.

Suggested priority: medium. Very useful before larger live stress batches.

### 10. Split `_orchestrator.py`

Original concern: `_orchestrator.py` is too large and mixes lifecycle,
observation, result I/O, goals, and input delivery.

Current state: Not implemented.

Remaining implementation:

- Split into modules such as `lifecycle.py`, `observation.py`, `goals.py`,
  `result_io.py`, and `input_delivery.py`.
- Keep `RunnerOrchestrator` as a facade if that preserves external call sites.
- Move tests gradually with each extracted responsibility.

Suggested priority: low to medium. Best done after the repository/config cleanup
so the split does not just move tangled imports around.

## Remaining Smaller Nits

### Remove Redundant `_request_key` Alias

Current state: Not implemented.

Remaining implementation: Replace
`from codex_agy_bridge.run_request import (_request_key as _request_key,)` with
a normal import.

### Add Debug Logging in `_process_alive`

Current state: Not implemented.

Remaining implementation: Log suppressed process-liveness exceptions at DEBUG
without making status/cancel noisy.

### Centralize `WaitCondition`

Current state: Not implemented.

Remaining implementation: Define `WaitCondition` once and import it from the
canonical module instead of duplicating compatible literals.

### Harden `TmuxSession.__init__` Defaults

Current state: Not implemented.

Remaining implementation: Make `execution_mode` and `execution_surface` required
kwargs or validate that direct construction matches persisted run state.

## Items That Are Effectively Closed

These are not remaining work unless a deeper redesign is desired:

- Interactive runs now have a hard wall-clock cap.
- Provider health now uses the latest matching signal.
- Completion can fall back to stable `PLANNER_RESPONSE status=DONE`.
- `progress_stalled` can re-emit with elapsed stall duration.
- Waiter attention events compare payloads and can emit clear/new transitions.
- Waiter poll backoff now follows a clean schedule.
- Fresh queued PID-less runs get janitor spawn grace.
- Status reaper avoids killing young sessions inside `timeout_seconds + 30s`.
- Cancel gives the runner a grace window before force termination.
- Janitor preserves `final-result.txt`.
- Active attention has an `attention.state.json` projection.
- CLI capabilities discovery is locked.
- Interactive input pop is conditional on successful send and state update.
- Tmux descendant signaling revalidates against a captured parent tree.
- `dangerously_skip_permissions` is forced true and false/null inputs are rejected.
- `terminal.attach` escapes session names before AppleScript interpolation.
- Identifiers are tightened to a safe character set.
- `send_text` rejects oversized input with `input_too_large`.
- The synthetic `M-Enter` separator has a clarifying comment.
- `PRIVATE_STATE_FIELDS` and top-level `shutil` cleanup nits are handled.
- Prompt redaction uses non-mutating list construction.

## Suggested Next Implementation Order

1. Consolidate state writes through `RunStore`.
2. Split MCP tools into explicit operations, leaving compatibility wrappers.
3. Move config paths into `config.py` to reduce import cycles.
4. Extract completion detection into a strategy.
5. Add structured logging, metrics, and `bridge_version`.
6. Add per-request-key start locks.
7. Split read model from command handler.
8. Split `_orchestrator.py` by responsibility.
