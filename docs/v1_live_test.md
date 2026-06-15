# V1 User-Facing Live Test Plan

## Why This Plan Exists

The core automated lifecycle is well covered. The least explored user-facing
area is **human intervention during a live Run**:

- Terminal.app attachment
- permission and authentication prompts
- recovery after disconnects or process restarts
- clarity when the bridge is waiting for the user

These paths were skipped or only indirectly observed because previous live
tests were designed to run unattended.

The second-largest gaps are observing upstream CLI policy behavior and proving
durability of queued interactive input across restarts.

## Priority

| Priority | Area | Current confidence |
| --- | --- | --- |
| P0 | Human permission/authentication workflow | LOW |
| P0 | Interactive recovery across server/worker restart | LOW |
| P1 | CLI sandbox policy behavior | MEDIUM-LOW |
| P1 | Terminal attachment and reattachment | MEDIUM-LOW |
| P1 | Long interactive soak and queue pressure | MEDIUM |
| P2 | Multi-repository write workflow | MEDIUM |
| P2 | Diagnostics under real provider/account failures | MEDIUM |

## Execution Rules

- Use isolated temporary workspaces and state roots.
- Never use production credentials specifically created for destructive tests.
- Outside-write tests may target only a dedicated temporary denial directory.
- Record timestamps, Run IDs, conversation IDs, and bounded transcript excerpts.
- Verify behavior through MCP state and transcripts, not terminal appearance alone.
- Cancel every test Run and confirm no test-owned tmux sessions remain.
- When a test fails, record a defect before changing product code.
- Fix defects with one RED-GREEN TDD slice at a time.

Result values: `NOT RUN`, `PASS`, `FAIL`, `BLOCKED`, `SKIPPED`.

## Phase A: Human Intervention

### V1-01 - Permission Prompt Is Observable

Start a non-auto-approved Run that requests a harmless filesystem action.

Pass:

- MCP status remains active rather than failing silently.
- Provider diagnostics identify that user interaction is required.
- The response tells the user what action to take.
- No duplicate prompt or duplicate Run is created.

Result: `NOT RUN`

### V1-02 - Approve Permission Through Attached Terminal

Open the exact Run terminal and approve one harmless action.

Pass:

- `agy_target_open_terminal` attaches to the persisted tmux session.
- Approval unblocks the existing Run.
- The Run completes without creating a replacement conversation.
- MCP status and transcript reflect the resumed work.

Result: `NOT RUN`

### V1-03 - Deny Permission

Deny the requested action.

Pass:

- Denial does not crash the bridge or leave the Run permanently ambiguous.
- The agent can report or recover from the denial.
- Cancellation remains available.

Result: `NOT RUN`

### V1-04 - Authentication Recovery

Use an isolated CLI configuration where authentication is missing or expired.

Pass:

- Doctor/provider diagnostics classify the authentication problem.
- The Run remains controllable.
- Completing authentication in the attached terminal allows work to continue,
  or the Run terminates with an actionable bounded error.

Result: `NOT RUN`

### V1-05 - Terminal Reattachment

Attach, close Terminal.app, then attach again while the Run remains active.

Pass:

- Closing Terminal.app does not kill tmux or the Run.
- Reattachment targets the same session.
- No second CLI process is launched.

Result: `NOT RUN`

## Phase B: Restart And Durability

### V1-06 - Interactive Queue Survives MCP Restart

Queue several prompts, disconnect the MCP client, reconnect, and observe them.

Pass:

- The same Run and conversation remain observable.
- Queued prompts are processed exactly once and in order.
- New prompts can be appended after reconnect.

Result: `PASS` - fresh MCP server reconnect preserved the Run, conversation,
FIFO ordering, and post-reconnect input.

### V1-07 - Interactive Queue Survives Worker Failure

Queue prompts, terminate only the detached Python supervisor, and inspect the
persisted Run.

Pass:

- The bridge does not claim queued prompts were delivered.
- Status reports an actionable terminal failure.
- Queue files remain valid and inspectable.
- Recovery behavior is explicit: resume, replay, or fail without duplication.

Result: `PASS` - supervisor death now produces an actionable failed state,
preserves the queue, and stops the surviving tmux session.

### V1-08 - Server Restart During Permission Prompt

Restart the MCP server while Antigravity waits for user input.

Pass:

- The Run remains attached to the original tmux session.
- Diagnostics still identify the pending interaction.
- Approval and cancellation work after reconnect.

Result: `NOT RUN`

### V1-09 - Cancel With Pending Interactive Queue

Queue multiple prompts, then cancel before all are delivered.

Pass:

- No queued prompt is delivered after cancellation.
- Terminal state remains `canceled`.
- Pending queue contents cannot revive the Run.

Result: `PASS`

## Phase C: Real Safety Boundaries

### V1-10 - Sandbox Allows Workspace Write

Run with `sandbox=true` and write a nonce file inside the workspace.

Pass:

- The intended file is created with exact content.
- Persisted state records sandbox mode.

Result: `PASS` - workspace write succeeded and sandbox policy was persisted.

### V1-11 - Observe Outside Write Behavior

Attempt to write into a dedicated temporary directory outside the workspace
and outside all additional directories.

Record:

- Whether the outside file is created or modified.
- Any CLI denial is visible and actionable.
- The Run remains controllable regardless of the CLI result.

Result: `KNOWN UPSTREAM LIMITATION` - Antigravity CLI 1.0.8 allowed this write
in one live reproduction. The case remains an executable expected failure.

### V1-12 - Additional Directory Policy Behavior

Provide one additional directory and attempt writes to:

1. the primary workspace,
2. the approved additional directory,
3. an unapproved sibling directory.

Record:

- Behavior in the workspace and approved additional directory.
- Behavior in the unapproved sibling directory.
- Persisted normalized CLI policy hints and observed behavior.

Result: `KNOWN UPSTREAM LIMITATION` - approved writes succeeded, but the CLI
also wrote to the unapproved sibling. The case remains an executable expected
failure.

### V1-13 - Observe Symlink Escape Behavior

Place a workspace symlink pointing outside the allowed roots and ask the agent
to write through it.

Record:

- Record whether the Antigravity sandbox blocks the resolved escape.
- Any CLI limitation is documented as a product safety limitation.

Result: `NOT RUN`

## Phase D: Real User Workflows

### V1-14 - Multi-Repository Change

Use one primary workspace and one approved additional repository. Ask the agent
to make a small coordinated change and run tests in both.

Pass:

- Both repositories are accessible.
- Changes occur only in approved roots.
- Transcript and final result clearly identify work in each repository.

Result: `NOT RUN`

### V1-15 - Goal With User Intervention

Start three Goal targets where one requires permission approval.

Pass:

- Other targets continue independently.
- Goal status accurately shows mixed active/completed states.
- Approving one target does not affect another target's terminal.
- Aggregate completion occurs only after all targets terminate.

Result: `NOT RUN`

### V1-16 - Explicit Artifact Handoff

Have one target create a report and a later independent target consume it from
an explicitly approved path.

Pass:

- No native conversation context leaks between targets.
- The artifact is the only context transport.
- Missing or malformed artifacts produce actionable errors.

Result: `NOT RUN`

## Phase E: Soak And Pressure

### V1-17 - Eight-Hour Interactive Idle Soak

Keep an interactive Run awaiting input for eight hours with periodic status
and doctor calls.

Pass:

- Run remains active and `awaiting_input`.
- Memory, state files, logs, and transcript reads remain bounded.
- A final prompt is processed successfully.

Result: `NOT RUN`

### V1-18 - Large Interactive Queue

Rapidly enqueue 100 numbered prompts with short expected responses.

Pass:

- MCP calls remain responsive.
- Every prompt is delivered exactly once and in order.
- Queue storage remains valid during concurrent reads and cancellation.
- Resource use remains acceptable and recorded.

Result: `PASS` - 100 prompts were accepted in 1.02 seconds and delivered
exactly once in FIFO order in 310.61 seconds. All 100 responses contained the
expected token. State grew from 29,568 bytes after enqueue to 246,679 bytes
after drain, and cancellation left no runner or tmux session.

### V1-19 - Mixed Capacity Soak

Run a mix of print, interactive, sandboxed, unrestricted, and Goal targets at
the global capacity limit for at least 20 minutes.

Pass:

- Capacity is never exceeded.
- Diagnostics remain responsive.
- Completion and cancellation release capacity promptly.
- No orphan workers, sentinels, or tmux sessions remain.

Result: `PASS WITH DEFECT FIXED` - four mixed workloads remained active for
1,200 seconds across 37 health samples, the fifth start was rejected, and MCP
diagnostics remained responsive. Initial cleanup exposed `V1-DEFECT-002`:
three terminal-tool subprocesses survived cancellation after escaping into
independent process groups. The terminal adapter now terminates the tmux pane's
descendant process tree before destroying the session. A 60-second live
reproduction then proved all three marked subprocesses, all active Runs, and
all tmux sessions were removed.

### V1-20 - Disk And Log Pressure

Generate sustained transcript/log output near configured bounds.

Pass:

- Public MCP responses remain bounded.
- State updates remain valid JSON.
- Disk growth and cleanup behavior are recorded.
- The MCP server remains responsive.

Result: `NOT RUN`

## Defect Workflow

For each failure:

1. Add a defect record with exact reproduction evidence.
2. Add one public-interface regression test and confirm RED.
3. Run scoped GitNexus impact analysis before editing.
4. Implement the smallest fix and confirm GREEN.
5. Run the related tests, full suite, Ruff, and the failed live scenario.
6. Run GitNexus `detect_changes()` before committing.

## V1 Exit Criteria

- All P0 tests pass.
- V1-10 proves workspace writes work with the requested CLI sandbox policy.
- V1-11 through V1-13 record upstream CLI containment behavior but are not
  bridge release gates; the bridge is not a filesystem security boundary.
- At least one terminal permission flow and one authentication flow are
  completed end to end.
- Restart tests prove no duplicate prompt delivery.
- The interactive queue passes the 100-prompt stress case.
- The mixed-capacity soak leaves zero active Runs, workers, sentinels, or tmux
  sessions.
- Every known failure has a defect record and an explicit release decision.

## Automation Decisions

- Automated live MCP tests: V1-06, V1-07, V1-09, V1-10, V1-11, V1-12.
- Manual macOS acceptance: V1-01 through V1-05 and V1-08 because they require
  visible Terminal.app interaction or authentication mutation.
- Dedicated soak jobs: V1-17 through V1-20; do not run them in the normal test
  suite. V1-18 and V1-19 are executable in `tests/test_v1_soak_live.py` with
  `AGY_LIVE_SOAK_TESTS=1`; V1-19 defaults to 1,200 seconds.
- Remove V1-13, V1-14, and V1-16 from bridge release gating. Symlink
  containment is owned by the upstream CLI, while multi-repository edits and
  file handoff add agent-workflow coverage but no new bridge control-plane
  behavior beyond V1-12.
