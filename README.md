# codex-agy-bridge

An observable and resumable MCP bridge that lets Codex delegate work to the
official Antigravity CLI (`agy`) using the user's existing Antigravity login.

## Why

`agy --print` is designed as a blocking one-shot command. Long-running agent
work can exceed the caller's timeout, while useful progress remains available
only in Antigravity's local trajectory files.

This bridge starts each job in a detached runner and exposes six MCP tools:

- `agy_start`
- `agy_continue`
- `agy_status`
- `agy_transcript`
- `agy_result`
- `agy_cancel`

It also exposes lightweight orchestration tools:

- `agy_goal_create`
- `agy_goal_target_start`
- `agy_goal_status`
- `agy_target_open_terminal`

New and continued runs may select an installed Antigravity model explicitly
with the optional `model` argument instead of changing global CLI settings.
The default is `Gemini 3.5 Flash (Medium)`.

Runs open in a persistent `tmux` session by default. Terminal.app attaches to
that session for user visibility; closing the terminal only detaches the view
and does not stop the target. Set `visible_terminal=false` for headless runs.

Goals group named targets and permit bounded parallel work. Parallelism is
capped at three globally and may be lowered per goal.

## Project Scoping

Every start and continuation requires an absolute `workspace` directory.
Starting with the same workspace creates a project-scoped Antigravity
conversation; continuing also requires its exact `conversation_id`, preventing
follow-up work from drifting into another project's conversation.

For example, all work for a repository can consistently use:

```text
/Users/you/repositories/your-project
```

The bridge appends a unique internal completion marker to each prompt. When the
marker remains the latest response through a stability window, the detached
runner records the result and terminates the CLI process instead of waiting
indefinitely for process exit. This avoids treating an intermediate response
followed by a scheduled wait as completion. The marker is removed from public
tool results.

## Architecture

- `server` owns only the MCP tool interface and translates calls to
  `orchestration`.
- `orchestration` owns run and goal lifecycle behavior independently of MCP.
- `state` defines and validates persisted run and goal contracts while allowing
  older state files to omit fields added by newer versions.
- `terminal` owns tmux execution and Terminal.app attachment.
- `runner` owns completion monitoring for one detached Antigravity process.
- `core` owns JSON persistence and Antigravity transcript reading.

Run state and logs survive MCP server restarts under:

```text
~/.local/state/codex-agy-bridge/runs/
```

`agy_transcript` defaults to bounded progress metadata: step identifiers,
statuses, event types, tool names, and short error summaries. Raw event content
is available only when `include_content=true` and remains length-capped. Private
model reasoning fields are never exposed.

`agy_status` returns a small status envelope by default. Set `compact=false`
only when process identifiers and artifact paths are needed for diagnosis.
Compact status also reports the latest semantic event and provider-health
classification when Antigravity logs expose authentication, rate-limit, or
quota signals.

## Safety

Antigravity is an agentic CLI. It can read and write files, run commands, and
access the network with the current user's privileges. The bridge does not
provide a security boundary.

`dangerously_skip_permissions` defaults to `false`, but Antigravity print mode
may still execute tools without an interactive approval channel. The workspace
is context, not a filesystem or network boundary. Use only trusted prompts and
trusted local content; use an OS-level container or VM for actual isolation.

The bridge never reads or copies Antigravity OAuth credentials. It invokes the
installed `agy` binary and reads ordinary conversation metadata and trajectory
files from `~/.gemini/antigravity-cli`.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Run the MCP server:

```bash
uv run codex-agy-bridge
```

## Codex Configuration

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.codex-agy-bridge]
command = "/Users/you/.local/bin/uv"
args = [
  "--directory",
  "/absolute/path/to/codex-agy-bridge",
  "run",
  "codex-agy-bridge",
]
startup_timeout_sec = 30
tool_timeout_sec = 30

[mcp_servers.codex-agy-bridge.env]
AGY_CMD = "/absolute/path/to/agy"
```

Restart Codex after changing MCP configuration.

## Compatibility

The initial implementation targets Antigravity CLI 1.0.7. It reads the JSONL
trajectory currently written under:

```text
~/.gemini/antigravity-cli/brain/<conversation-id>/
  .system_generated/logs/transcript.jsonl
```

Antigravity is also migrating conversations to SQLite. The bridge keeps this
reader isolated so a future SQLite or local-daemon adapter can be added without
changing the MCP tool contract.

MCP Tasks were considered but remain experimental in the 2025-11-25 protocol
and are moving to an extension. This bridge uses the stable explicit-handle
pattern instead: `agy_start` returns a durable `run_id` that is passed to
status, transcript, result, and cancellation tools.

## Design References

- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Tasks specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp)
- [Antigravity MCP bridge prior art](https://github.com/SinanTufekci/Claude-Code-Antigravity-CLI-MCP-Server)
- [Antigravity trajectory reader prior art](https://github.com/mjacobs/agy-reader)
