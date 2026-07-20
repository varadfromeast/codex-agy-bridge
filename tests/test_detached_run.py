from __future__ import annotations

import stat
import time

from codex_agy_bridge import core, orchestration, server, terminal


def test_detached_runner_recovers_conversation_and_result(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    agy_root = tmp_path / "agy-root"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(
        """#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path

if "--help" in sys.argv:
    print("--prompt-interactive")
    raise SystemExit(0)

root = Path(os.environ["AGY_BRIDGE_AGY_ROOT"])
conversation_id = "fake-conversation"
completion_marker = re.search(
    r"AGY_RUN_COMPLETE_[0-9a-f]+",
    sys.argv[-1],
).group(0)
mapping = root / "cache" / "last_conversations.json"
mapping.parent.mkdir(parents=True, exist_ok=True)
mapping.write_text(json.dumps({str(Path.cwd()): conversation_id}))
transcript = (
    root / "brain" / conversation_id
    / ".system_generated" / "logs" / "transcript.jsonl"
)
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.write_text(
    json.dumps({
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "content": sys.argv[-1],
    })
    + "\\n"
    + json.dumps({
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "content": "fake result\\n" + completion_marker,
    })
    + "\\n"
)
""",
        encoding="utf-8",
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv("AGY_CMD", str(fake_agy))
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(state_root))
    monkeypatch.setenv("AGY_BRIDGE_AGY_ROOT", str(agy_root))
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(core, "AGY_ROOT", agy_root)
    monkeypatch.setattr(
        core,
        "LAST_CONVERSATIONS",
        agy_root / "cache" / "last_conversations.json",
    )
    monkeypatch.setattr(core, "BRAIN_DIR", agy_root / "brain")
    monkeypatch.setattr(server, "STATE_ROOT", state_root)
    monkeypatch.setattr(orchestration, "STATE_ROOT", state_root)
    monkeypatch.setattr(terminal, "attach", lambda _session, *, check=False: None)

    state = server.create_run(
        prompt="return a fake result",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=None,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = core.load_state(state["run_id"])
        if state["status"] in core.TERMINAL_STATUSES:
            break
        time.sleep(0.1)

    assert state["status"] == "completed"
    assert state["conversation_id"] == "fake-conversation"
    assert state["result"] == "fake result"
    assert not core.process_alive(state["agy_pid"])
