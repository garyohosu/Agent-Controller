"""Public run lifecycle operations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_controller.models import Event, RunInput, RunState, State
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger


class RunStartError(ValueError):
    """The requested workspace or run cannot be started safely."""


def validate_workspace(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.exists():
        raise RunStartError(f"workspace does not exist: {root}")
    if not root.is_dir():
        raise RunStartError(f"workspace is not a directory: {root}")
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if probe.returncode != 0:
        raise RunStartError(f"workspace is not a Git repository: {root}")
    top = Path(probe.stdout.strip()).resolve()
    if top != root:
        raise RunStartError(f"workspace must be the Git repository root: {root}")
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if status.returncode != 0:
        raise RunStartError(status.stderr.strip() or "could not inspect Git status")
    if status.stdout:
        raise RunStartError("workspace working tree is not clean")
    return root


def _write_start_diagnostic(workspace: Path, payload: dict[str, Any]) -> None:
    target = workspace.parent / "agent-controller-diagnostics.jsonl"
    safe = {
        "event": "run_start",
        "run_id": payload["run_id"],
        "workspace": str(workspace),
        "state": payload["state"],
        "request_chars": len(payload["request"]),
        "request_utf8_bytes": len(payload["request"].encode("utf-8")),
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def start_run(
    store: Store,
    logger: TransitionLogger,
    *,
    run_id: str,
    workspace: str | Path,
    request: str,
) -> tuple[RunState, RunInput]:
    if not run_id.strip():
        raise RunStartError("run ID must not be empty")
    if not request.strip():
        raise RunStartError("request must not be empty")
    root = validate_workspace(workspace)
    if store.load_run(run_id) is not None:
        raise RunStartError(f"run already exists: {run_id}")

    run = RunState(project_id=root.name, run_id=run_id)
    run_input = RunInput(
        run_id=run_id,
        workspace=str(root),
        request=request.strip(),
    )
    # Persist the run and its formal input before entering the graph. Any
    # validation failure happens before this point, so no partial run is made.
    store.save_run(run)
    store.save_input(run_input)
    _write_start_diagnostic(root, {
        "run_id": run_id,
        "workspace": str(root),
        "request": run_input.request,
        "state": run.current_state.value,
    })
    transition = logger.record(run, Event.START, reason="public CLI init")
    if transition.to_state != State.DESIGN:
        raise RunStartError("init did not enter DESIGN")
    return run, run_input
