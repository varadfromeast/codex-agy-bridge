# Lean Foreground Agy CLI Plan

Date: 2026-06-16

## Goal

Make each interactive Run feel like a normal `agy` CLI session owned by the
MCP bridge.

Codex starts and manages Runs through MCP. A human can open any Run's visible
terminal and steer that same `agy` session directly.

## Current Problem

Interactive terminals are not truly the foreground CLI.

Today the tmux pane runs a shell wrapper that starts `agy` in the background,
redirects output to files, and tails bridge progress into the visible terminal.
That is useful for observation, but it is not the same as typing into the live
CLI.

## Target Behavior

For interactive Runs:

```text
Terminal.app -> tmux session -> foreground agy --prompt-interactive ...
```

The human can type into the terminal as if they launched `agy` manually.
The MCP bridge still owns lifecycle, status, cancellation, logs, and optional
MCP-originated input.

For print Runs, keep the existing non-interactive behavior unless a separate
change says otherwise.

## Design

1. Keep tmux as the Execution Session container.
   - tmux provides persistence, reattach, process cleanup, named sessions, and
     MCP `send-keys` support.
   - The change is inside the tmux pane: foreground `agy`, not a transcript
     tail.

2. Add human-readable session labels.
   - Persist a label on each Run.
   - Prefer Goal target names when present.
   - Always include a short Run suffix to avoid collisions.
   - Example: `agy-tests-a1b2c3d4`.

3. Split terminal launch behavior by execution mode.
   - `print`: current wrapper is allowed to remain.
   - `interactive`: foreground `agy --prompt-interactive`.
   - Keep `tmux pipe-pane` for terminal capture.

4. Track MCP-originated input.
   - When `agy_target_send_text` sends text, record an input event with
     `origin=mcp`.
   - Human terminal input is inferred later from Antigravity transcript
     `USER_INPUT` events that do not match MCP input.
   - Ambiguous cases should be reported as `origin=unknown`, not guessed.

5. Expose labels in MCP responses.
   - `agy_status`
   - `agy_goal_status`
   - `agy_target_open_terminal`
   - possibly start responses

## Implementation Slice

1. Add safe label generation.
2. Persist `session_label` in Run state.
3. Use label-derived tmux session names for new Runs.
4. Add an interactive foreground launch path in `terminal.py`.
5. Update `agy_target_open_terminal` responses with label metadata.
6. Add MCP input logging.
7. Add transcript/input-origin reconciliation later if needed for UI clarity.

## Tests

- Label sanitization and collision resistance.
- Existing Runs with old tmux names still work.
- Interactive launch keeps `agy` in the foreground.
- Print launch keeps existing behavior.
- `agy_target_send_text` records MCP input before sending keys.
- Goal status includes labels for multiple targets.

## Non-Goals

- Durable scheduling.
- Result sidecar metadata.
- MCP resource URIs.
- Replacing tmux.
- Perfect attribution of raw terminal keystrokes before Antigravity records
  transcript events.

## Open Question

Should standalone interactive Runs accept an explicit `session_label`, or should
labels remain derived from prompt and Run ID until Goals provide target names?
