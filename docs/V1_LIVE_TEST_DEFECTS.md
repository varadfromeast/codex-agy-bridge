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
