# V1 Live Test Defects

## V1-DEFECT-001 - Worker Failure Leaves Interactive Run Active

- Live test: `V1-07`
- Observed: June 15, 2026
- Run ID: `2026-06-15T123634.933919+0000-ec9ac3ee`
- Result: `FAIL`

After the detached Python supervisor received `SIGTERM`, the persisted Run
continued to report `status=running` and `session_state=awaiting_input` because
the tmux child session was still alive. The durable input queue therefore had
no owner, but MCP did not expose an actionable terminal failure.

Expected recovery policy: fail the Run when its authoritative supervisor exits,
stop the surviving execution session, preserve the queue file for inspection,
and reject further input without replay.

Resolution: fixed in the working tree. `RunnerOrchestrator.status()` now treats
the supervisor as authoritative, and `LocalProcessManager.is_alive()` reaps
terminated child processes instead of classifying zombies as alive. The live
scenario passed after the fix.

## V1-LIMITATION-001 - CLI Sandbox Does Not Contain Filesystem Writes

- Live tests: `V1-11`, `V1-12`
- Observed: June 15, 2026
- Antigravity CLI: `1.0.8`
- Result: `KNOWN UPSTREAM LIMITATION`

With `sandbox=true`, Antigravity successfully wrote to a dedicated temporary
directory outside the workspace. It also wrote to an unapproved sibling when
one additional directory was configured. The bridge correctly persisted and
forwarded the requested policy, but the CLI did not enforce it as a filesystem
containment boundary.

These cases remain executable as expected failures so a future CLI release can
prove stronger behavior. They must not be V1 bridge release gates while the
README states that the bridge and workspace are not security boundaries.

## V1-DEFECT-002 - Cancellation Leaves Tool Subprocesses Running

- Live test: `V1-19`
- Observed: June 15, 2026
- Runs:
  - `2026-06-15T140953.654938+0000-64f63474`
  - `2026-06-15T140953.664264+0000-2ccec774`
  - `2026-06-15T140953.684328+0000-0f2580a9`
- Result: `FAIL`

After the 20-minute mixed-capacity soak canceled all four Runs, the bridge
reported zero active Runs and removed every test-owned tmux session. However,
three terminal-tool commands remained alive as PID-1 children:

```text
python3 -c import time; time.sleep(1500)
```

The tmux pane cleanup terminates the Antigravity process, but a terminal-tool
command may create its own process group. Such descendants are not terminated
by `tmux kill-session` and can continue executing after the Run is reported
`canceled`.

Expected behavior: cancellation snapshots the tmux pane's descendant process
tree, terminates those descendants, kills the tmux session, and escalates any
survivors before recording cleanup as complete.

Resolution: fixed with a public `TmuxSession` regression test that launches a
child in an independent process group. `terminal.stop()` now snapshots the
pane's descendant tree, sends `SIGTERM` to descendant process groups, destroys
the tmux session, waits briefly, and sends `SIGKILL` to surviving snapshot
members. A 60-second live mixed-capacity reproduction started three uniquely
marked hold commands and confirmed zero marked processes after cancellation.
