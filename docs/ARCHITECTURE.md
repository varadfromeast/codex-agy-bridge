# Product Architecture

## Process Topology

The bridge is a local, process-isolated control plane around the Antigravity
CLI. MCP requests remain short-lived while delegated agent work survives in a
detached Python worker and a persistent tmux session.

```mermaid
flowchart LR
    subgraph Client["Codex client process"]
        Codex["Codex agent"]
    end

    subgraph MCP["Bridge MCP server process"]
        Server["server.py<br/>FastMCP tools"]
        Facade["orchestration.py<br/>compatibility facade"]
        Orchestrator["RunnerOrchestrator<br/>capacity, reservation, control"]
        Request["RunRequest<br/>validation, normalization,<br/>dedup identity, initial state"]
        CLI["AntigravityCli<br/>capabilities, models,<br/>command construction"]
        Diagnostics["diagnostics.py<br/>bounded read-only probes"]
    end

    subgraph Storage["Durable local storage"]
        RunStore["DiskRunStore<br/>JSON state + FileLock"]
        RunFiles["runs/&lt;run-id&gt;/<br/>state, logs, exit code"]
        GoalFiles["goals/&lt;goal-id&gt;/state.json"]
        Active["active/&lt;run-id&gt;<br/>capacity registry"]
    end

    subgraph Worker["Detached runner process per Run"]
        Runner["python -m codex_agy_bridge.runner"]
        Supervisor["RunSupervisor<br/>lifecycle authority"]
        Harvester["TranscriptHarvester<br/>incremental JSONL reader"]
    end

    subgraph Terminal["Persistent execution session"]
        Tmux["tmux server + session"]
        Shell["session shell<br/>records child exit code"]
        Agy["agy CLI<br/>print or prompt-interactive"]
        TerminalApp["Terminal.app<br/>optional attachment"]
    end

    subgraph Antigravity["Antigravity local data"]
        Brain["brain/&lt;conversation-id&gt;/<br/>transcript.jsonl"]
        ConversationMap["cache/last_conversations.json"]
    end

    Codex <-->|"MCP over stdio"| Server
    Server --> Facade --> Orchestrator
    Server --> Diagnostics --> CLI
    Orchestrator --> Request --> CLI
    Orchestrator <-->|"RunStore interface"| RunStore
    RunStore --> RunFiles
    RunStore --> GoalFiles
    RunStore --> Active
    Orchestrator -->|"detached Popen"| Runner
    Runner --> Supervisor -->|"start/kill/is_alive"| Tmux
    Tmux --> Shell --> Agy
    TerminalApp -.->|"tmux attach via AppleScript"| Tmux
    Agy --> Brain
    Agy --> ConversationMap
    Supervisor --> Harvester --> Brain
    Supervisor --> RunFiles
```

## Run Creation And Execution

```mermaid
sequenceDiagram
    participant C as Codex
    participant S as FastMCP server
    participant O as RunnerOrchestrator
    participant R as RunRequest
    participant ST as RunStore
    participant W as Detached runner
    participant T as tmux session
    participant A as agy CLI
    participant B as Antigravity transcript

    C->>S: agy_start / agy_interactive_start
    S->>O: create_run(...)
    O->>R: prepare request
    R->>R: validate + normalize + compute request_key
    O->>ST: lock start registry
    O->>ST: check duplicate and capacity
    O->>R: initial_state(run_id, marker, session)
    O->>ST: persist queued Run
    O->>W: detached Popen(run_id)
    S-->>C: durable run_id

    W->>ST: load persisted Run
    W->>T: start persistent session
    T->>A: launch print or interactive command
    A->>B: append trajectory events
    W->>B: incrementally harvest events
    W->>ST: persist running/completed/failed/canceled

    C->>S: agy_status / agy_transcript / agy_result
    S->>ST: read durable state
    S->>B: read bounded transcript view
    S-->>C: compact observable result
```

## The Important Modules

| Module | Interface responsibility | Why it matters |
| --- | --- | --- |
| `server.py` | Stable MCP tool contract | Keeps Codex-facing schemas small and transport-specific behavior out of the product logic. |
| `run_request.py` | Prepare one immutable Run Request | Concentrates validation, execution-policy checks, deduplication identity, and initial persisted-state construction. |
| `_orchestrator.py` | Reserve and control durable Runs and Goals | Owns capacity, deduplication reservation, persistence coordination, cancellation, and detached process startup. |
| `store.py` | Persist and atomically update Run and Goal state | Disk and memory adapters make the same lifecycle interface available to production and tests. |
| `runner.py` | Detached worker entrypoint | Separates long-lived delegated work from MCP tool timeouts and server restarts. |
| `supervision.py` | Authoritative lifecycle for one Run | Observes completion, timeout, cancellation, conversation discovery, and actual child exit status. |
| `execution.py` / `terminal.py` | Execution Session interface and tmux adapter | Keeps process persistence, input delivery, and Terminal.app attachment behind one seam. |
| `cli.py` | Antigravity CLI compatibility | Localizes changing CLI commands, capability probing, model discovery, and bounded subprocess output. |
| `core.py` / `transcript.py` | Antigravity data compatibility and observation | Isolates assumptions about trajectory files and returns bounded, sanitized progress. |

## Lifecycle Authority

```mermaid
stateDiagram-v2
    [*] --> queued: Run reserved and persisted
    queued --> running: detached runner launches tmux
    running --> completed: stable print marker or response after clean exit
    running --> failed: timeout, nonzero exit, provider failure, lost runner
    running --> cancel_requested: agy_cancel
    cancel_requested --> canceled: session stopped and state finalized
    running --> running: interactive response completes but session remains live
    completed --> [*]
    failed --> [*]
    canceled --> [*]
```

The persisted Run state is authoritative. Transcript events are historical
producer output and may still show a running tool call after cancellation.

## Stability And Extensibility

- **MCP timeout isolation:** the MCP server only reserves work and returns a
  `run_id`; detached runner processes own long execution.
- **Server restart survival:** Run and Goal state, logs, active sentinels, and
  tmux sessions are durable outside the MCP process.
- **Concurrency correctness:** a global start lock makes deduplication and
  capacity reservation atomic before spawning.
- **Terminal persistence:** tmux keeps the CLI alive independently of
  Terminal.app and records the actual child exit code.
- **Compatibility locality:** CLI changes belong in `AntigravityCli`;
  trajectory-format changes belong in `core.py` and `TranscriptHarvester`.
- **Testable seams:** `RunStore`, `ProcessManager`, and `ExecutionSession` have
  production and in-memory adapters. The Run Request interface is directly
  testable without spawning processes.
- **Explicit context:** exact conversation IDs continue native context; Goals
  coordinate Runs but do not implicitly merge conversation context.
