"""Worker Capability Profile と Auto-Recovery のテスト（指示書 2026-08-17-018 §4 / §5）。

AC-03: can_write=False の候補は GENERATE / FIX へ最初から呼ばれない。
AC-04: can_write=True の候補が全滅した場合だけ最終的に失敗する。
AC-05: dirty workspace は今まで通り自動 clean せずに拒否される（回帰の固定）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_controller.capability import (
    DEFAULT_CAPABILITIES,
    WorkerCapability,
    capability_for,
    classify_worker_error,
    satisfies,
    shorten_directive,
)
from agent_controller.git_checkpoint import DirtyWorkingTreeError, GitCheckpointManager
from agent_controller.models import DocumentStage, Event, Phase, Role, RunState, State, Worker
from agent_controller.store import Store
from agent_controller.worker import WorkerRequest, WorkerResult, WorkerRouter, phase_handlers_from_worker


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


class CountingWorker:
    """呼ばれた回数を記録する固定応答 Worker。"""

    def __init__(self, name: Worker, event: Event, root: Path, reason: str = "") -> None:
        self.name = name
        self.event = event
        self.root = root
        self.reason = reason
        self.calls = 0

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.calls += 1
        if self.event not in (Event.WORKER_ERROR, Event.WORKER_RESOURCE_LIMIT):
            (self.root / "tracked.txt").write_text(self.name.value + "\n")
        return WorkerResult(event=self.event, reason=self.reason, files_changed=["tracked.txt"])


# --- unit: classification -------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("SPEC.md and dream.md could not be updated because filesystem writes were denied.", "WRITE_PERMISSION_DENIED"),
        ("permission denied while writing output", "WRITE_PERMISSION_DENIED"),
        ("SPEC.md への書き込みがファイルシステムから拒否されました。", "WRITE_PERMISSION_DENIED"),
        ("cannot read the input document, access denied", "READ_PERMISSION_DENIED"),
        ("claude timed out after 900s", "TIMEOUT"),
        ("exited with 1", None),
    ],
)
def test_classify_worker_error(reason: str, expected: str | None) -> None:
    assert classify_worker_error(reason) == expected


def test_capability_for_unknown_worker_is_fully_conservative() -> None:
    fallback = capability_for(Worker.GROK, profiles={})
    assert fallback.can_write is False
    assert fallback.can_read is False
    assert fallback.can_answer is False


def test_default_capabilities_keep_existing_worker_behaviour() -> None:
    for worker in (Worker.CLAUDE_CODE, Worker.CODEX_CLI, Worker.GROK, Worker.ANTIGRAVITY):
        capability = capability_for(worker)
        assert capability.can_write and capability.can_read and capability.can_review
    assert satisfies(DEFAULT_CAPABILITIES[Worker.CLAUDE_CODE], Phase.GENERATE)
    assert satisfies(DEFAULT_CAPABILITIES[Worker.CLAUDE_CODE], Phase.REVIEW_LIGHT)


def test_shorten_directive_collapses_and_truncates() -> None:
    long_text = "word " * 500
    short = shorten_directive(long_text, limit=50)
    assert len(short) <= 50
    assert "\n" not in short


# --- AC-03: capability-filtered candidates ---------------------------------


def run_at(state: State, substate: DocumentStage, phase: Phase, run_id: str = "cap-run") -> RunState:
    return RunState(project_id="p", run_id=run_id, current_state=state, substate=substate, phase=phase)


def test_write_incapable_candidate_is_never_invoked_for_generate(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    store = Store(":memory:")
    run = run_at(State.DESIGN, DocumentStage.CLASS, Phase.GENERATE)
    store.save_run(run)
    manager = GitCheckpointManager(root, store)

    read_only_first = CountingWorker(Worker.CLAUDE_CODE, Event.DONE, root)
    write_capable_second = CountingWorker(Worker.CODEX_CLI, Event.DONE, root)
    capabilities = {
        Worker.CLAUDE_CODE: WorkerCapability(worker=Worker.CLAUDE_CODE, can_write=False),
        Worker.CODEX_CLI: WorkerCapability(worker=Worker.CODEX_CLI),
    }

    handler = phase_handlers_from_worker(
        WorkerRouter({role: [read_only_first, write_capable_second] for role in Role}),
        str(root), [], "tracked.txt", git=manager, capabilities=capabilities,
    )[Phase.GENERATE]

    result = handler(run)

    assert result.event == Event.DONE
    assert result.worker == Worker.CODEX_CLI
    assert read_only_first.calls == 0, "capability-mismatched candidate must not be invoked"
    assert write_capable_second.calls == 1

    attempts = store.recovery_attempts(run.run_id)
    mismatches = [item for item in attempts if item.capability_mismatch]
    assert len(mismatches) == 1
    assert mismatches[0].failed_worker == Worker.CLAUDE_CODE
    assert mismatches[0].error_code == "CAPABILITY_MISMATCH"
    assert mismatches[0].fallback_worker == Worker.CODEX_CLI


def test_read_only_capability_does_not_restrict_review(tmp_path: Path) -> None:
    """Reviewer は read-only のままでよい。can_write=False でも REVIEW には呼べる。"""
    root = make_repo(tmp_path)
    store = Store(":memory:")
    run = run_at(State.DESIGN, DocumentStage.CLASS, Phase.REVIEW_LIGHT)
    store.save_run(run)
    manager = GitCheckpointManager(root, store)

    reviewer = CountingWorker(Worker.CLAUDE_CODE, Event.PASS, root)
    capabilities = {Worker.CLAUDE_CODE: WorkerCapability(worker=Worker.CLAUDE_CODE, can_write=False)}

    handler = phase_handlers_from_worker(
        WorkerRouter({role: [reviewer] for role in Role}),
        str(root), [], "tracked.txt", git=manager, capabilities=capabilities,
    )[Phase.REVIEW_LIGHT]

    result = handler(run)
    assert result.event == Event.PASS
    assert reviewer.calls == 1


# --- AC-04: all write-capable candidates fail ------------------------------


def test_write_permission_denied_falls_back_then_human_required_only_after_all_fail(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    store = Store(":memory:")
    run = run_at(State.DESIGN, DocumentStage.SPEC, Phase.GENERATE, run_id="ac04")
    store.save_run(run)
    manager = GitCheckpointManager(root, store)

    denied_message = "SPEC.md could not be updated because filesystem writes were denied."
    first = CountingWorker(Worker.CODEX_CLI, Event.WORKER_ERROR, root, reason=denied_message)
    second = CountingWorker(Worker.CLAUDE_CODE, Event.WORKER_ERROR, root, reason=denied_message)

    handler = phase_handlers_from_worker(
        WorkerRouter({role: [first, second] for role in Role}),
        str(root), [], "tracked.txt", git=manager,
    )[Phase.GENERATE]

    result = handler(run)

    assert result.event == Event.WORKER_ERROR
    assert first.calls == 1
    assert second.calls == 1, "the same worker must not be retried; the OTHER write-capable candidate must be tried"

    attempts = store.recovery_attempts(run.run_id)
    assert len(attempts) == 2
    assert {item.error_code for item in attempts} == {"WRITE_PERMISSION_DENIED"}
    assert all(item.final_outcome == "ALL_CANDIDATES_FAILED" for item in attempts)
    assert attempts[0].failed_worker == Worker.CODEX_CLI
    assert attempts[0].fallback_worker == Worker.CLAUDE_CODE
    assert attempts[1].failed_worker == Worker.CLAUDE_CODE
    assert attempts[1].fallback_worker is None


def test_write_permission_denied_recovers_when_a_second_candidate_can_write(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    store = Store(":memory:")
    run = run_at(State.DESIGN, DocumentStage.SPEC, Phase.GENERATE, run_id="ac03b")
    store.save_run(run)
    manager = GitCheckpointManager(root, store)

    denied_message = "filesystem writes were denied"
    first = CountingWorker(Worker.CODEX_CLI, Event.WORKER_ERROR, root, reason=denied_message)
    second = CountingWorker(Worker.CLAUDE_CODE, Event.DONE, root)

    handler = phase_handlers_from_worker(
        WorkerRouter({role: [first, second] for role in Role}),
        str(root), [], "tracked.txt", git=manager,
    )[Phase.GENERATE]

    result = handler(run)

    assert result.event == Event.DONE
    assert result.worker == Worker.CLAUDE_CODE
    attempts = store.recovery_attempts(run.run_id)
    assert len(attempts) == 1
    assert attempts[0].error_code == "WRITE_PERMISSION_DENIED"
    assert attempts[0].final_outcome == "FALLBACK_SUCCEEDED"
    assert attempts[0].fallback_worker == Worker.CLAUDE_CODE


def test_claude_rate_limit_falls_back_to_codex_from_same_checkpoint(tmp_path: Path) -> None:
    """実測した Claude Code rate limit は Codex fallback で継続できる。"""
    root = make_repo(tmp_path)
    store = Store(":memory:")
    run = run_at(State.DESIGN, DocumentStage.SPEC, Phase.GENERATE, run_id="rate-limit-fallback")
    store.save_run(run)
    manager = GitCheckpointManager(root, store)

    claude = CountingWorker(
        Worker.CLAUDE_CODE,
        Event.WORKER_RESOURCE_LIMIT,
        root,
        reason="Claude Code rate limit reached after 2m11s",
    )
    codex = CountingWorker(Worker.CODEX_CLI, Event.DONE, root)

    handler = phase_handlers_from_worker(
        WorkerRouter({role: [claude, codex] for role in Role}),
        str(root), [], "tracked.txt", git=manager,
    )[Phase.GENERATE]

    result = handler(run)

    assert result.event == Event.DONE
    assert result.worker == Worker.CODEX_CLI
    assert claude.calls == 1
    assert codex.calls == 1
    assert (root / "tracked.txt").read_text() == f"{Worker.CODEX_CLI.value}\n"
    attempts = store.recovery_attempts(run.run_id)
    assert len(attempts) == 1
    assert attempts[0].error_code == "WORKER_RESOURCE_LIMIT"
    assert attempts[0].failed_worker == Worker.CLAUDE_CODE
    assert attempts[0].fallback_worker == Worker.CODEX_CLI
    assert attempts[0].final_outcome == "FALLBACK_SUCCEEDED"


# --- AC-05: dirty workspace stays a hard stop, never auto-cleaned ---------


def test_dirty_workspace_is_rejected_without_touching_git(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "tracked.txt").write_text("uncommitted user change\n")
    manager = GitCheckpointManager(root)

    with pytest.raises(DirtyWorkingTreeError):
        manager.begin(RunState(project_id="p", run_id="dirty"))

    # No clean / reset / stash must have run: the dirty content is untouched
    # and still reported dirty.
    assert (root / "tracked.txt").read_text() == "uncommitted user change\n"
    assert git(root, "status", "--porcelain") != ""


def test_dirty_workspace_stops_phase_handler_before_any_worker_is_called(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "tracked.txt").write_text("uncommitted user change\n")
    store = Store(":memory:")
    manager = GitCheckpointManager(root, store)
    run = run_at(State.DESIGN, DocumentStage.SPEC, Phase.GENERATE, run_id="dirty2")
    store.save_run(run)

    worker = CountingWorker(Worker.CODEX_CLI, Event.DONE, root)
    handler = phase_handlers_from_worker(worker, str(root), [], "tracked.txt", git=manager)[Phase.GENERATE]

    result = handler(run)

    assert result.event == Event.WORKER_ERROR
    assert "git checkpoint unavailable" in (result.reason or "")
    assert worker.calls == 0
    assert (root / "tracked.txt").read_text() == "uncommitted user change\n"
