from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codex_agy_bridge import session_events
from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.run_observation import RunObservation
from codex_agy_bridge.store import MemoryRunStore


def _state(run_id: str, *, status: str = "running") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": status,
        "conversation_id": None,
        "execution_surface": "headless",
    }


@pytest.mark.parametrize(
    "resolving_kind",
    ["attention_cleared", "mcp_input_submitted", "mcp_input_delivered"],
)
def test_wait_folds_resolved_attention_and_advances_cursor(
    tmp_path: Path,
    resolving_kind: str,
) -> None:
    run_id = "run-stale-attention"
    state = _state(run_id)
    run_dir = tmp_path / "runs" / run_id
    started = session_events.append_event(run_dir, "run_started")
    session_events.append_event(
        run_dir,
        "needs_attention",
        {
            "category": "approval_prompt",
            "observed": {"prompt": "Continue?", "suggested_inputs": ["y", "n"]},
        },
    )
    # Put the resolution beyond the old filtered wait page boundary. Wait must
    # fold the durable order rather than returning the now-stale prompt.
    for _ in range(100):
        session_events.append_event(run_dir, "terminal_output_observed")
    resolved = session_events.append_event(run_dir, resolving_kind)
    loads: list[str] = []

    def load_state(value: str) -> dict[str, Any]:
        loads.append(value)
        return state

    observation = RunObservation(
        state_root=tmp_path,
        load_state=load_state,  # type: ignore[arg-type]
        run_dir=lambda _value: run_dir,
    )

    result = observation.wait(
        [run_id],
        after={run_id: started["event_id"]},
        timeout_seconds=0,
    )

    assert result["matched"] is False
    assert result["events"] == []
    assert result["next_after"][run_id] == resolved["event_id"]
    assert loads == [run_id]

    repeated = observation.wait(
        [run_id],
        after=result["next_after"],
        timeout_seconds=0,
    )
    assert repeated["matched"] is False
    assert repeated["events"] == []


def test_observe_accepts_integer_cursor_and_loads_state_once(tmp_path: Path) -> None:
    run_id = "run-observe-cursor"
    state = _state(run_id)
    run_dir = tmp_path / "runs" / run_id
    first = session_events.append_event(run_dir, "run_started")
    second = session_events.append_event(run_dir, "terminal_output_observed")
    loads: list[str] = []

    def load_state(value: str) -> dict[str, Any]:
        loads.append(value)
        return state

    observation = RunObservation(
        state_root=tmp_path,
        load_state=load_state,  # type: ignore[arg-type]
        run_dir=lambda _value: run_dir,
    )

    result = observation.observe(
        [run_id],
        after={run_id: int(first["run_seq"])},
    )["runs"][run_id]

    assert result["events"] == [second]
    assert result["cursor"]["event_id"] == second["run_seq"]
    assert loads == [run_id]


class _ObservationSpy:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _call(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, args, kwargs))
        return {"delegated": name}

    def transcript(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("transcript", *args, **kwargs)

    def observe(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("observe", *args, **kwargs)

    def terminal_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("terminal_snapshot", *args, **kwargs)

    def result(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("result", *args, **kwargs)

    def result_read(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("result_read", *args, **kwargs)

    def wait(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("wait", *args, **kwargs)

    def result_artifact_path(self, run_id: str) -> Path:
        self.calls.append(("result_artifact_path", (run_id,), {}))
        return self.tmp_path / "delegated-result.txt"


def test_orchestrator_observation_methods_delegate_unchanged(tmp_path: Path) -> None:
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=MemoryRunStore(),
    )
    spy = _ObservationSpy(tmp_path)
    orchestrator._run_observation = spy  # type: ignore[assignment]

    assert orchestrator.transcript(
        "run-1",
        after_step=3,
        limit=4,
        include_content=True,
        max_content_chars=9,
    ) == {"delegated": "transcript"}
    assert orchestrator.observe(
        ["run-1"],
        after={"run-1": 2},
        include_terminal_tail=True,
    ) == {"delegated": "observe"}
    assert orchestrator.terminal_snapshot(
        "run-1", max_chars=7, timeout_seconds=0.25
    ) == {"delegated": "terminal_snapshot"}
    assert orchestrator.result("run-1") == {"delegated": "result"}
    assert orchestrator.result_read("run-1", offset_bytes=5, max_bytes=8) == {
        "delegated": "result_read"
    }
    assert orchestrator.wait(
        ["run-1"],
        condition="any_event",
        after={"run-1": "4"},
        timeout_seconds=6,
    ) == {"delegated": "wait"}
    assert orchestrator.result_artifact_path("run-1") == (
        tmp_path / "delegated-result.txt"
    )

    assert spy.calls == [
        (
            "transcript",
            ("run-1",),
            {
                "after_step": 3,
                "limit": 4,
                "include_content": True,
                "max_content_chars": 9,
            },
        ),
        (
            "observe",
            (["run-1"],),
            {"after": {"run-1": 2}, "include_terminal_tail": True},
        ),
        (
            "terminal_snapshot",
            ("run-1",),
            {"max_chars": 7, "timeout_seconds": 0.25},
        ),
        ("result", ("run-1",), {}),
        ("result_read", ("run-1",), {"offset_bytes": 5, "max_bytes": 8}),
        (
            "wait",
            (["run-1"],),
            {
                "condition": "any_event",
                "after": {"run-1": "4"},
                "timeout_seconds": 6,
                "max_slice_seconds": 120,
            },
        ),
        ("result_artifact_path", ("run-1",), {}),
    ]
