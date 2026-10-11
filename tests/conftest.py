import pytest


@pytest.fixture(autouse=True)
def default_antigravity_cli_is_authenticated(monkeypatch, request):
    if request.module.__name__.endswith("test_cli"):
        return
    monkeypatch.setattr(
        "codex_agy_bridge.cli.AntigravityCli.authentication_status",
        lambda _self: {"status": "authenticated"},
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
