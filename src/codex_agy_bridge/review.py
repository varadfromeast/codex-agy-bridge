"""Review-specific task wrappers and artifact validation."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from codex_agy_bridge.state import RunState

REVIEW_SCHEMA = "agy.review.v1"
REVIEW_TASK_KINDS = {"review_commit", "review_branch"}
REVIEW_ARTIFACT = {
    "kind": "review_json",
    "schema": REVIEW_SCHEMA,
}


def default_output_file(workspace: str) -> str:
    root = Path(workspace).expanduser().resolve()
    directory = root / ".agy-bridge-artifacts" / "reviews"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{uuid.uuid4().hex}.json")


def normalize_output_file(workspace: str, output_file: str | None) -> str:
    if output_file is None:
        return default_output_file(workspace)
    if "\x00" in output_file:
        raise ValueError("output_file must not contain NUL bytes")
    if not output_file.strip():
        raise ValueError("output_file must not be empty")
    root = Path(workspace).expanduser().resolve()
    path = Path(output_file).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("output_file must resolve inside the workspace") from error
    if len(os.fsencode(path)) > 4096:
        raise ValueError("output_file path exceeds 4096 bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def launch_response(state: RunState) -> dict[str, Any]:
    return {
        "status": state["status"],
        "run_id": state["run_id"],
        "output_file": state.get("review_output_file"),
        "expected_artifact": dict(REVIEW_ARTIFACT),
        "next": {
            "result_tool": "agy_review_result",
            "wait_tool": "agy_run_wait",
            "max_wait_seconds": 120,
            "poll_interval_seconds": 60,
            "advice": (
                "Poll agy_review_result or the output_file directly for long "
                "reviews; MCP wait/request timeouts only mean the observer "
                "disconnected, not that the review failed."
            ),
        },
    }


def commit_prompt(
    *,
    commit: str,
    issue: str,
    scope_paths: list[str] | None,
    output_file: str,
) -> str:
    commit = _required_text(commit, "commit")
    return _review_prompt(
        title=f"Review commit: {commit}",
        issue=issue,
        scope_paths=scope_paths,
        output_file=output_file,
        change_contract=(
            "Inspect the repository and review the exact changes introduced by "
            f"commit `{commit}`. Let git determine the changed files."
        ),
    )


def branch_prompt(
    *,
    issue: str,
    scope_paths: list[str] | None,
    base_ref: str | None,
    include_untracked: bool,
    output_file: str,
) -> str:
    base = _optional_text(base_ref, "base_ref")
    source = "staged, unstaged, and untracked"
    if not include_untracked:
        source = "staged and unstaged"
    base_line = f"Use base ref: {base}." if base else "Determine the branch base."
    return _review_prompt(
        title="Review current branch work",
        issue=issue,
        scope_paths=scope_paths,
        output_file=output_file,
        change_contract=(
            f"Review committed branch changes plus {source} working tree changes. "
            f"{base_line} Record the reviewed change sources in the artifact."
        ),
    )


def result(state: RunState, *, provider_health: dict[str, Any]) -> dict[str, Any]:
    if state.get("task_kind") not in REVIEW_TASK_KINDS:
        return {
            "status": "failed_validation",
            "summary": None,
            "review": None,
            "artifact": None,
            "validation_errors": ["run was not started by a review tool"],
            "run": _run_metadata(state, provider_health),
        }
    path_text = state.get("review_output_file")
    if not isinstance(path_text, str) or not path_text:
        return {
            "status": "failed_validation",
            "summary": None,
            "review": None,
            "artifact": None,
            "validation_errors": ["review_output_file is missing from run state"],
            "run": _run_metadata(state, provider_health),
        }
    path = Path(path_text)
    artifact = _artifact_base(path)
    active = state["status"] in {"queued", "running", "cancel_requested"}
    if not path.is_file():
        if active:
            return {
                "status": state["status"],
                "summary": None,
                "review": None,
                "artifact": {
                    **artifact,
                    "exists": False,
                    "valid": False,
                },
                "validation_errors": [],
                "run": _run_metadata(state, provider_health),
            }
        errors = ["review artifact was not written"]
        return {
            "status": "failed_validation",
            "summary": None,
            "review": None,
            "artifact": {
                **artifact,
                "exists": False,
                "valid": False,
                "validation_errors": errors,
            },
            "validation_errors": errors,
            "run": _run_metadata(state, provider_health),
        }
    artifact["exists"] = True
    artifact["total_bytes"] = path.stat().st_size
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        parse_error = f"{error.msg} at line {error.lineno} column {error.colno}"
        if active:
            return {
                "status": state["status"],
                "summary": None,
                "review": None,
                "artifact": {
                    **artifact,
                    "valid": False,
                    "parse_error": parse_error,
                },
                "validation_errors": [],
                "run": _run_metadata(state, provider_health),
            }
        return {
            "status": "failed_validation",
            "summary": None,
            "review": None,
            "artifact": {
                **artifact,
                "valid": False,
                "parse_error": parse_error,
            },
            "validation_errors": [parse_error],
            "run": _run_metadata(state, provider_health),
        }
    errors = validate_artifact(review)
    if errors:
        if active:
            return {
                "status": state["status"],
                "summary": None,
                "review": review if isinstance(review, dict) else None,
                "artifact": {
                    **artifact,
                    "valid": False,
                    "validation_errors": errors,
                },
                "validation_errors": [],
                "run": _run_metadata(state, provider_health),
            }
        return {
            "status": "failed_validation",
            "summary": None,
            "review": review if isinstance(review, dict) else None,
            "artifact": {
                **artifact,
                "valid": False,
                "validation_errors": errors,
            },
            "validation_errors": errors,
            "run": _run_metadata(state, provider_health),
        }
    summary = summarize(review)
    status = "blocked" if summary["blocker_count"] else "completed"
    return {
        "status": status,
        "summary": summary,
        "review": review,
        "artifact": {
            **artifact,
            "valid": True,
            "schema": REVIEW_SCHEMA,
        },
        "validation_errors": [],
        "run": _run_metadata(state, provider_health),
    }


def validate_artifact(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["review artifact must be a JSON object"]
    errors: list[str] = []
    schema = value.get("schema")
    if schema != REVIEW_SCHEMA:
        errors.append(f"schema must be {REVIEW_SCHEMA!r}")
    verdict = value.get("verdict")
    if verdict not in {"accepted", "rejected", "unknown"}:
        errors.append("verdict must be accepted, rejected, or unknown")
    findings = value.get("findings")
    blockers = value.get("blockers")
    files_inspected = value.get("files_inspected")
    commands_run = value.get("commands_run")
    if not isinstance(findings, list):
        errors.append("findings must be an array")
    if not isinstance(blockers, list):
        errors.append("blockers must be an array")
    if not isinstance(files_inspected, list):
        errors.append("files_inspected must be an array")
    if not isinstance(commands_run, list):
        errors.append("commands_run must be an array")
    if isinstance(findings, list) and not findings:
        if isinstance(blockers, list) and blockers:
            return errors
        if isinstance(files_inspected, list) and not files_inspected:
            errors.append("files_inspected must not be empty")
        if isinstance(commands_run, list) and not commands_run:
            errors.append("commands_run must not be empty")
    return errors


def summarize(review: dict[str, Any]) -> dict[str, Any]:
    findings = review["findings"]
    blockers = review["blockers"]
    blocking_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict)
        and finding.get("severity") in {"critical", "high", "blocking"}
    ]
    return {
        "verdict": review["verdict"],
        "finding_count": len(findings),
        "blocking_finding_count": len(blocking_findings),
        "blocker_count": len(blockers),
    }


def _review_prompt(
    *,
    title: str,
    issue: str,
    scope_paths: list[str] | None,
    output_file: str,
    change_contract: str,
) -> str:
    issue = _required_text(issue, "issue")
    scopes = _scope_lines(scope_paths)
    return "\n".join(
        [
            title,
            "",
            "Issue/context:",
            issue,
            "",
            "Change selection:",
            change_contract,
            "",
            "Scope paths:",
            *scopes,
            "",
            "Review contract:",
            "- Perform a code-review style inspection focused on bugs, regressions, "
            "missing tests, and mismatches with the issue/context.",
            "- Do not mutate git state, PR state, labels, issues, or source files.",
            "- Record files inspected and commands run. If commands cannot run, "
            "record a blocker instead of pretending success.",
            "- A zero-finding review is valid only with files inspected and "
            "commands run.",
            f"- Write the final review artifact as strict JSON to: {output_file}",
            "- Do not finish until that exact file exists and is non-empty.",
            "",
            "Required JSON shape:",
            "{",
            f'  "schema": "{REVIEW_SCHEMA}",',
            '  "verdict": "accepted | rejected | unknown",',
            '  "findings": [],',
            '  "blockers": [],',
            '  "files_inspected": [],',
            '  "commands_run": []',
            "}",
        ]
    )


def _scope_lines(scope_paths: list[str] | None) -> list[str]:
    if not scope_paths:
        return ["- Entire requested change."]
    lines = []
    for path in scope_paths:
        text = _required_text(path, "scope_paths")
        if "\n" in text:
            raise ValueError("scope_paths entries must be single-line paths")
        lines.append(f"- {text}")
    return lines


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or "\x00" in value or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(value) > 20_000:
        raise ValueError(f"{label} is too large")
    return value.strip()


def _optional_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _artifact_base(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": False,
        "total_bytes": None,
        "schema": REVIEW_SCHEMA,
    }


def _run_metadata(state: RunState, provider_health: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": state["run_id"],
        "status": state["status"],
        "artifact_path": state.get("review_output_file"),
        "output_file": state.get("review_output_file"),
        "provider_health": provider_health,
        "error": state.get("error"),
    }
