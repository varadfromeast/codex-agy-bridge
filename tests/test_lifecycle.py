from __future__ import annotations

import json

from codex_agy_bridge import lifecycle


def test_register_server_instance_terminates_previous_live_server(
    monkeypatch, tmp_path
):
    pidfile = tmp_path / lifecycle.PIDFILE_NAME
    pidfile.write_text(json.dumps({"pid": 1234}), encoding="utf-8")
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 5678)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 42)
    monkeypatch.setattr(lifecycle, "_process_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(
        lifecycle,
        "_terminate_pid",
        lambda pid: signals.append((pid, lifecycle.signal.SIGTERM)),
    )

    lifecycle.register_server_instance(tmp_path)

    assert signals == [(1234, lifecycle.signal.SIGTERM)]
    assert json.loads(pidfile.read_text(encoding="utf-8"))["pid"] == 5678


def test_clear_pidfile_only_removes_matching_server(tmp_path):
    pidfile = tmp_path / lifecycle.PIDFILE_NAME
    pidfile.write_text(json.dumps({"pid": 1234}), encoding="utf-8")

    lifecycle._clear_pidfile(pidfile, 5678)

    assert pidfile.exists()

    lifecycle._clear_pidfile(pidfile, 1234)

    assert not pidfile.exists()
