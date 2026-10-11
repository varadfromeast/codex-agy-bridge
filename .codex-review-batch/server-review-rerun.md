# Code Review Report: `src/codex_agy_bridge/server.py`

## Overview
This report provides a meticulous review of `src/codex_agy_bridge/server.py` in the `codex-agy-bridge` repository. The analysis focuses on correctness bugs, MCP/API contract violations, concurrency/race conditions, unsafe behaviors, and error-handling gaps.

## Findings

### Finding 1: Unsafe Behavior - Exposure of Server Metadata via Loose Directory Permissions
* **Severity**: Medium
* **Exact Line(s)**: Line 514 (and related registration logic in `lifecycle.py` lines 19–21)
* **Trigger**: Initializing the MCP server by running `main()` (which calls `STATE_ROOT.mkdir(parents=True, exist_ok=True)`).
* **Demonstrated Impact**:
  The `STATE_ROOT` directory and its subdirectories (like `servers/` used for process registration) are created using default `mkdir` permissions (umask-based, typically `0o755` or `0o775` on macOS/Linux), rather than owner-only permissions.
  This allows other local users on a shared machine to read process registration JSON files (which contain sensitive metadata such as PIDs, parent PIDs, and startup timestamps), posing a metadata exposure security risk.
* **Specific Fix**:
  Pass `mode=0o700` to `.mkdir()` when creating the state directory, ensuring only the owner can read or list the directory contents:
  ```python
  STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
  ```

---

### Finding 2: Error-Handling Gap - Potential `RecursionError` on Circular Dictionary Outputs in `sanitize_mcp_output`
* **Severity**: Low
* **Exact Line(s)**: Lines 21–34
* **Trigger**: An MCP tool returning a dictionary, list, or tuple container that contains self-referencing (circular) object references.
* **Demonstrated Impact**:
  `sanitize_mcp_output` recursively iterates through dictionaries, lists, and tuples to strip control characters. Because it does not track already visited objects, a circular structure will lead to infinite recursion, raising a `RecursionError` and crashing the tool execution.
* **Specific Fix**:
  Track visited object IDs using a set to detect and short-circuit circular references:
  ```python
  def sanitize_mcp_output(value: Any, memo: set[int] | None = None) -> Any:
      if memo is None:
          memo = set()
      val_id = id(value)
      if val_id in memo:
          return value
      if isinstance(value, str):
          return terminal.strip_control_sequences(value)
      if isinstance(value, dict):
          memo.add(val_id)
          res = {
              sanitize_mcp_output(key, memo): sanitize_mcp_output(item, memo)
              for key, item in value.items()
          }
          memo.remove(val_id)
          return res
      if isinstance(value, list):
          memo.add(val_id)
          res = [sanitize_mcp_output(item, memo) for item in value]
          memo.remove(val_id)
          return res
      if isinstance(value, tuple):
          memo.add(val_id)
          res = tuple(sanitize_mcp_output(item, memo) for item in value)
          memo.remove(val_id)
          return res
      return value
  ```

---

### Finding 3: Correctness Bug - Validation Crash on `None`/`null` for `dangerously_skip_permissions` in `agy_goal`
* **Severity**: Low
* **Exact Line(s)**: Line 455
* **Trigger**: A client explicitly invoking the `agy_goal` tool with action `"create"` and setting the optional argument `dangerously_skip_permissions` to `None` (or `null` via MCP JSON-RPC).
* **Demonstrated Impact**:
  The expression `dangerously_skip_permissions=bool(dangerously_skip_permissions)` coerces `None` to `False`. This forces `dangerously_skip_permissions` to be `False` when calling `orchestration.create_goal`, which fails the strict validation check in `goal_scheduler.py` (`if dangerously_skip_permissions is not True: raise ValueError(...)`), crashing the tool call instead of defaulting it to `True`.
* **Specific Fix**:
  Only coerce `dangerously_skip_permissions` to `bool` if it is not `None` (defaulting it to `True` if it is `None`):
  ```python
  dangerously_skip_permissions=True if dangerously_skip_permissions is None else bool(dangerously_skip_permissions)
  ```

---

## Commands & Tests Run
The following commands were run in `/Users/varad/V/repo/codex-agy-bridge` to perform dynamic validation and static analysis:
1. **Running Python unit tests**:
   ```bash
   .venv/bin/pytest
   ```
   *Result*: 375 passed, 9 skipped in 27.45s.
2. **Linting and style check**:
   ```bash
   .venv/bin/ruff check src/codex_agy_bridge/server.py
   ```
   *Result*: All checks passed.
3. **Type checking**:
   ```bash
   .venv/bin/mypy src/codex_agy_bridge/server.py
   ```
   *Result*: Success: no issues found in 1 source file.
4. **Validating tool argument configuration dynamically**:
   ```bash
   .venv/bin/python -c '
   from codex_agy_bridge.server import mcp
   tool = mcp._tool_manager.get_tool("agy_login")
   print("model_config:", tool.fn_metadata.arg_model.model_config)
   '
   ```
   *Result*: Verified that `extra='forbid'` is successfully applied to MCP tools via `StrictFastMCP`.
