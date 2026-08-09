from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_controller.complete import CompleteGate, CompleteGateError
from agent_controller.graph import StageResult, run_graph, wired_handlers
from agent_controller.models import (
    ArtifactKind,
    ArtifactState,
    ArtifactStatus,
    Event,
    RunState,
    State,
    Worker,
)
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger
from agent_controller.worker import WorkerRequest, WorkerResult


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def pushed_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    git(root, "branch", "-M", "main")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-qu", "origin", "main")
    return root


class FakeWorker:
    def __init__(self, name: Worker, root: Path, event: Event) -> None:
        self.name = name
        self.root = root
        self.event = event

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.state == State.IMPLEMENT:
            (self.root / "CODE.txt").write_text("implemented\n")
        return WorkerResult(event=self.event)


def test_wired_main_graph_reaches_complete(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id="r")
        store.save_run(run)
        handlers = wired_handlers(
            logger,
            workspace=root,
            implementer=FakeWorker(Worker.CODEX_CLI, root, Event.DONE),
            reviewer=FakeWorker(Worker.CLAUDE_CODE, root, Event.PASS),
            test_runner=lambda current: 0,
        )
        final = run_graph(run, logger, handlers, complete_gate=CompleteGate(store, root))
        assert final.current_state == State.COMPLETE
        states = [item.to_state for item in logger.history("r")]
        compressed = [state for index, state in enumerate(states)
                      if index == 0 or state != states[index - 1]]
        assert compressed == [
            State.DESIGN, State.IMPLEMENT, State.TEST,
            State.REVIEW, State.DOC_SYNC, State.COMPLETE,
        ]
        assert store.artifacts("r")[ArtifactKind.CODE].status == ArtifactStatus.VALID


def test_wired_graph_does_not_complete_with_a_gate_blocker(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id="r")
        store.save_run(run)
        handlers = wired_handlers(
            logger,
            workspace=root,
            implementer=FakeWorker(Worker.CODEX_CLI, root, Event.DONE),
            reviewer=FakeWorker(Worker.CLAUDE_CODE, root, Event.PASS),
            test_runner=lambda current: 0,
        )
        original = handlers[State.DOC_SYNC]

        def blocked_doc_sync(current: RunState) -> StageResult:
            result = original(current)
            code = store.artifacts("r")[ArtifactKind.CODE]
            code.status = ArtifactStatus.STALE
            store.save_artifact(code)
            return result

        handlers[State.DOC_SYNC] = blocked_doc_sync
        with pytest.raises(CompleteGateError):
            run_graph(run, logger, handlers, complete_gate=CompleteGate(store, root))
        assert logger.history("r")[-1].to_state == State.DOC_SYNC
