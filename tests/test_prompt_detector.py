from __future__ import annotations

import subprocess

import pytest

from codex_agy_bridge import prompt_detector, terminal
from codex_agy_bridge.prompt_detector import PromptDetector


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_detector_emits_attention_after_transcript_prompt_is_stable(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    records = [{"content": "Do you want to proceed?", "step_index": 3}]

    assert detector.inspect(transcript_records=records) is None
    clock.advance(0.49)
    assert detector.inspect(transcript_records=records) is None
    clock.advance(0.02)

    event = detector.inspect(transcript_records=records)

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.source == "transcript"
    assert event.activity_state == "awaiting_user"
    assert event.attention["reason"] == "approval_prompt"
    assert event.attention["prompt"] == "Do you want to proceed?"


def test_detector_dedupes_same_stable_prompt(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    records = [{"content": "Proceed? [y/N]"}]

    detector.inspect(transcript_records=records)
    clock.advance(1.0)

    assert detector.inspect(transcript_records=records) is not None
    assert detector.inspect(transcript_records=records) is None


def test_detector_prefers_transcript_over_terminal_and_capture(tmp_path, monkeypatch):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text("Continue?", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda session: captured.append(session) or "Approve command?",
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)
    records = [{"content": "Do you want to proceed?"}]

    detector.inspect(transcript_records=records)
    clock.advance(1.0)
    event = detector.inspect(transcript_records=records)

    assert event is not None
    assert event.source == "transcript"
    assert captured == []


def test_detector_uses_terminal_log_before_live_capture(tmp_path, monkeypatch):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text("Continue?", encoding="utf-8")
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda _session: "Approve command?",
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.source == "terminal_log"
    assert event.attention["prompt"] == "Continue?"


def test_detector_falls_back_to_live_capture(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda _session, **_kwargs: "Approve tool call?",
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.source == "live_pane"
    assert event.attention["prompt"] == "Approve tool call?"


def test_detector_treats_capture_timeout_as_explicit_state(tmp_path, monkeypatch):
    clock = FakeClock()

    def timeout(_session, **_kwargs):
        raise terminal.TmuxCommandError(
            command=["tmux", "capture-pane", "-p"],
            reason="timeout",
        )

    monkeypatch.setattr(terminal, "capture_pane", timeout)
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    event = detector.inspect()

    assert event is not None
    assert event.kind == "source_unavailable"
    assert event.source == "live_pane"
    assert event.attention["state"] == "timeout"


def test_detector_emits_attention_cleared_when_output_advances_past_prompt(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    prompt = [{"content": "Proceed? [y/N]", "step_index": 2}]
    advanced = [{"content": "working again", "step_index": 3}]

    detector.inspect(transcript_records=prompt)
    clock.advance(1.0)
    assert detector.inspect(transcript_records=prompt).kind == "needs_attention"

    event = detector.inspect(transcript_records=advanced)

    assert event is not None
    assert event.kind == "attention_cleared"
    assert event.activity_state == "working"
    assert event.attention is None


def test_detector_does_not_treat_old_terminal_prompt_as_active(tmp_path):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "Do you want to proceed?\nAccepted. Working again.\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    assert detector.inspect() is None
    clock.advance(1.0)

    assert detector.inspect() is None


def test_detector_treats_approval_menu_after_prompt_as_active(tmp_path):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "\x1b[1mDo you want to proceed?\x1b[m\n"
        "\x1b[94m> 1. Yes\x1b[K\x1b[m\n"
        "  2. Yes, and always allow in this conversation\n"
        "  3. Yes, and always allow (Persist to settings.json)\n"
        "  4. No\n"
        "  ↑/↓ Navigate · tab Amend · e edit command\n"
        "esc to cancel\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.attention["prompt"] == "Do you want to proceed?"


def test_terminal_capture_pane_uses_timeout_and_structured_errors(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="pane\n", stderr="")

    monkeypatch.setattr(terminal.subprocess, "run", run)

    assert terminal.capture_pane("agy-run", timeout_seconds=2.5) == "pane\n"
    assert calls == [
        (
            ["tmux", "capture-pane", "-p", "-t", "agy-run"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": 2.5,
            },
        )
    ]


def test_terminal_capture_pane_timeout_is_structured(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(terminal.subprocess, "run", run)

    with pytest.raises(terminal.TmuxCommandError) as error:
        terminal.capture_pane("agy-run", timeout_seconds=1)

    assert error.value.reason == "timeout"
    assert error.value.command == ["tmux", "capture-pane", "-p", "-t", "agy-run"]


def test_approval_patterns_are_explicit_and_deduped():
    assert prompt_detector.APPROVAL_PATTERNS == [
        r"Do you want to proceed\?",
        r"Proceed\? \[y/N\]",
        r"Continue\?",
        r"Approve .*\?",
    ]
