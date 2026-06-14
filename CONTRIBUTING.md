# Contributing

## Setup

```bash
git clone https://github.com/varadfromeast/codex-agy-bridge.git
cd codex-agy-bridge
uv sync --extra dev
```

## Before opening a pull request

```bash
uv run ruff check .
uv run pytest
uv build
```

Add regression tests for behavior changes. Keep the MCP tool contract stable
unless the pull request explicitly documents a breaking change.

Do not include Antigravity credentials, conversation contents, trajectory
files, or state from `~/.local/state/codex-agy-bridge`.

## Scope

Issues and pull requests are especially useful for:

- Antigravity CLI compatibility updates;
- non-macOS terminal adapters;
- safer process lifecycle behavior;
- transcript parser fixtures;
- MCP Registry and packaging improvements.
