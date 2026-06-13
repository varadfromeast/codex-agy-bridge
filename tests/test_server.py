from __future__ import annotations

import inspect

from codex_agy_bridge import server


def test_create_run_defaults_to_visible_terminal():
    parameter = inspect.signature(server.create_run).parameters["visible_terminal"]

    assert parameter.default is True
