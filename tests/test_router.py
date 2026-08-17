"""Task Complexity Router / Design Fast Path のテスト（指示書 2026-08-17-018 §3）。

AC-01: SMALL_FEATURE は SPEC -> TESTCASE だけを通り、不要な USECASE / SEQUENCE /
       CLASS / UI を生成しない。TEST / REVIEW / CompleteGate は省略しない。
AC-02: task_type が未分類（None）または NEW_PRODUCT の run は、
       既存のフル Progressive Refinement のまま壊れない。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_controller.complete import CompleteGate
from agent_controller.design import design_stages_for_task_type, mark_fast_path_skips
from agent_controller.graph import run_graph, wired_handlers
from agent_controller.models import (
    ArtifactKind,
    ArtifactStatus,
    DocumentStage,
    Event,
    RunState,
    State,
    TaskType,
    Worker,
)
from agent_controller.router import classify_task_type, skipped_stages_for_task_type
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


def stage_order(logger: TransitionLogger, run_id: str) -> list[DocumentStage]:
    return [
        item.to_substate
        for item in logger.history(run_id)
        if item.event == Event.START and item.to_substate is not None
    ]


# --- pure stage-selection ---------------------------------------------------


class TestStageSelection:
    def test_small_feature_is_spec_and_testcase_only(self) -> None:
        names = [c.name for c in design_stages_for_task_type(TaskType.SMALL_FEATURE)]
        assert names == [DocumentStage.SPEC, DocumentStage.TESTCASE]

    def test_new_feature_adds_usecase(self) -> None:
        names = [c.name for c in design_stages_for_task_type(TaskType.NEW_FEATURE)]
        assert names == [DocumentStage.SPEC, DocumentStage.USECASE, DocumentStage.TESTCASE]

    def test_new_product_matches_the_legacy_full_default(self) -> None:
        names = [c.name for c in design_stages_for_task_type(TaskType.NEW_PRODUCT)]
        assert names == [
            DocumentStage.SPEC, DocumentStage.USECASE, DocumentStage.SEQUENCE,
            DocumentStage.CLASS, DocumentStage.TESTCASE,
        ]

    def test_doc_only_is_spec_only(self) -> None:
        names = [c.name for c in design_stages_for_task_type(TaskType.DOC_ONLY)]
        assert names == [DocumentStage.SPEC]

    def test_bug_fix_and_refactor_skip_the_same_stages_as_small_feature(self) -> None:
        for task_type in (TaskType.BUG_FIX, TaskType.REFACTOR):
            names = [c.name for c in design_stages_for_task_type(task_type)]
            assert names == [DocumentStage.SPEC, DocumentStage.TESTCASE]

    def test_skipped_stages_for_small_feature(self) -> None:
        assert set(skipped_stages_for_task_type(TaskType.SMALL_FEATURE)) == {
            DocumentStage.USECASE, DocumentStage.SEQUENCE, DocumentStage.CLASS, DocumentStage.UI,
        }


class TestClassifyTaskType:
    def test_bug_report_classifies_as_bug_fix(self) -> None:
        assert classify_task_type("Fix the crash when input is empty") == TaskType.BUG_FIX

    def test_ambiguous_request_defaults_to_new_product(self) -> None:
        # 指示書 §3.3: 不明なら保守的な既定（= フル経路）へ倒す。
        assert classify_task_type("") == TaskType.NEW_PRODUCT
        assert classify_task_type("Do the thing we discussed") == TaskType.NEW_PRODUCT

    def test_docs_only_request(self) -> None:
        assert classify_task_type("Update the README with install instructions") == TaskType.DOC_ONLY

    def test_new_product_hint_wins_over_small_feature_hint(self) -> None:
        assert classify_task_type("Build a new product with a small onboarding flow") == TaskType.NEW_PRODUCT


# --- mark_fast_path_skips is write-once -------------------------------------


def test_mark_fast_path_skips_only_writes_missing_kinds(tmp_path: Path) -> None:
    with Store(":memory:") as store:
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id="r", task_type=TaskType.SMALL_FEATURE)
        store.save_run(run)

        mark_fast_path_skips(logger, run, TaskType.SMALL_FEATURE)
        first_pass = store.artifacts(run.run_id)
        for kind in (ArtifactKind.USECASE, ArtifactKind.SEQUENCE, ArtifactKind.CLASS):
            assert first_pass[kind].status == ArtifactStatus.VALID
            assert first_pass[kind].reason.startswith("NOT_REQUIRED")
        first_stamp = first_pass[ArtifactKind.USECASE].updated_at

        # Calling again (e.g. on a resumed run) must not rewrite the stub -
        # advisor concern: a later timestamp could trip CODE_NOT_LATEST.
        mark_fast_path_skips(logger, run, TaskType.SMALL_FEATURE)
        second_pass = store.artifacts(run.run_id)
        assert second_pass[ArtifactKind.USECASE].updated_at == first_stamp


# --- AC-01: end-to-end SMALL_FEATURE fast path ------------------------------


def test_small_feature_fast_path_skips_docs_but_keeps_test_review_and_gate(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id="r", task_type=TaskType.SMALL_FEATURE)
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

        # Only the fast-path stages were actually run.
        assert stage_order(logger, "r") == [DocumentStage.SPEC, DocumentStage.TESTCASE]

        artifacts = store.artifacts("r")
        for skipped in (ArtifactKind.USECASE, ArtifactKind.SEQUENCE, ArtifactKind.CLASS):
            assert artifacts[skipped].status == ArtifactStatus.VALID
            assert "NOT_REQUIRED" in (artifacts[skipped].reason or "")

        # TEST / REVIEW / CompleteGate were never skipped: the run only
        # reaches COMPLETE because run_graph actually walked TEST and REVIEW.
        states = [item.to_state for item in logger.history("r")]
        assert State.TEST in states
        assert State.REVIEW in states
        assert CompleteGate(store, root).check(final).ready is True


# --- AC-02: NEW_PRODUCT / unclassified runs are unaffected ------------------


def test_unclassified_run_keeps_the_legacy_full_progressive_refinement(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id="r")  # task_type left as None
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
        assert stage_order(logger, "r") == [
            DocumentStage.SPEC, DocumentStage.USECASE,
            DocumentStage.SEQUENCE, DocumentStage.CLASS, DocumentStage.TESTCASE,
        ]


def test_explicit_new_product_task_type_matches_the_unclassified_default(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id="r", task_type=TaskType.NEW_PRODUCT)
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
        assert stage_order(logger, "r") == [
            DocumentStage.SPEC, DocumentStage.USECASE,
            DocumentStage.SEQUENCE, DocumentStage.CLASS, DocumentStage.TESTCASE,
        ]
