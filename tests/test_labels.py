from __future__ import annotations

from codex_agy_bridge import labels


def test_session_label_prefers_safe_human_seed_and_run_suffix():
    assert (
        labels.session_label(
            seed="Tests: Pytest / Integration!",
            run_id="20260616T010203Z-a1b2c3d4",
        )
        == "agy-tests-pytest-integration-a1b2c3d4"
    )


def test_session_label_falls_back_when_seed_has_no_word_characters():
    assert (
        labels.session_label(seed="!!!", run_id="20260616T010203Z-deadbeef")
        == "agy-run-deadbeef"
    )

def test_session_label_sanitizes_only_bounded_seed_prefix(monkeypatch):
    observed = []

    def fake_sub(pattern, replacement, value):
        observed.append(value)
        return "x" * 100

    monkeypatch.setattr(labels.re, "sub", fake_sub)

    assert labels._sanitize("a" * 100_000) == "x" * labels.MAX_LABEL_STEM_CHARS
    assert observed[0] == "a" * labels.MAX_LABEL_SEED_CHARS
