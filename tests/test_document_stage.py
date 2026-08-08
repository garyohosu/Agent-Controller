"""共通 Document Stage Subgraph のテスト（指示書 §17-6）。

この chunk の合否は「トップレベル State を増やさずに substate / phase だけで
stage の往復が回るか」で決まる。各テストで run.current_state が DESIGN のまま
であることを確認しているのはそのため。
"""

from __future__ import annotations

from datetime import timezone

import pytest

from agent_controller.document_stage import (
    ENTER_REVIEW,
    EXIT,
    STAGE_TRANSITIONS,
    DocumentStageConfig,
    ScriptedPhaseHandlers,
    StageResult,
    MissingUpstreamTargetError,
    UnknownPhaseTransitionError,
    build_document_stage,
    next_phase,
    run_document_stage,
    stage_completed,
    stub_phase_handlers,
)
from agent_controller.models import (
    DEFAULT_REVIEW_LEVELS,
    DocumentStage,
    Event,
    Phase,
    ReviewLevel,
    Role,
    RunState,
    RunStatus,
    State,
    Worker,
)
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

UTC = timezone.utc

CLASS_STAGE = DocumentStageConfig(
    name=DocumentStage.CLASS,
    inputs=["SEQUENCE.md", "SPEC.md"],
    output="CLASS.md",
    review_level=ReviewLevel.LIGHT,
    max_review_retry=2,
)

SPEC_STAGE = DocumentStageConfig(
    name=DocumentStage.SPEC,
    inputs=[],
    output="SPEC.md",
    review_level=ReviewLevel.DEEP,
    max_review_retry=2,
)


@pytest.fixture
def design_run(store: Store) -> RunState:
    """DESIGN に入った直後の run。"""
    run = RunState(
        project_id="agent-controller",
        run_id="run-stage",
        current_state=State.DESIGN,
        checkpoint_commit="a1c22fc",
    )
    store.save_run(run)
    return run


def phases(logger: TransitionLogger, run_id: str) -> list[Phase | None]:
    return [item.to_phase for item in logger.history(run_id)]


class TestStageTable:
    def test_fast_path(self) -> None:
        assert next_phase(Phase.GENERATE, Event.DONE) == ENTER_REVIEW
        assert next_phase(Phase.REVIEW_LIGHT, Event.PASS) == Phase.COMPLETE

    def test_local_fix_stays_inside_the_stage(self) -> None:
        assert next_phase(Phase.REVIEW_LIGHT, Event.LOCAL_FIX) == Phase.FIX
        assert next_phase(Phase.FIX, Event.DONE) == ENTER_REVIEW

    def test_serious_issue_escalates_light_to_deep(self) -> None:
        assert next_phase(Phase.REVIEW_LIGHT, Event.SERIOUS_ISSUE) == Phase.REVIEW_DEEP

    def test_serious_issue_in_deep_leaves_the_stage(self) -> None:
        """DEEP でさらに重大なら、それは上位工程の問題（§4）。"""
        assert next_phase(Phase.REVIEW_DEEP, Event.SERIOUS_ISSUE) == EXIT

    def test_upstream_change_always_exits(self) -> None:
        for phase in (Phase.GENERATE, Phase.REVIEW_LIGHT, Phase.REVIEW_DEEP, Phase.FIX):
            assert next_phase(phase, Event.UPSTREAM_CHANGE_REQUIRED) == EXIT

    def test_unknown_stage_transition_raises(self) -> None:
        with pytest.raises(UnknownPhaseTransitionError):
            next_phase(Phase.GENERATE, Event.PASS)

    def test_complete_is_terminal_inside_the_stage(self) -> None:
        assert not any(phase == Phase.COMPLETE for phase, _ in STAGE_TRANSITIONS)

    def test_stage_pass_never_reaches_the_toplevel_table(self) -> None:
        """stage の PASS は「文書が通った」であって「DESIGN が終わった」ではない。"""
        assert next_phase(Phase.REVIEW_LIGHT, Event.PASS) != EXIT
        assert next_phase(Phase.REVIEW_DEEP, Event.PASS) != EXIT


class TestFastPath:
    def test_generate_review_complete(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        final = run_document_stage(design_run, CLASS_STAGE, logger)

        assert stage_completed(final, CLASS_STAGE)
        assert final.current_state == State.DESIGN
        assert final.substate == DocumentStage.CLASS
        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.COMPLETE,
        ]

    def test_deep_stage_enters_deep_review_directly(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§5: SPEC は初期値 DEEP。設定値だけで振る舞いが変わる。"""
        final = run_document_stage(design_run, SPEC_STAGE, logger)

        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.REVIEW_DEEP,
            Phase.COMPLETE,
        ]
        assert stage_completed(final, SPEC_STAGE)

    def test_default_review_levels_match_the_instruction(self) -> None:
        assert DEFAULT_REVIEW_LEVELS[DocumentStage.SPEC] == ReviewLevel.DEEP
        assert DEFAULT_REVIEW_LEVELS[DocumentStage.USECASE] == ReviewLevel.DEEP
        assert DEFAULT_REVIEW_LEVELS[DocumentStage.CLASS] == ReviewLevel.LIGHT


class TestLocalFix:
    def test_fix_loop_returns_to_the_same_review_level(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§3 の図: REVIEW → LOCAL_FIX → FIX → REVIEW → PASS → COMPLETE。"""
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.LOCAL_FIX,
                        worker=Worker.CODEX_CLI,
                        role=Role.REVIEWER,
                        reason="class naming mismatch",
                    ),
                    Event.PASS,
                ],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.FIX,
            Phase.REVIEW_LIGHT,
            Phase.COMPLETE,
        ]
        assert final.current_state == State.DESIGN
        assert final.review_retry == 1

    def test_toplevel_state_never_moves_during_the_loop(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.LOCAL_FIX, Event.LOCAL_FIX, Event.PASS],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        history = logger.history(design_run.run_id)
        assert {item.from_state for item in history} == {State.DESIGN}
        assert {item.to_state for item in history} == {State.DESIGN}

        reloaded = store.load_run(design_run.run_id)
        assert reloaded is not None
        assert reloaded.current_state == State.DESIGN


class TestEscalation:
    def test_serious_issue_switches_the_stage_to_deep(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(event=Event.SERIOUS_ISSUE, reason="sequence mismatch")
                ],
                Phase.REVIEW_DEEP: [Event.LOCAL_FIX, Event.PASS],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.REVIEW_DEEP,
            Phase.FIX,
            Phase.REVIEW_DEEP,
            Phase.COMPLETE,
        ]
        # 一度上がったら、その stage は以後 DEEP で回る。
        assert final.review_phase == Phase.REVIEW_DEEP


class TestQandA:
    def test_answer_returns_to_the_asking_phase(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [
                    StageResult(event=Event.QUESTION, reason="retry policy unspecified"),
                    Event.DONE,
                ],
                Phase.QANDA: [StageResult(event=Event.DONE, role=Role.DIRECTOR)],
                Phase.REVIEW_LIGHT: [Event.PASS],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.QANDA,
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.COMPLETE,
        ]
        assert final.question_source_phase is None

    def test_question_from_review_returns_to_review(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.QUESTION, Event.PASS],
                Phase.QANDA: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.QANDA,
            Phase.REVIEW_LIGHT,
            Phase.COMPLETE,
        ]

    def test_answer_can_require_a_fix(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """Director の回答の結果、修正が必要になった場合。"""
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.QUESTION, Event.PASS],
                Phase.QANDA: [StageResult(event=Event.LOCAL_FIX, role=Role.DIRECTOR)],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert phases(logger, final.run_id) == [
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.QANDA,
            Phase.FIX,
            Phase.REVIEW_LIGHT,
            Phase.COMPLETE,
        ]

    def test_unanswerable_question_leaves_the_stage(
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

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.status == RunStatus.WAITING
        assert final.return_state == State.DESIGN
        assert final.substate == DocumentStage.CLASS
        assert not stage_completed(final, CLASS_STAGE)


class TestExitEvents:
    def test_upstream_change_propagates_to_the_toplevel_table(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SEQUENCE,
                        reason="sequence responsibility mismatch",
                    )
                ],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        last = logger.history(final.run_id)[-1]
        assert last.event == Event.UPSTREAM_CHANGE_REQUIRED
        assert last.to_state == State.DESIGN
        assert last.to_phase is None
        assert final.phase is None
        assert final.pending_upstream_stage == DocumentStage.SEQUENCE
        # 上位へ戻る離脱では stage をやり直すので、再開位置は残さない。
        assert final.return_phase is None
        assert not stage_completed(final, CLASS_STAGE)

    def test_upstream_change_without_a_target_is_rejected(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """どの上位工程が問題かを Controller が推測しない。"""
        handlers = ScriptedPhaseHandlers(
            {Phase.GENERATE: [Event.UPSTREAM_CHANGE_REQUIRED]}
        ).as_handlers()

        with pytest.raises(MissingUpstreamTargetError):
            run_document_stage(design_run, CLASS_STAGE, logger, handlers)

    def test_resource_limit_stops_the_stage(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [
                    StageResult(
                        event=Event.WORKER_RESOURCE_LIMIT,
                        worker=Worker.CLAUDE_CODE,
                        reason="session limit",
                    )
                ],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.current_state == State.WAIT_RESOURCE
        assert final.return_state == State.DESIGN
        assert final.substate == DocumentStage.CLASS


class TestRetryLimit:
    def test_exceeding_max_review_retry_leaves_the_stage(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """FIX → レビューを max_review_retry 回まで許し、超えたら RETRY_LIMIT。"""
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.LOCAL_FIX],  # 何度でも LOCAL_FIX を返す
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.return_state == State.DESIGN

        events = [item.event for item in logger.history(final.run_id)]
        assert events[-1] == Event.RETRY_LIMIT
        assert Event.NO_PROGRESS not in events
        assert final.review_retry == CLASS_STAGE.max_review_retry + 1

    def test_retry_limit_is_configurable_per_stage(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        patient = DocumentStageConfig(
            name=DocumentStage.CLASS, output="CLASS.md", max_review_retry=5
        )
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.LOCAL_FIX],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(design_run, patient, logger, handlers)
        assert final.review_retry == 6


class TestResume:
    def test_stage_resumes_from_the_phase_it_stopped_in(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§12: Worker を替えて、止まった位置から同じ stage を続ける。"""
        stopping = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.WORKER_RESOURCE_LIMIT, worker=Worker.CODEX_CLI
                    )
                ],
            }
        ).as_handlers()
        stopped = run_document_stage(design_run, CLASS_STAGE, logger, stopping)
        assert stopped.current_state == State.WAIT_RESOURCE
        assert stopped.substate == DocumentStage.CLASS
        assert stopped.return_phase == Phase.REVIEW_LIGHT

        # Worker を切り替えて DESIGN へ戻す。
        logger.record(
            stopped,
            Event.RESOURCE_AVAILABLE,
            to_substate=DocumentStage.CLASS,
            worker=Worker.CLAUDE_CODE,
        )
        assert stopped.current_state == State.DESIGN

        # GENERATE からやり直さず、止まった REVIEW_LIGHT へ戻る。
        resumed = run_document_stage(stopped, CLASS_STAGE, logger, stub_phase_handlers())
        assert stage_completed(resumed, CLASS_STAGE)
        assert resumed.active_worker == Worker.CLAUDE_CODE
        assert resumed.return_phase is None

        after_resume = [item.to_phase for item in logger.history(resumed.run_id)][-2:]
        assert after_resume == [Phase.REVIEW_LIGHT, Phase.COMPLETE]

    def test_resume_keeps_the_review_retry_budget(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """resource limit を挟んで retry 予算がリセットされないこと。

        リセットされると、thrash している stage が resource limit を挟むだけで
        RETRY_LIMIT を回避できてしまう。
        """
        stopping = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.LOCAL_FIX, Event.WORKER_RESOURCE_LIMIT],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()
        stopped = run_document_stage(design_run, CLASS_STAGE, logger, stopping)
        assert stopped.review_retry == 1

        logger.record(stopped, Event.RESOURCE_AVAILABLE, to_substate=DocumentStage.CLASS)

        thrashing = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.LOCAL_FIX],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()
        final = run_document_stage(stopped, CLASS_STAGE, logger, thrashing)

        assert final.current_state == State.HUMAN_REQUIRED
        assert [item.event for item in logger.history(final.run_id)][-1] == Event.RETRY_LIMIT
        assert final.review_retry == CLASS_STAGE.max_review_retry + 1

    def test_run_already_inside_a_phase_is_not_restarted(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        design_run.substate = DocumentStage.CLASS
        design_run.phase = Phase.REVIEW_LIGHT
        design_run.review_phase = Phase.REVIEW_LIGHT
        store.save_run(design_run)

        final = run_document_stage(design_run, CLASS_STAGE, logger)

        assert phases(logger, final.run_id) == [Phase.COMPLETE]


class TestWiring:
    def test_stub_handlers_cover_every_working_phase(self) -> None:
        handlers = stub_phase_handlers()
        assert set(handlers) == {
            Phase.GENERATE,
            Phase.REVIEW_LIGHT,
            Phase.REVIEW_DEEP,
            Phase.FIX,
            Phase.QANDA,
        }

    def test_missing_phase_handler_is_rejected(self, logger: TransitionLogger) -> None:
        handlers = stub_phase_handlers()
        del handlers[Phase.QANDA]
        with pytest.raises(ValueError, match="QANDA"):
            build_document_stage(CLASS_STAGE, logger, handlers)

    def test_human_log_shows_the_phase_path(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.LOCAL_FIX,
                        worker=Worker.CODEX_CLI,
                        reason="class naming mismatch",
                    ),
                    Event.PASS,
                ],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        rendered = logger.render(design_run.run_id, UTC)
        assert "DESIGN/CLASS/REVIEW_LIGHT | LOCAL_FIX" in rendered
        assert "-> DESIGN/CLASS/FIX" in rendered
        assert "reason=class naming mismatch" in rendered
