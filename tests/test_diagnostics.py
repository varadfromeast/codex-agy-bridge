from __future__ import annotations

import pytest

from codex_agy_bridge import diagnostics


class FakeCli:
    executable = "/usr/local/bin/agy"

    def version(self):
        return "1.0.8"

    def models(self, *, refresh=False):
        assert refresh is True
        return ["Model A", "Model B"]

    def plugins(self):
        return [{"name": "alpha", "raw": "alpha"}]

    def capabilities(self):
        return type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )()

    def validate_plugin(self, path):
        return {"valid": True, "output": str(path)}


def test_models_report_includes_default_and_observation_time():
    result = diagnostics.models(cli=FakeCli(), refresh=True)

    assert result["cli_version"] == "1.0.8"
    assert result["default_model"] == "Model A"
    assert result["models"] == ["Model A", "Model B"]
    assert result["observed_at"]


def test_plugin_validation_is_contained_to_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugin = workspace / "plugin"
    plugin.mkdir()

    assert diagnostics.validate_plugin(
        path=str(plugin),
        workspace=str(workspace),
        cli=FakeCli(),
    )["valid"]

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="inside workspace"):
        diagnostics.validate_plugin(
            path=str(outside),
            workspace=str(workspace),
            cli=FakeCli(),
        )


def test_doctor_isolates_model_discovery_failure(monkeypatch, tmp_path):
    class ModelFailureCli(FakeCli):
        def models(self, *, refresh=False):
            raise TimeoutError("model discovery stalled")

    monkeypatch.setattr(diagnostics.core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(diagnostics.core, "AGY_ROOT", tmp_path / "agy")

    result = diagnostics.doctor(cli=ModelFailureCli())

    assert result["cli"]["models"] is None
    assert "model discovery stalled" in result["cli"]["errors"]["models"]
    assert result["cli"]["version"] == "1.0.8"
    assert result["cli"]["plugins"] == [{"name": "alpha", "raw": "alpha"}]
    assert result["storage"]["state_root"] == str(tmp_path / "state")
    assert result["capacity"]["configured_parallel_limit"]
