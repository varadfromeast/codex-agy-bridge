# Antigravity CLI Extension Report

Date: 2026-06-15

## Executive Summary

The bridge currently uses the stable non-interactive execution core of
Antigravity CLI 1.0.8:

- `--print`
- `--print-timeout`
- `--conversation`
- `--model`
- `--log-file`
- `--dangerously-skip-permissions`

This is enough for asynchronous agent runs, exact continuation, transcript
observation, cancellation, and bounded parallel goals. It does not use two
high-value execution controls exposed by the CLI:

- `--sandbox`
- repeatable `--add-dir`

It also does not expose read-only CLI discovery such as `agy models`, version,
changelog, or imported-plugin listing.

The recommended next release should add sandboxing, additional workspace
directories, dynamic model discovery, and a consolidated diagnostics tool.
Interactive mode should be treated as a separate product capability rather
than mixed into the existing print-mode lifecycle.

## Installed CLI Surface

The installed executable reports:

```text
agy 1.0.8
```

### Global Execution Flags

| CLI option | Current bridge support | Assessment |
|---|---:|---|
| `--print` / `--prompt` | Yes | Correct mode for asynchronous one-shot work |
| `--print-timeout` | Yes | Mapped from persisted run timeout |
| `--conversation <id>` | Yes | Correct exact-continuation primitive |
| `--model <name>` | Yes | Accepted without preflight validation |
| `--log-file <path>` | Yes | Used for bounded post-run diagnostics |
| `--dangerously-skip-permissions` | Yes | Powerful and currently enabled by default |
| `--sandbox` | No | High-value safety improvement |
| `--add-dir <path>` | No | High-value multi-repository capability |
| `--prompt-interactive` | No | Requires a different lifecycle contract |
| `--continue` | No | Intentionally inferior to exact conversation IDs |

### Subcommands

| Subcommand | Current bridge support | Assessment |
|---|---:|---|
| `models` | No | Useful read-only discovery |
| `plugin list` | No | Useful read-only diagnostics |
| `plugin validate` | No | Potentially useful with path containment |
| Plugin mutations | No | Shared-user configuration mutation; high risk |
| `changelog` | No | Useful for compatibility diagnosis |
| `install` | No | Shell configuration mutation; exclude |
| `update` | No | Self-update during MCP operation; exclude |

Available models observed during this probe:

```text
Gemini 3.5 Flash (Medium)
Gemini 3.5 Flash (High)
Gemini 3.5 Flash (Low)
Gemini 3.1 Pro (Low)
Gemini 3.1 Pro (High)
Claude Sonnet 4.6 (Thinking)
Claude Opus 4.6 (Thinking)
GPT-OSS 120B (Medium)
```

## Recommended Extensions

### 1. Sandboxed Runs

Add `sandbox: bool` to:

- `agy_start`
- `agy_continue`
- `agy_goal_target_start`
- persisted `RunState`
- request deduplication keys

When enabled, append `--sandbox` before `--print`.

Recommended policy:

```text
sandbox=true, dangerously_skip_permissions=false
    safest interactive-approval posture

sandbox=true, dangerously_skip_permissions=true
    unattended execution constrained by CLI sandbox

sandbox=false, dangerously_skip_permissions=true
    current unrestricted autonomous behavior
```

The bridge should document that sandboxing and permission auto-approval are
independent controls. Sandbox mode limits terminal capabilities; it does not
make arbitrary prompts trustworthy.

Required tests:

- Flag appears before `--print`.
- Sandbox participates in deduplication.
- Goal targets inherit or override sandbox policy explicitly.
- Persisted older runs without the field remain readable.
- A live sandbox probe cannot write outside its allowed scope.
- Cancellation and transcript observation work identically in sandbox mode.

### 2. Additional Workspace Directories

Add:

```python
additional_directories: list[str] = []
```

Emit one `--add-dir <absolute-path>` pair per directory.

This would allow a narrow primary workspace with explicit secondary
repositories, which is safer and more legible than setting the workspace to a
broad parent such as `/Users/varad/V/repo`.

Validation requirements:

- Resolve every path to an absolute existing directory.
- Reject files, missing directories, duplicates, and NUL bytes.
- Define whether symlinks are preserved or resolved.
- Cap the count and aggregate encoded path length.
- Include normalized directories in request deduplication.
- Persist the normalized list for auditability.
- Do not infer additional directories from prompt text.

Suggested initial limits:

```text
maximum directories: 16
maximum encoded path length per directory: 4096 bytes
```

### 3. Model Discovery and Validation

Add a read-only tool:

```text
agy_models
```

It should execute `agy models`, parse nonempty lines, and return:

```json
{
  "cli_version": "1.0.8",
  "models": ["Gemini 3.5 Flash (Medium)"],
  "default_model": "Gemini 3.5 Flash (Medium)"
}
```

Run creation should optionally validate requested models against this list.
Validation should use a short TTL cache because model availability can change
with CLI updates or account configuration.

Failure policy:

- An explicit unknown model should fail before process creation.
- Failure to query models should produce a warning or diagnostic error, not
  silently substitute a different model.
- Existing persisted runs must retain their recorded model even if it later
  disappears from discovery.

### 4. Bridge and CLI Diagnostics

Add:

```text
agy_doctor
```

Recommended output:

- Bridge package source path.
- Bridge version and git commit when available.
- CLI executable path and `agy --version`.
- Available models.
- `tmux` executable and server status.
- State root writability.
- Antigravity root readability.
- Imported plugin names.
- Active run count and configured parallel limit.
- Recent per-run provider classification, only when a run ID is supplied.
- Whether sandbox and `--add-dir` are supported by the installed CLI.

The tool must be read-only and bounded. It should not scan all historical run
directories or treat old provider failures as launch authority.

### 5. Interactive Session Mode

The existing bridge launches `agy --print` inside tmux. This is still a
non-interactive print-mode process even though a terminal is visible.

Live testing showed:

- `agy_target_send_text` successfully delivered keys to tmux.
- The active print-mode agent did not consume the message as a new prompt.
- It continued its existing tool sequence until completion.

Therefore, interactive operation should use a separate tool:

```text
agy_interactive_start
```

backed by `--prompt-interactive`.

It needs a different state machine:

```text
starting -> interactive -> awaiting_input -> working -> awaiting_input
                                      \-> stopped / failed
```

Unlike print mode, completion of one response must not imply termination of the
session. The bridge would need to distinguish:

- Response completion.
- Session liveness.
- Pending permission or question.
- User-requested session close.
- Conversation persistence after terminal detachment.

Do not retrofit this behavior into `agy_start`; that would make result and
terminal-state semantics ambiguous.

### 6. Read-Only Plugin Diagnostics

Potential tools:

```text
agy_plugins
agy_plugin_validate
```

`agy_plugins` can safely wrap `agy plugin list`.

`agy_plugin_validate` should only accept an existing directory under an
explicit caller-provided workspace or approved plugin root.

Do not expose unrestricted wrappers for:

- `plugin import`
- `plugin install`
- `plugin uninstall`
- `plugin enable`
- `plugin disable`
- `plugin link`

These mutate shared user configuration and may download or execute
third-party content.

Probe finding: plugin subcommands do not consistently interpret `--help`.
For example, some commands treated `--help` as a plugin name or filesystem
target. Adapters must use explicit command schemas and must not infer safety
from conventional help behavior.

## Commands That Should Remain Unexposed

### `--continue`

This resumes the most recent conversation, which is ambiguous under parallel
runs and multiple workspaces. Exact `--conversation <id>` is deterministic and
already supported.

### `install`

This modifies shell paths, aliases, and profile files. Installation belongs to
operator setup, not runtime MCP operation.

### `update`

Self-updating the CLI can change behavior while bridge processes are active.
Updates should be an explicit operator action followed by compatibility tests.

### Plugin Mutations

Plugin mutation affects global/shared Antigravity configuration and expands
the executable capability surface. If ever added, it should require a separate
administrative plugin with explicit confirmation and audit logging.

## Live Stress-Test Findings

The June 15 live campaign exercised:

- Three concurrent repository reviews using Low and Medium models.
- Independent conversation discovery and transcript streams.
- GitNexus use from an Antigravity session.
- Active-request deduplication.
- Goal creation and target aggregation.
- Terminal reattachment.
- Incremental transcript cursors.
- Provider diagnostics.
- Exact conversation continuation.
- Native and artifact-based context sharing.
- Global parallel capacity.
- Concurrent cancellation.
- Terminal-state monotonicity.

### Confirmed Healthy Behavior

- MCP server processes load the current workspace through `uv --directory`.
- Three independent review sessions and a fourth goal target ran concurrently.
- Identical active requests returned the original run ID.
- Incremental transcript calls returned only records after the supplied cursor.
- Goal state moved from running to completed with its target.
- Four concurrent runs filled the configured global capacity.
- A fifth run was rejected with a bounded error.
- Four parallel cancellations converged to `canceled`.
- Repeated status calls did not regress terminal state.
- Canceled results returned `result: null`.
- Provider classification reported authentication without acting as a launch
  gate.
- The clean-environment suite passed: 105 tests, Ruff, and mypy.

### Native Context Isolation

Continuation of the same conversation retained a secret token.

A different conversation, asked for that token without tools or file access,
returned:

```text
NO_SHARED_CONTEXT
```

Therefore, parallel Antigravity conversations do not share native context.

Explicit artifact handoff worked: a sibling conversation read a report from
`/tmp`, accurately summarized it, and identified the filesystem as the context
transport.

Recommended product rule:

```text
conversation context is private
shared context must be an explicit artifact, prompt, or durable goal record
```

### Defect 1: Environment-Sensitive Tests

With the MCP-configured environment:

```text
AGY_CMD=/Users/varad/.local/bin/agy
```

two runner tests fail:

1. The binary-selection test patches `shutil.which` but does not clear
   `AGY_CMD`, which has higher precedence.
2. The tmux-launch test assumes fixed argument positions and does not account
   for propagated `-e NAME=VALUE` options.

The production behavior is correct; the tests are not hermetic.

Recommended fix:

- Use `monkeypatch.delenv("AGY_CMD", raising=False)` when testing PATH lookup.
- Assert semantic tmux command segments rather than the entire positional list.
- Add one test with all propagated bridge environment variables set.

### Defect 2: Tmux Child Exit Status Is Lost

The tmux path records:

```python
return_code=0
```

regardless of the actual `agy` exit status. This masks crashes and makes
failure classification less precise.

Recommended design:

1. The tmux shell writes the child status atomically to:

   ```text
   <run-directory>/agy.exit-code
   ```

2. The supervisor reads and validates the file after session exit.
3. Missing or malformed status remains `None`, not `0`.
4. State persists the real code.
5. Classification distinguishes:

   - nonzero CLI exit
   - zero exit without response
   - signal termination
   - canceled run

### Defect 3: Dead Transcript Cursor State

`RunSupervisor.last_terminal_step` is assigned but no longer used to filter
records. `TranscriptHarvester` already owns byte-level incremental delivery.

Recommended fix:

- Remove `last_terminal_step`.
- Make terminal rendering return `None`, unless the latest index is retained
  strictly for observability.

### Defect 4: Public Contract Compatibility

The former `visible_terminal` argument was removed because every run now uses
tmux and opens Terminal.app. Existing clients that still send the argument may
receive schema rejection.

Options:

1. Reintroduce it as a deprecated ignored field for one compatibility window.
2. Keep the breaking change and publish a major schema/version transition.

The first option is lower risk while the project is still stabilizing.

### Defect 5: `send_text` Semantics Are Overstated

The tool currently promises to send text to a target session. Transport works,
but print-mode Antigravity does not necessarily process it as input.

Recommended fix:

- Clarify the existing tool description: it sends terminal keystrokes and is
  mainly useful for terminal prompts or authentication interaction.
- Only promise conversational follow-up for future interactive sessions.
- Return the run execution mode in status and send-text responses.

### Defect 6: Active Status Can Show a Running Latest Step After Cancellation

Canceled probe runs correctly reached terminal `canceled`, but their latest
transcript event remained a `RUN_COMMAND` with status `RUNNING`. This reflects
producer history, not current run state.

Recommended UI/API rule:

- Run state is authoritative for lifecycle.
- Transcript step status is historical and must not override terminal state.
- Optionally add a synthetic bridge cancellation event to progress output,
  but do not modify Antigravity-owned transcripts.

## Proposed MCP Contracts

### Extended Start

```python
agy_start(
    prompt: str,
    workspace: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] = [],
) -> RunState
```

The same execution fields should be accepted by `agy_continue`. Goal policy
should be persisted on the goal and inherited by targets to avoid target-level
configuration drift.

### Models

```python
agy_models(refresh: bool = False) -> {
    "cli_version": str,
    "default_model": str,
    "models": list[str],
    "observed_at": str,
}
```

### Doctor

```python
agy_doctor(run_id: str | None = None) -> {
    "bridge": {...},
    "cli": {...},
    "tmux": {...},
    "storage": {...},
    "capacity": {...},
    "run_diagnostics": {...} | None,
}
```

### Plugins

```python
agy_plugins() -> {"plugins": list[dict[str, object]]}

agy_plugin_validate(
    path: str,
    workspace: str,
) -> {"valid": bool, "output": str}
```

## Architecture Recommendations

### CLI Capability Adapter

Avoid scattering subprocess calls across server functions. Introduce a narrow
adapter responsible for:

- Executable discovery.
- Version probing.
- Capability probing.
- Model listing.
- Plugin listing and validation.
- Command construction.
- Bounded subprocess output.
- Timeouts and error normalization.

Illustrative interface:

```python
class AntigravityCli:
    def version(self) -> str: ...
    def capabilities(self) -> CliCapabilities: ...
    def models(self) -> list[str]: ...
    def plugins(self) -> list[PluginInfo]: ...
    def validate_plugin(self, path: Path) -> ValidationResult: ...
    def build_run_command(self, state: RunState) -> list[str]: ...
```

This separates changing CLI compatibility from orchestration and MCP schema
code.

### Capability Detection

Do not assume every installed CLI supports every new option. Parse `agy
--help` once per server process or cache a bounded capability record keyed by:

```text
executable path + version + mtime
```

If a requested capability is unavailable, fail before spawning a runner with
an actionable error.

### Security Defaults

The current default is:

```text
dangerously_skip_permissions=true
```

That is convenient but broad. Adding sandbox mode creates an opportunity to
offer safer presets:

| Preset | Sandbox | Auto-approve |
|---|---:|---:|
| `safe` | Yes | No |
| `sandboxed_auto` | Yes | Yes |
| `unrestricted_auto` | No | Yes |

Avoid replacing explicit booleans immediately; presets can be additive and
expanded into persisted explicit fields.

## Phased Delivery Plan

### Phase 0: Correctness Repairs

1. Make runner and terminal tests environment-independent.
2. Persist real tmux child exit codes.
3. Remove dead `last_terminal_step` state.
4. Clarify `send_text` print-mode semantics.
5. Decide `visible_terminal` compatibility policy.

### Phase 1: Safe Execution Expansion

1. Add `sandbox`.
2. Add validated `additional_directories`.
3. Include both in state, request keys, status, and tests.
4. Run live sandbox containment and multi-repository tests.

### Phase 2: Discovery and Diagnostics

1. Add the CLI adapter.
2. Add `agy_models`.
3. Add `agy_doctor`.
4. Add read-only `agy_plugins`.
5. Add capability/version caching.

### Phase 3: Explicit Context Handoff

1. Add goal-level artifact metadata.
2. Allow completed targets to publish named report paths.
3. Allow subsequent targets to receive selected artifacts explicitly.
4. Preserve conversation isolation by default.

### Phase 4: Interactive Sessions

1. Prototype `--prompt-interactive` outside existing run semantics.
2. Define response-versus-session state transitions.
3. Add structured input acknowledgements.
4. Add permission/question detection.
5. Only then expose conversational `send_text`.

## Required Live Test Matrix

| Area | Scenario |
|---|---|
| Sandbox | Attempt allowed workspace write and denied outside write |
| Add-dir | Review two repositories with one primary and one added directory |
| Models | Reject an unknown model before runner spawn |
| Models | Refresh list after CLI/account change |
| Doctor | Run with missing tmux, missing CLI, unwritable state, and healthy setup |
| Plugins | List zero and multiple imported plugins |
| Plugin validation | Reject paths outside approved roots |
| Exit status | Record zero, nonzero, signal, canceled, and missing status file |
| Interactive | Send two prompts through one persistent session |
| Context | Verify native isolation and explicit artifact handoff |
| Capacity | Mix sandboxed, unrestricted, and interactive runs at global limit |
| Compatibility | Start with and without deprecated `visible_terminal` |

## Priority Recommendation

Implement in this order:

1. Real tmux exit codes and hermetic tests.
2. `sandbox`.
3. `additional_directories`.
4. `agy_models`.
5. `agy_doctor`.
6. Explicit artifact handoff.
7. Read-only plugin diagnostics.
8. Interactive sessions.

This order improves correctness and safety before increasing the bridge's
execution surface.
