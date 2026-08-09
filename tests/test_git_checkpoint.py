from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_controller.git_checkpoint import DirtyWorkingTreeError, GitCheckpointManager
from agent_controller.models import DocumentStage, Event, Phase, Role, RunState, State, Worker
from agent_controller.worker import WorkerRequest, WorkerResult, WorkerRouter, phase_handlers_from_worker


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_begin_saves_head_and_rollback_preserves_baseline_untracked(tmp_path: Path) -> None:
    root = repo(tmp_path)
    user_file = root / "scratchpad.txt"
    user_file.write_text("keep me\n")
    manager = GitCheckpointManager(root)
    run = RunState(project_id="p", run_id="r")

    checkpoint = manager.begin(run)
    assert checkpoint == git(root, "rev-parse", "HEAD")
    assert run.checkpoint_commit == checkpoint

    (root / "tracked.txt").write_text("worker change\n")
    (root / "worker-output.txt").write_text("generated\n")
    manager.rollback(checkpoint)

    assert (root / "tracked.txt").read_text() == "base\n"
    assert (root / "worker-output.txt").exists()
    assert user_file.read_text() == "keep me\n"


def test_begin_rejects_preexisting_tracked_changes(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "tracked.txt").write_text("user change\n")
    with pytest.raises(DirtyWorkingTreeError):
        GitCheckpointManager(root).begin(RunState(project_id="p", run_id="r"))


def test_rollback_returns_to_checkpoint_even_after_worker_commit(tmp_path: Path) -> None:
    root = repo(tmp_path)
    manager = GitCheckpointManager(root)
    run = RunState(project_id="p", run_id="r")
    checkpoint = manager.begin(run)
    (root / "tracked.txt").write_text("worker commit\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "worker-owned commit")

    manager.rollback(checkpoint)
    assert git(root, "rev-parse", "HEAD") == checkpoint
    assert (root / "tracked.txt").read_text() == "base\n"


def test_success_commit_uses_git_state_not_worker_claim(tmp_path: Path) -> None:
    root = repo(tmp_path)
    manager = GitCheckpointManager(root)
    run = RunState(project_id="p", run_id="r")
    manager.begin(run)
    (root / "tracked.txt").write_text("actual\n")

    observed = manager.verified_files_changed(["made-up.txt"])
    assert observed == ["tracked.txt"]
    manager.commit_success(Event.DONE)
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "log", "-1", "--pretty=%s") == "controller: Worker DONE"


class FakeWorker:
    def __init__(self, name: Worker, event: Event, root: Path) -> None:
        self.name = name
        self.event = event
        self.root = root

    def run(self, request: WorkerRequest) -> WorkerResult:
        (self.root / "tracked.txt").write_text(self.name.value + "\n")
        return WorkerResult(event=self.event, files_changed=["claimed-by-worker.txt"])


def test_resource_limit_can_switch_claude_to_codex_at_same_phase(tmp_path: Path) -> None:
    root = repo(tmp_path)
    run = RunState(
        project_id="p", run_id="r", current_state=State.DESIGN,
        substate=DocumentStage.CLASS, phase=Phase.REVIEW_LIGHT,
    )
    manager = GitCheckpointManager(root)
    claude = FakeWorker(Worker.CLAUDE_CODE, Event.WORKER_RESOURCE_LIMIT, root)
    first = phase_handlers_from_worker(claude, str(root), [], "tracked.txt", git=manager)
    limited = first[Phase.REVIEW_LIGHT](run)
    assert limited.event == Event.WORKER_RESOURCE_LIMIT
    assert run.checkpoint_commit == git(root, "rev-parse", "HEAD")
    assert (root / "tracked.txt").read_text() == "base\n"

    codex = FakeWorker(Worker.CODEX_CLI, Event.PASS, root)
    second = phase_handlers_from_worker(codex, str(root), [], "tracked.txt", git=manager)
    resumed = second[Phase.REVIEW_LIGHT](run)
    assert resumed.event == Event.PASS
    assert resumed.worker == Worker.CODEX_CLI


def test_router_fallback_keeps_position_and_checkpoint(tmp_path: Path) -> None:
    root = repo(tmp_path)
    run = RunState(project_id="p", run_id="router", current_state=State.DESIGN,
                   substate=DocumentStage.CLASS, phase=Phase.REVIEW_LIGHT)
    manager = GitCheckpointManager(root)
    first = FakeWorker(Worker.CLAUDE_CODE, Event.WORKER_ERROR, root)
    second = FakeWorker(Worker.CODEX_CLI, Event.PASS, root)
    handler = phase_handlers_from_worker(
        WorkerRouter({role: [first, second] for role in Role}), str(root), [], "tracked.txt", git=manager
    )[Phase.REVIEW_LIGHT]
    result = handler(run)
    assert result.event == Event.PASS
    assert result.worker == Worker.CODEX_CLI
    assert run.current_state == State.DESIGN
    assert run.substate == DocumentStage.CLASS
    assert run.phase == Phase.REVIEW_LIGHT
    assert run.current_state == State.DESIGN
    assert run.substate == DocumentStage.CLASS
    assert run.phase == Phase.REVIEW_LIGHT
