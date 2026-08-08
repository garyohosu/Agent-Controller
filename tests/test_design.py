"""DESIGN の Progressive Refinement のテスト（指示書 §17-7）。

確認したいのは 2 点。

- SPEC → USECASE → SEQUENCE → CLASS → (UI) → TESTCASE が順に回ること
- その間、トップレベル State が DESIGN から動かないこと
"""

from __future__ import annotations

from datetime import timezone

import pytest

from agent_controller.design import (
    DesignProgressError,
    artifact_kind_for,
    default_design_stages,
    design_artifact_statuses,
    invalidate_from,
    run_design,
)
from agent_controller.document_stage import ScriptedPhaseHandlers, StageResult
from agent_controller.guards import GuardLimits, LoopGuard
from agent_controller.models import (
    ArtifactState,
    ArtifactStatus,
    DocumentStage,
    Event,
    Phase,
    ReviewLevel,
    RunState,
    RunStatus,
    State,
    Worker,
)
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

UTC = timezone.utc

DEFAULT_ORDER = [
    DocumentStage.SPEC,
    DocumentStage.USECASE,
    DocumentStage.SEQUENCE,
    DocumentStage.CLASS,
    DocumentStage.TESTCASE,
]


@pytest.fixture
def design_run(store: Store) -> RunState:
    run = RunState(
        project_id="agent-controller",
        run_id="run-design",
        current_state=State.DESIGN,
        checkpoint_commit="7083803",
    )
    store.save_run(run)
    return run


def stage_order(logger: TransitionLogger, run_id: str) -> list[DocumentStage]:
    """各 stage に入った順（START 行の to_substate）。"""
    return [
        item.to_substate
        for item in logger.history(run_id)
        if item.event == Event.START and item.to_substate is not None
    ]


class TestPlan:
    def test_default_plan_skips_ui(self) -> None:
        """§2 の UI は「必要な場合」。既定では入れない。"""
        assert [config.name for config in default_design_stages()] == DEFAULT_ORDER

    def test_ui_can_be_included(self) -> None:
        names = [config.name for config in default_design_stages(include_ui=True)]
        assert names == [
            DocumentStage.SPEC,
            DocumentStage.USECASE,
            DocumentStage.SEQUENCE,
            DocumentStage.CLASS,
            DocumentStage.UI,
            DocumentStage.TESTCASE,
        ]

    def test_review_levels_come_from_the_instruction_defaults(self) -> None:
        """§5: SPEC / USECASE のみ DEEP。§17-8 の初期配線。"""
        levels = {config.name: config.review_level for config in default_design_stages()}
        assert levels == {
            DocumentStage.SPEC: ReviewLevel.DEEP,
            DocumentStage.USECASE: ReviewLevel.DEEP,
            DocumentStage.SEQUENCE: ReviewLevel.LIGHT,
            DocumentStage.CLASS: ReviewLevel.LIGHT,
            DocumentStage.TESTCASE: ReviewLevel.LIGHT,
        }

    def test_inputs_reference_upstream_documents(self) -> None:
        inputs = {config.name: config.inputs for config in default_design_stages()}
        assert inputs[DocumentStage.SPEC] == []
        assert inputs[DocumentStage.USECASE] == ["SPEC.md"]
        assert "SEQUENCE.md" in inputs[DocumentStage.CLASS]


class TestHappyPath:
    def test_all_stages_run_in_order_then_design_passes(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        final = run_design(design_run, logger)

        assert stage_order(logger, final.run_id) == DEFAULT_ORDER
        assert final.current_state == State.IMPLEMENT
        assert final.status == RunStatus.RUNNING

    def test_toplevel_state_stays_in_design_until_the_last_event(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(design_run, logger)

        history = logger.history(design_run.run_id)
        assert {item.from_state for item in history} == {State.DESIGN}
        assert {item.to_state for item in history[:-1]} == {State.DESIGN}
        assert history[-1].event == Event.PASS
        assert history[-1].to_state == State.IMPLEMENT

    def test_every_artifact_ends_valid(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(design_run, logger)

        statuses = design_artifact_statuses(logger, design_run)
        assert set(statuses) == set(DEFAULT_ORDER)
        assert all(status == ArtifactStatus.VALID for status in statuses.values())

    def test_ui_stage_runs_when_included(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        stages = default_design_stages(include_ui=True)
        final = run_design(design_run, logger, stages)

        assert DocumentStage.UI in stage_order(logger, final.run_id)
        assert final.current_state == State.IMPLEMENT

    def test_spec_and_usecase_get_deep_review(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(design_run, logger)

        deep = {
            item.from_substate
            for item in logger.history(design_run.run_id)
            if item.phase == Phase.REVIEW_DEEP
        }
        assert deep == {DocumentStage.SPEC, DocumentStage.USECASE}


class TestUpstreamReturn:
    def _handlers(self) -> dict:
        """CLASS のレビューで一度だけ SEQUENCE の問題を指摘する。"""
        return ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,  # SEQUENCE (1 回目)
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SEQUENCE,
                        reason="sequence responsibility mismatch",
                        worker=Worker.CODEX_CLI,
                    ),  # CLASS (1 回目)
                    Event.PASS,  # SEQUENCE (2 回目)
                    Event.PASS,  # CLASS (2 回目)
                    Event.PASS,  # TESTCASE
                ],
                Phase.REVIEW_DEEP: [Event.PASS],
            }
        ).as_handlers()

    def test_only_the_named_stage_and_its_downstream_rerun(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§2「必要最小限の上位工程へ戻す」。SPEC / USECASE は触らない。"""
        final = run_design(design_run, logger, handlers=self._handlers())

        assert final.current_state == State.IMPLEMENT
        assert stage_order(logger, final.run_id) == [
            DocumentStage.SPEC,
            DocumentStage.USECASE,
            DocumentStage.SEQUENCE,
            DocumentStage.CLASS,
            DocumentStage.SEQUENCE,  # 戻る
            DocumentStage.CLASS,
            DocumentStage.TESTCASE,
        ]

    def test_upstream_artifacts_are_not_invalidated(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(design_run, logger, handlers=self._handlers())

        entries = stage_order(logger, design_run.run_id)
        assert entries.count(DocumentStage.SPEC) == 1
        assert entries.count(DocumentStage.USECASE) == 1

    def test_the_return_is_visible_in_the_human_log(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§最初の実証: どの State で、どの Event により、どこへ戻ったか。"""
        run_design(design_run, logger, handlers=self._handlers())

        rendered = logger.render(design_run.run_id, UTC)
        assert "DESIGN/CLASS/REVIEW_LIGHT | UPSTREAM_CHANGE_REQUIRED" in rendered
        assert "reason=sequence responsibility mismatch" in rendered
        assert "-> DESIGN/SEQUENCE/GENERATE" in rendered

    def test_invalidate_from_marks_target_and_downstream(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        stages = default_design_stages()
        for config in stages:
            logger.store.save_artifact(
                ArtifactState(
                    run_id=design_run.run_id,
                    kind=artifact_kind_for(config.name),
                    status=ArtifactStatus.VALID,
                )
            )

        invalidate_from(logger, design_run, stages, DocumentStage.SEQUENCE)

        statuses = design_artifact_statuses(logger, design_run, stages)
        assert statuses[DocumentStage.SPEC] == ArtifactStatus.VALID
        assert statuses[DocumentStage.USECASE] == ArtifactStatus.VALID
        assert statuses[DocumentStage.SEQUENCE] == ArtifactStatus.STALE
        assert statuses[DocumentStage.CLASS] == ArtifactStatus.STALE
        assert statuses[DocumentStage.TESTCASE] == ArtifactStatus.STALE

    def test_unknown_upstream_target_is_rejected(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        stages = default_design_stages()
        with pytest.raises(ValueError, match="UI"):
            invalidate_from(logger, design_run, stages, DocumentStage.UI)

    def test_repeated_upstream_returns_trip_the_loop_guard(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§11: 同一理由で何度も上位へ戻るなら LOOP_DETECTED。"""
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,  # SEQUENCE
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SEQUENCE,
                        reason="still mismatched",
                    ),
                ],
            }
        ).as_handlers()

        guard = LoopGuard(
            logger.store, GuardLimits(max_upstream_rework=2, max_same_fingerprint=99)
        )
        final = run_design(design_run, logger, handlers=handlers, guard=guard)

        assert final.current_state == State.HUMAN_REQUIRED
        events = [item.event for item in logger.history(final.run_id)]
        assert events[-1] == Event.LOOP_DETECTED
        assert final.upstream_rework == 3


class TestInterruption:
    def test_resource_limit_stops_design_and_keeps_the_position(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,  # SEQUENCE
                    StageResult(
                        event=Event.WORKER_RESOURCE_LIMIT,
                        worker=Worker.CLAUDE_CODE,
                        reason="session limit",
                    ),  # CLASS
                ],
            }
        ).as_handlers()

        stopped = run_design(design_run, logger, handlers=handlers)

        assert stopped.current_state == State.WAIT_RESOURCE
        assert stopped.return_state == State.DESIGN
        assert stopped.substate == DocumentStage.CLASS
        assert stopped.return_phase == Phase.REVIEW_LIGHT

        statuses = design_artifact_statuses(logger, stopped)
        assert statuses[DocumentStage.SEQUENCE] == ArtifactStatus.VALID
        assert statuses[DocumentStage.CLASS] == ArtifactStatus.STALE

    def test_design_continues_from_the_interrupted_phase(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§12: Worker を替えて、止まった phase から設計を続ける。"""
        stopping = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,
                    StageResult(
                        event=Event.WORKER_RESOURCE_LIMIT, worker=Worker.CLAUDE_CODE
                    ),
                ],
            }
        ).as_handlers()
        stopped = run_design(design_run, logger, handlers=stopping)
        assert stopped.current_state == State.WAIT_RESOURCE

        logger.record(
            stopped,
            Event.RESOURCE_AVAILABLE,
            to_substate=DocumentStage.CLASS,
            worker=Worker.CODEX_CLI,
        )
        resumed = run_design(stopped, logger)

        assert resumed.current_state == State.IMPLEMENT
        # CLASS は GENERATE からやり直さず REVIEW_LIGHT から再開する。
        resume_row = next(
            item
            for item in logger.history(resumed.run_id)
            if item.event == Event.START and item.reason == "resume"
        )
        assert resume_row.to_substate == DocumentStage.CLASS
        assert resume_row.to_phase == Phase.REVIEW_LIGHT

    def test_unanswerable_question_stops_design(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.QUESTION],
                Phase.QANDA: [
                    StageResult(event=Event.CANNOT_ANSWER, reason="not in any artifact")
                ],
            }
        ).as_handlers()

        final = run_design(design_run, logger, handlers=handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.return_state == State.DESIGN
        assert final.substate == DocumentStage.SPEC


class TestGuards:
    def test_escalation_without_a_target_is_reported(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """SERIOUS_ISSUE で DEEP から抜けたが、戻り先が無い場合。"""
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.SERIOUS_ISSUE],
            }
        ).as_handlers()

        with pytest.raises(DesignProgressError):
            run_design(design_run, logger, handlers=handlers)
