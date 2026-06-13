from __future__ import annotations

import inspect

from codex_agy_bridge import server


def test_create_run_defaults_to_visible_terminal():
    parameters = inspect.signature(server.create_run).parameters

    assert parameters["visible_terminal"].default is True
    assert parameters["dangerously_skip_permissions"].default is True
