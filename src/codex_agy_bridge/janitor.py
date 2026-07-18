"""Background watchdog and log sweeper services for running agents."""

from __future__ import annotations

import json
import shutil
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_agy_bridge.execution import TmuxSession
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.state import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    RunState,
)
from codex_agy_bridge.store import RunStore

QUEUED_RUNNER_START_GRACE_SECONDS = 300.0
SWEEP_PRESERVED_FILES = {"state.json", "state.lock", "final-result.txt"}


class RunJanitor:
    """Watchdog service to clean orphaned active runs and sweep old log files."""

    def __init__(
        self,
        state_root: Path,
        store: RunStore,
        process_manager: ProcessManager,
    ) -> None:
        """Initialize RunJanitor.

        Args:
            state_root: Root directory of state storage
            store: RunStore persistence seam
            process_manager: ProcessManager execution seam
        """
        self.state_root = state_root
        self.store = store
        self.process_manager = process_manager

    def update_state(self, run_id: str, **changes: Any) -> RunState:
        """Transactional helper to update run state during cleanup.

        Args:
            run_id: Unique run identifier
            changes: State fields to change

        Returns:
            The updated, validated RunState dict
        """
        return self.store.update_run(run_id, changes, require_active=True)

    def clean(self, max_log_age_days: int = 7) -> None:
        """Reclaim dead runs and delete terminal log folders older than X days.

        Args:
            max_log_age_days: Age threshold in days to delete completed run logs
        """
        active_dir = self.state_root / "active"
        if active_dir.exists():
            for path in list(active_dir.iterdir()):
                if path.name.startswith(".") or not path.is_file():
                    continue
                run_id = path.name
                try:
                    state = self.store.get_run(run_id)
                except Exception:
                    with suppress(OSError):
                        path.unlink()
                    continue
                if state.get("status") not in ACTIVE_STATUSES:
                    with suppress(OSError):
                        path.unlink()
                    continue

                runner_pid = state.get("runner_pid")
                agy_pid = state.get("agy_pid")

                is_stale = False
                age_seconds: float | None = None
                created_at_str = state.get("created_at")
                if created_at_str:
                    try:
                        created_at = datetime.fromisoformat(
                            created_at_str.replace("Z", "+00:00")
                        )
                        age_seconds = (datetime.now(UTC) - created_at).total_seconds()
                        if age_seconds > 60:
                            is_stale = True
                    except Exception:
                        is_stale = True
                else:
                    is_stale = True

                if is_stale:
                    if (
                        state.get("status") == "queued"
                        and not runner_pid
                        and not agy_pid
                        and age_seconds is not None
                        and age_seconds <= QUEUED_RUNNER_START_GRACE_SECONDS
                    ):
                        continue
                    runner_alive = (
                        self.process_manager.is_alive(runner_pid)
                        if runner_pid
                        else False
                    )
                    agy_alive = (
                        self.process_manager.is_alive(agy_pid) if agy_pid else False
                    )
                    if not runner_alive and not agy_alive:
                        tmux_session = state.get("tmux_session")
                        if (
                            isinstance(tmux_session, str)
                            and tmux_session.endswith(run_id[-8:])
                        ):
                            TmuxSession(
                                self.state_root / "runs" / run_id,
                                session_name=tmux_session,
                            ).kill()
                        self.update_state(
                            run_id,
                            status="failed",
                            error="runner process died or failed to start",
                            finished_at=datetime.now(UTC).isoformat(),
                        )

        # Log Sweeper
        runs_root = self.state_root / "runs"
        if runs_root.exists():
            now_ts = time.time()
            for run_path in list(runs_root.iterdir()):
                if not run_path.is_dir():
                    continue
                try:
                    mtime = run_path.stat().st_mtime
                    age_days = (now_ts - mtime) / 86400
                    if age_days > max_log_age_days:
                        state_file = run_path / "state.json"
                        if state_file.is_file():
                            try:
                                state_data = json.loads(
                                    state_file.read_text(encoding="utf-8")
                                )
                                if state_data.get("status") in TERMINAL_STATUSES:
                                    for child in run_path.iterdir():
                                        if child.name in SWEEP_PRESERVED_FILES:
                                            continue
                                        if child.is_dir():
                                            shutil.rmtree(child)
                                        else:
                                            child.unlink()
                            except Exception:
                                # Durable state is evidence. Preserve the entire
                                # run directory when state cannot be classified.
                                continue
                        else:
                            shutil.rmtree(run_path)
                except Exception:
                    pass
