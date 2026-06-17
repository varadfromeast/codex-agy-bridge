# Domain Context

## Run Request

A caller's immutable request to start one Antigravity execution. It includes
the prompt, workspace, execution policy, continuation identity, and optional
goal target identity. Preparing a Run Request validates and normalizes these
values and computes the deduplication identity.

## Run

A durable execution record created from a prepared Run Request. A Run owns a
stable `run_id`, persisted lifecycle state, logs, and one execution session.

## Execution Policy

The explicit controls that shape Antigravity execution: model, sandbox mode,
permission auto-approval, additional directories, timeout, and print versus
interactive mode.

## Execution Session

The live process container for a Run. Production uses a persistent tmux
session; tests can use an in-memory adapter through the same interface.

## Goal

A durable parent record that groups named Run targets and supplies inherited
execution policy plus a parallelism limit.

## Conversation

Antigravity's durable model-side context identity. Continuation always uses an
exact conversation ID; separate conversations do not share native context.

## Control Plane

The bridge control plane is responsible for answering one question reliably:
what is this Run doing right now? Do not rely on raw lifecycle `status` alone.
Use these separate concepts:

- `lifecycle_status`: queued, running, cancel_requested, completed, failed, or
  canceled.
- `activity_state`: starting, working, awaiting_user, awaiting_mcp_input,
  waiting_for_response, possibly_stalled, terminal, or idle.
- `attention`: a structured object describing whether Codex or a human should
  act, why, and which inputs are plausible.

`RunControlSnapshot` is the projection module for this combined view. It reads
durable state, sparse Run events, transcript position, interactive input queue
state, and terminal/log hints. `agy_run_observe`,
`agy_goal(action="status")`, `agy_run_wait`, and `agy_run_input` should use
this projection rather than each inventing its own interpretation of Run state.

## Run Events

Run events are durable sparse control-plane facts in `session-events.jsonl`.
They are not a transcript mirror. Emit events only for lifecycle, attention,
terminal-observation, progress-stall, cancellation, and MCP input-delivery
changes that should wake or orient a client.

There are two event identifiers:

- `run_seq`: the per-Run cursor stored in `notify.seq`; efficient for
  `after={run_id: cursor}` waits.
- `event_id` / `latest_event_key`: globally keyable as
  `{run_id}:{run_seq}`. Clients should key UI/cache records by this global
  value, not by a bare sequence.

Old numeric-only event IDs must continue to be readable. Project them to a
global key when exposing them to clients.

## Attention And Prompt Detection

Approval prompts are production blockers if invisible. The bridge should detect
known Antigravity permission/approval prompts from:

1. new transcript records,
2. the bounded tail of `terminal.log`,
3. a bounded live `tmux capture-pane` fallback.

Terminal output contains ANSI/control sequences and the actual approval prompt
often includes a menu after the question:

```text
Do you want to proceed?
> 1. Yes
  2. Yes, and always allow...
  3. Yes, and always allow... (Persist to settings.json)
  4. No
```

Prompt detection must treat that menu as active, while rejecting stale prompts
followed by normal later output. `agy_run_wait` must not let live pane capture
consume the whole wait budget; it should use a small per-run capture budget and
return `matched=false` on timeout.

## Permission Policy

The production policy is that Antigravity CLI approval prompts must not wedge
Codex. The bridge always forces Antigravity's dangerous permission-skip policy
on when preparing a Run Request. The request API only accepts
`dangerously_skip_permissions=true`; false or null values are rejected before
request-key/state creation.

This is intentionally risky: Antigravity can read/write files and run commands
with the current user's privileges. The bridge is not a sandbox or filesystem
security boundary. Agents and docs should warn about this, but agents should
not be able to disable the policy through MCP tool arguments.

## Observation Tools

`agy_run_wait` is the low-churn wake mechanism, not the only source of truth. If
a wait times out or looks suspicious, Codex should call
`agy_run_observe(view="full")` to read a merged view: Run state, events,
transcript cursor, terminal hints, and provider health. When Codex needs the
actual foreground CLI state, it should call `agy_run_observe(view="terminal")`
to read bounded raw tmux pane and log tails without asking the bridge to
classify the prompt.

The runner emits `progress_stalled` after a transcript cursor remains unchanged
for `AGY_BRIDGE_TRANSCRIPT_IDLE_SECONDS`. This is a wake-up warning, not a
verdict. Codex should inspect the transcript and terminal view before sending
input.

## Text Input Delivery

`agy_run_input` is a delivery API, not a model-response wait. It should reject
stale writes when the caller provides `expected_event_key` or
`expected_transcript_step` and the Run has advanced since Codex observed it. On
stale rejection, return the latest transcript step and compact status so Codex
can decide from fresh evidence. If preconditions pass, record
`mcp_input_submitted`, attempt bounded tmux delivery, then record either
`mcp_input_delivered` or `mcp_input_failed`. It must never block waiting for
Antigravity to respond. After successful delivery, status should not
immediately reopen the same stale approval prompt while terminal output is
settling.

## Cancellation And Results

Cancellation should converge quickly to terminal `canceled`, kill the execution
session/process group with bounded TERM/KILL behavior, emit `run_canceled`, and
avoid publishing misleading partial planner text as a final result artifact.
Only completed Runs should synthesize or expose final result artifacts.

## Current Stabilization Focus

Recent live MCP stress testing found that permission prompts can be invisible to
`agy_run_wait` and `agy_goal(action="status")`, especially file-access prompts.
The current mitigation is twofold: force dangerous permission-skip on every Run
Request and improve prompt detection for the visible approval menu shape. Keep
the TODO live defect open until a fresh MCP live run verifies file-access
prompts no longer wedge sessions.

Before calling the bridge production-stable, run another live stress pass that
specifically checks:

- `agy_run_wait(condition="any_attention")` returns promptly for approval
  prompts,
- `agy_goal(action="status")` shows `activity_state="awaiting_user"` when
  attention is required,
- `agy_run_observe` can reveal terminal/transcript state after a suspicious
  timeout,
- `agy_run_input` never hangs and returns structured delivery state,
- dangerous permission-skip is persisted as `true` even when callers pass
  `false`.
