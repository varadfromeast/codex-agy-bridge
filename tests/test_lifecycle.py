from __future__ import annotations

from codex_agy_bridge import lifecycle


def test_register_server_instance_does_not_terminate_sibling_server(
    monkeypatch, tmp_path
):
    servers = tmp_path / lifecycle.SERVERS_DIR_NAME
    servers.mkdir()
    (servers / "1234.json").write_text('{"pid": 1234}', encoding="utf-8")

    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 5678)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 42)
    monkeypatch.setattr(lifecycle, "_process_alive", lambda pid: pid == 1234)

    lifecycle.register_server_instance(tmp_path)

    assert (servers / "1234.json").exists()
    assert (servers / "5678.json").exists()


def test_register_server_instance_removes_stale_registrations(monkeypatch, tmp_path):
    servers = tmp_path / lifecycle.SERVERS_DIR_NAME
    servers.mkdir()
    (servers / "1234.json").write_text('{"pid": 1234}', encoding="utf-8")

    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 5678)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 42)
    monkeypatch.setattr(lifecycle, "_process_alive", lambda _pid: False)

    lifecycle.register_server_instance(tmp_path)

    assert not (servers / "1234.json").exists()
    assert (servers / "5678.json").exists()
