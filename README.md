# codex-agy-bridge

[![CI](https://github.com/varadfromeast/codex-agy-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/varadfromeast/codex-agy-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An observable, resumable MCP bridge that lets Codex delegate work to the
official Antigravity CLI (`agy`) using the user's existing Antigravity login.

<!-- mcp-name: io.github.varadfromeast/codex-agy-bridge -->

`agy --print` is a blocking command. Agent work can outlive an MCP tool timeout,
and its useful progress normally exists only in local Antigravity trajectory
files. This bridge starts a detached worker, returns a durable `run_id`
immediately, and exposes status, transcript, result, cancellation, continuation,
and bounded parallel-goal tools over MCP.

## Status

This project is experimental. It currently targets:

- Codex CLI/app with local stdio MCP servers;
- Antigravity CLI 1.0.8-compatible commands and trajectory files;
- Python 3.11 or newer;
- macOS plus `tmux` for persistent Terminal.app sessions.

Every run executes in a persistent `tmux` session. Terminal.app can be attached
on demand without stopping the run when the window closes. Antigravity's local
storage format is not a stable public API, so compatibility may require updates
when the CLI changes.

## Features

- Starts long-running Antigravity work asynchronously.
- Persists run state and logs across MCP server restarts.
- Returns bounded, sanitized transcript events without private model reasoning.
- Opens each run in a persistent `tmux` session.
- Continues an exact Antigravity `conversation_id`.
- Cancels active process groups.
- Groups named targets with bounded parallelism.
- Deduplicates identical active start requests.
- Keeps separate client-owned MCP server processes from terminating each other.
- Detects authentication, rate-limit, quota, and response-timeout conditions.
- Forwards CLI `--sandbox` and up to 16 `--add-dir` policy hints without
  claiming filesystem containment.
- Discovers models, plugins, capabilities, changelog, and bridge diagnostics.
- Starts experimental persistent `--prompt-interactive` sessions for
  occasional conversational input.

## Install

### Prerequisites

Install:

1. [Codex](https://developers.openai.com/codex/cli/)
2. The official Antigravity CLI (`agy`), authenticated locally
3. [uv](https://docs.astral.sh/uv/getting-started/installation/)
4. `tmux` for persistent terminal sessions:

```bash
brew install tmux
```

Confirm the required commands:

```bash
codex --version
agy --version
uv --version
tmux -V
```

### One-command Codex installation from GitHub

This works before a PyPI release:

```bash
codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- uvx --from git+https://github.com/varadfromeast/codex-agy-bridge \
  codex-agy-bridge
```

Restart Codex, then verify:

```bash
codex mcp get codex-agy-bridge
codex mcp list
```

Remove it with:

```bash
codex mcp remove codex-agy-bridge
```

### Install from a local clone

Use this when developing the bridge:

```bash
git clone https://github.com/varadfromeast/codex-agy-bridge.git
cd codex-agy-bridge
uv sync --extra dev

codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- uv --directory "$PWD" run codex-agy-bridge
```

### Install from PyPI

After the first PyPI release:

```bash
codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- uvx codex-agy-bridge
```

The MCP Registry metadata in `server.json` also depends on that public PyPI
package.

## Configuration

The `codex mcp add` command writes the equivalent of:

```toml
[mcp_servers.codex-agy-bridge]
command = "/absolute/path/to/uv"
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

Useful environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGY_CMD` | `agy` on `PATH` | Exact Antigravity executable |
| `AGY_BRIDGE_STATE_DIR` | `~/.local/state/codex-agy-bridge` | Durable run and goal state |
| `AGY_BRIDGE_AGY_ROOT` | `~/.gemini/antigravity-cli` | Antigravity conversations and trajectories |
| `AGY_BRIDGE_MAX_PARALLEL` | `4` | Global concurrent-run limit |
| `AGY_BRIDGE_COMPLETION_STABILITY_SECONDS` | `150` | Time a final marker must remain stable |

## MCP Tools

| Tool | Purpose |
| --- | --- |
| `agy_start` | Start a new asynchronous conversation and return a `run_id` |
| `agy_interactive_start` | Start an experimental transcript-gated interactive session |
| `agy_continue` | Continue an exact `conversation_id` |
| `agy_status` | Read compact status or diagnostic paths |
| `agy_transcript` | Read bounded progress events |
| `agy_result` | Read the final semantic response |
| `agy_cancel` | Cancel an active run |
| `agy_models` | List models available to the installed CLI |
| `agy_doctor` | Report bounded bridge, CLI, tmux, storage, and run diagnostics |
| `agy_plugins` | List imported plugins without mutating configuration |
| `agy_plugin_validate` | Validate a plugin directory contained by a workspace |
| `agy_changelog` | Read the installed CLI changelog |
| `agy_goal_create` | Create a bridge-owned MCP scheduling container |
| `agy_goal_target_start` | Start one independent scheduler target |
| `agy_goal_status` | Aggregate bridge scheduler target status |
| `agy_target_open_terminal` | Reattach Terminal.app to an existing run |
| `agy_target_send_text` | Queue or send input to an interactive Run only |

Typical call flow:

```text
agy_start
  -> run_id
  -> agy_status / agy_transcript
  -> agy_result
```

Use `agy_continue` only with the exact `conversation_id` returned by a previous
run. Every start and continuation also requires an absolute workspace path.

## How It Works

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the detailed process
topology, lifecycle diagrams, and module responsibilities.

```text
Codex
  |
  | MCP over stdio
  v
server.py
  |
  v
orchestration.py -- persists state --> core.py / state.py
  |
  | starts detached Python worker
  v
runner.py --> supervision.py -- launches --> agy print/interactive
  |                         |
  |                         v
  |                  Antigravity trajectory
  |                         |
  +------ reads progress ---+
  |
  +-- persistent session --> terminal.py --> tmux --> Terminal.app
```

1. `server.py` exposes stable MCP tools and delegates behavior.
2. `orchestration.py` validates requests, enforces parallel limits, deduplicates
   active retries, persists initial state, and starts a detached runner.
3. `cli.py` owns executable discovery, capability probing, read-only commands,
   and run command construction.
4. `runner.py` provides the detached worker entrypoint and process adapters.
5. `supervision.py` launches print or interactive mode and discovers the conversation,
   streams sanitized progress, observes completion, and records terminal state.
6. `core.py` atomically persists JSON and reads Antigravity trajectory JSONL.
7. `terminal.py` owns persistent `tmux` execution and Terminal.app interaction.

Each prompt receives a unique completion marker. A response is considered
complete only after that marker remains the latest response for a stability
window. The marker is removed before results are returned.

## Core Files

Read these in this order to understand the product:

1. `src/codex_agy_bridge/server.py`
   - The public MCP contract.
   - Start here to see every tool and its arguments.
2. `src/codex_agy_bridge/orchestration.py`
   - The product's control plane.
   - Owns goals, concurrency, cancellation, durable reservation, and
     detached-run startup.
3. `src/codex_agy_bridge/run_request.py`
   - The Run Request module.
   - Owns request validation, workspace normalization, execution-policy
     capability checks, deduplication identity, and initial persisted state.
4. `src/codex_agy_bridge/runner.py`
   - The detached-worker entrypoint and process adapter.
   - Owns command construction, tmux launch, and process shutdown.
5. `src/codex_agy_bridge/supervision.py`
   - The lifecycle supervision module for one run.
   - Owns conversation discovery, incremental transcript polling, progress
     monitoring, completion detection, timeouts, and cancellation.
6. `src/codex_agy_bridge/transcript.py`
   - Provides one supervisor-owned `TranscriptHarvester` per conversation.
   - Retains only file identity, byte offset, a partial record, the latest step,
     and the latest completed response.
7. `src/codex_agy_bridge/core.py`
   - The persistence and Antigravity compatibility layer.
   - Owns state paths, atomic writes, stateless transcript reads, response
     extraction, and bounded per-run failure classification.
8. `src/codex_agy_bridge/state.py`
   - The persisted data contracts and run-state machine.
9. `src/codex_agy_bridge/terminal.py`
   - The macOS terminal adapter built on `tmux` and AppleScript.
10. `src/codex_agy_bridge/lifecycle.py`
   - Registration and stale cleanup for client-owned MCP server processes.

Public transcript requests remain stateless full reads. Provider classification
only inspects bounded logs after a run exits without a response; it is never a
launch preflight or persisted health gate.

The most useful tests for learning behavior are:

- `tests/test_mcp_stdio.py`: real MCP initialization and tool discovery.
- `tests/test_detached_run.py`: end-to-end detached run with a fake `agy`.
- `tests/test_orchestration.py`: start deduplication and tmux session creation.
- `tests/test_runner.py`: command construction, completion, and progress output.
- `tests/test_core.py`: transcript parsing and provider-health behavior.
- `tests/test_terminal.py`: `tmux` and Terminal.app command construction.

## State and Observability

Run state survives MCP server restarts under:

```text
~/.local/state/codex-agy-bridge/
  runs/<run-id>/
    state.json
    bridge.log
    agy.log
    agy.stdout.log
    agy.stderr.log
    terminal-progress.log
  goals/<goal-id>/
    state.json
  servers/
    <pid>.json
```

`agy_status(compact=false)` returns these diagnostic paths.
`agy_transcript` returns bounded events by default; full event content is
opt-in and length-capped. Private model reasoning fields are never exposed.

## Execution Risk

Antigravity is an agentic CLI. It can read and write files, execute commands,
and access the network with the current user's privileges. This bridge is not a
sandbox or security boundary.

`dangerously_skip_permissions` defaults to `true` for print-mode tools. Set it
to `false` when unattended execution is not appropriate. `sandbox=true` and
`additional_directories` are CLI policy hints forwarded as `--sandbox` and
`--add-dir`; live testing with Antigravity CLI 1.0.8 showed that they do not
enforce filesystem containment. A workspace scopes conversation context only.

Interactive Runs are experimental and should be used sparingly. The bridge
queues submitted prompts and releases one after observing a completed planner
response in Antigravity's transcript. If those private transcript event
semantics change, delivery may stall. `agy_status` exposes the queue depth and
delivery state.

Goals are an MCP scheduler implemented by this bridge. They are not an
Antigravity feature, and separate targets do not share native conversation
context.

The bridge does not read or copy Antigravity OAuth credentials. It invokes the
installed `agy` binary and reads ordinary local conversation metadata and
trajectory files.

Model-provider calls may cost money. MCP clients should ask for user approval
before starting or continuing a run when cost consent has not already been
given.

## Development

```bash
git clone https://github.com/varadfromeast/codex-agy-bridge.git
cd codex-agy-bridge
uv sync --extra dev
uv run pytest
uv run ruff check .
uv build
```

Run the server directly:

```bash
uv run codex-agy-bridge
```

The server uses stdio transport. Do not print diagnostic text to stdout; it
would corrupt MCP framing.

## Publishing

The official MCP Registry stores metadata, not package artifacts. Publishing
there therefore requires:

1. Build and publish `codex-agy-bridge` to PyPI.
2. Confirm the README contains the matching `mcp-name` verification comment.
3. Update matching versions in `pyproject.toml` and `server.json`.
4. Install the official `mcp-publisher`.
5. Run `mcp-publisher login github`.
6. Run `mcp-publisher publish`.

The registry is currently in preview. See:

- [MCP Registry publishing quickstart](https://modelcontextprotocol.io/registry/quickstart)
- [MCP Registry package types](https://modelcontextprotocol.io/registry/package-types)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp)

## Compatibility

The compatibility boundary is isolated in `core.py`. The current reader expects
Antigravity trajectory JSONL under:

```text
~/.gemini/antigravity-cli/brain/<conversation-id>/
  .system_generated/logs/transcript.jsonl
```

If Antigravity completes its migration to SQLite or a local daemon API, a new
adapter can replace this reader without changing the MCP tool contract.

## License

[MIT](LICENSE)
