from __future__ import annotations

import inspect

from codex_agy_bridge import server


def test_create_run_requires_tmux_without_execution_mode_flag():
    parameters = inspect.signature(server.create_run).parameters

    assert "visible_terminal" not in parameters
    assert parameters["dangerously_skip_permissions"].default is True


def test_run_input_defaults_to_press_enter_and_accepts_preconditions():
    parameters = inspect.signature(server.agy_run_input).parameters

    assert parameters["enter"].default is True
    assert parameters["expected_event_key"].default is None
    assert parameters["expected_transcript_step"].default is None


def test_run_result_uses_optional_byte_offsets():
    parameters = inspect.signature(server.agy_run_result).parameters

    assert list(parameters) == ["run_id", "offset_bytes", "max_bytes"]
    assert parameters["offset_bytes"].default is None
    assert parameters["max_bytes"].default == 65_536


def test_run_wait_accepts_run_batch_and_cursor_map():
    parameters = inspect.signature(server.agy_run_wait).parameters

    assert list(parameters) == [
        "run_ids",
        "condition",
        "after",
        "timeout_seconds",
    ]
    assert parameters["condition"].default == "any_attention"
    assert parameters["after"].default is None
    assert parameters["timeout_seconds"].default == 900
