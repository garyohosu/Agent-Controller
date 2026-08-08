"""無限ループ防止のテスト（指示書 §11 / §17-9）。

AI に判定させない機械的な歯止めなので、テストも「何回で止まるか」を直接見る。
"""

from __future__ import annotations

from datetime import timezone

from agent_controller.design import default_design_stages, run_design
from agent_controller.document_stage import (
    DocumentStageConfig,
    ScriptedPhaseHandlers,
    StageResult,
    run_document_stage,
)
from agent_controller.graph import ScriptedHandlers
from agent_controller.graph import StageResult as TopStageResult
from agent_controller.graph import run_graph
from agent_controller.guards import (
    TRACKED_EVENTS,
    GuardLimits,
    LoopGuard,
    NoProgressTracker,
    check_counters,
    failure_fingerprint,
)
from agent_controller.models import (
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

CLASS_STAGE = DocumentStageConfig(
    name=DocumentStage.CLASS,
    output="CLASS.md",
    review_level=ReviewLevel.LIGHT,
    max_review_retry=99,  # stage 側の上限では止めず、run 全体の guard を見る
)


def make_run(**overrides) -> RunState:
    return RunState(project_id="p", run_id="run-guard", **overrides)


class TestCounterLimits:
    def test_within_limits_is_silent(self) -> None:
        assert check_counters(make_run(), GuardLimits()) is None

    def test_upstream_rework_is_a_loop(self) -> None:
        run = make_run(upstream_rework=4)
        verdict = check_counters(run, GuardLimits(max_upstream_rework=3))
        assert verdict is not None
        assert verdict.event == Event.LOOP_DETECTED
        assert "upstream rework" in verdict.reason

    def test_same_transition_is_a_loop(self) -> None:
        verdict = check_counters(make_run(repeat=4), GuardLimits(max_same_transition=3))
        assert verdict is not None
        assert verdict.event == Event.LOOP_DETECTED

    def test_state_retry_is_a_retry_limit_not_a_loop(self) -> None:
        """回数の超過と、同じところを回り続けることは別の話。"""
        verdict = check_counters(make_run(state_retry=6), GuardLimits(max_state_retry=5))
        assert verdict is not None
        assert verdict.event == Event.RETRY_LIMIT

    def test_total_transitions_is_a_backstop(self) -> None:
        verdict = check_counters(
            make_run(transition_count=501), GuardLimits(max_total_transitions=500)
        )
        assert verdict is not None
        assert verdict.event == Event.LOOP_DETECTED

    def test_limits_are_configurable(self) -> None:
        run = make_run(repeat=2)
        assert check_counters(run, GuardLimits(max_same_transition=3)) is None
        assert check_counters(run, GuardLimits(max_same_transition=1)) is not None

    def test_first_match_wins(self) -> None:
        """複数同時に超えたときは、具体的な事象を表すものを先に返す。"""
        run = make_run(upstream_rework=9, repeat=9, state_retry=9, transition_count=9999)
        verdict = check_counters(run, GuardLimits())
        assert verdict is not None
        assert "upstream rework" in verdict.reason


class TestFingerprint:
    def test_same_failure_has_the_same_fingerprint(self) -> None:
        a = failure_fingerprint(
            State.DESIGN, DocumentStage.CLASS, Phase.REVIEW_LIGHT, Event.LOCAL_FIX, "naming"
        )
        b = failure_fingerprint(
            State.DESIGN, DocumentStage.CLASS, Phase.REVIEW_LIGHT, Event.LOCAL_FIX, " Naming "
        )
        assert a == b

    def test_different_reason_is_a_different_failure(self) -> None:
        a = failure_fingerprint(State.TEST, None, None, Event.FAIL, "test_a failed")
        b = failure_fingerprint(State.TEST, None, None, Event.FAIL, "test_b failed")
        assert a != b

    def test_progress_events_are_not_tracked(self) -> None:
        assert Event.PASS not in TRACKED_EVENTS
        assert Event.DONE not in TRACKED_EVENTS
        assert Event.FAIL in TRACKED_EVENTS
        assert Event.LOCAL_FIX in TRACKED_EVENTS

    def test_repeated_failure_becomes_no_progress(self, store: Store) -> None:
        tracker = NoProgressTracker(store, GuardLimits(max_same_fingerprint=3))
        run = make_run()
        store.save_run(run)

        verdicts = [
            tracker.observe(
                run, Event.FAIL, State.TEST, None, None, "same failure", Worker.CLAUDE_CODE
            )
            for _ in range(4)
        ]

        assert verdicts[:3] == [None, None, None]
        assert verdicts[3] is not None
        assert verdicts[3].event == Event.NO_PROGRESS
        # どの失敗が繰り返されたのかが理由から分かる。
        assert "FAIL at TEST" in verdicts[3].reason

    def test_same_failure_after_a_worker_switch_stops_immediately(
        self, store: Store
    ) -> None:
        """§11: Worker を変更しても同一失敗なら HUMAN_REQUIRED。

        Worker の調子ではなく仕様か設計の問題なので、回数を待たない。
        """
        tracker = NoProgressTracker(store, GuardLimits(max_same_fingerprint=99))
        run = make_run()
        store.save_run(run)

        first = tracker.observe(
            run, Event.FAIL, State.TEST, None, None, "boom", Worker.CLAUDE_CODE
        )
        second = tracker.observe(
            run, Event.FAIL, State.TEST, None, None, "boom", Worker.CODEX_CLI
        )

        assert first is None
        assert second is not None
        assert second.event == Event.NO_PROGRESS
        assert "worker switch" in second.reason
        assert "FAIL at TEST" in second.reason

    def test_progress_events_never_fingerprint(self, store: Store) -> None:
        tracker = NoProgressTracker(store, GuardLimits(max_same_fingerprint=1))
        run = make_run()
        store.save_run(run)

        for _ in range(5):
            assert (
                tracker.observe(
                    run, Event.PASS, State.TEST, None, None, None, Worker.CLAUDE_CODE
                )
                is None
            )
        assert store.fingerprints(run.run_id) == {}


class TestSuspensionIsNotALoop:
    def test_repeated_resource_waits_do_not_trip_a_guard(
        self, logger: TransitionLogger
    ) -> None:
        """rate limit を 3 回待っただけの run は回っていない。"""
        run = make_run(current_state=State.IMPLEMENT)
        logger.store.save_run(run)

        for _ in range(3):
            logger.record(run, Event.WORKER_RESOURCE_LIMIT, worker=Worker.CLAUDE_CODE)
            assert run.current_state == State.WAIT_RESOURCE
            logger.record(run, Event.RESOURCE_AVAILABLE, worker=Worker.CODEX_CLI)
            assert run.current_state == State.IMPLEMENT

        assert run.state_retry == 0
        assert run.repeat == 0
        assert check_counters(run, GuardLimits()) is None


class TestStageDriver:
    def test_guard_stops_a_thrashing_stage(
        self, logger: TransitionLogger
    ) -> None:
        """同じ指摘が繰り返されると NO_PROGRESS で人間へ渡る。"""
        run = make_run(current_state=State.DESIGN)
        logger.store.save_run(run)

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.LOCAL_FIX,
                        worker=Worker.CODEX_CLI,
                        reason="class naming mismatch",
                    )
                ],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(
            run,
            CLASS_STAGE,
            logger,
            handlers,
            guard=LoopGuard(logger.store, GuardLimits(max_same_fingerprint=3)),
        )

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.status == RunStatus.WAITING
        assert final.return_state == State.DESIGN

        events = [item.event for item in logger.history(final.run_id)]
        assert events[-1] == Event.NO_PROGRESS
        # 起きたことと、歯止めが働いたことが別の行として残る。
        assert events[-2] == Event.LOCAL_FIX

    def test_no_guard_means_no_stop(self, logger: TransitionLogger) -> None:
        """guard を渡さなければ止まらない。歯止めは明示的に付ける。"""
        run = make_run(current_state=State.DESIGN)
        logger.store.save_run(run)

        stage = DocumentStageConfig(
            name=DocumentStage.CLASS, output="CLASS.md", max_review_retry=2
        )
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [Event.LOCAL_FIX],
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()

        final = run_document_stage(run, stage, logger, handlers)

        # stage 側の max_review_retry で止まるので、こちらは RETRY_LIMIT。
        assert [item.event for item in logger.history(final.run_id)][-1] == Event.RETRY_LIMIT

    def test_repeat_does_not_carry_across_stages(self, logger: TransitionLogger) -> None:
        """stage をまたいだ同じ形の遷移は repeat に数えない。

        CLASS のやり直しは upstream_rework が数える担当。
        """
        run = make_run(current_state=State.DESIGN)
        logger.store.save_run(run)

        after_class = run_document_stage(run, CLASS_STAGE, logger)
        assert after_class.repeat == 0
        assert after_class.last_transition_key is not None

        spec = DocumentStageConfig(name=DocumentStage.SPEC, output="SPEC.md")
        after_spec = run_document_stage(after_class, spec, logger)
        assert after_spec.repeat == 0

        # stage 開始行で指紋が切れているので、前 stage の遷移とは繋がらない。
        starts = [
            item
            for item in logger.history(run.run_id)
            if item.event == Event.START and item.to_substate == DocumentStage.SPEC
        ]
        assert len(starts) == 1
        assert starts[0].repeat == 0


class TestMainGraphDriver:
    def test_guard_stops_a_test_implement_ping_pong(
        self, logger: TransitionLogger
    ) -> None:
        run = make_run()
        logger.store.save_run(run)

        handlers = ScriptedHandlers(
            {
                State.IDLE: [Event.START],
                State.DESIGN: [Event.PASS],
                State.IMPLEMENT: [
                    TopStageResult(event=Event.DONE, worker=Worker.CLAUDE_CODE)
                ],
                State.TEST: [
                    TopStageResult(
                        event=Event.FAIL,
                        worker=Worker.CLAUDE_CODE,
                        reason="test_login always fails",
                    )
                ],
                State.REVIEW: [Event.PASS],
                State.DOC_SYNC: [Event.PASS],
            }
        ).as_handlers()

        final = run_graph(
            run,
            logger,
            handlers,
            guard=LoopGuard(logger.store, GuardLimits(max_same_fingerprint=3)),
        )

        assert final.current_state == State.HUMAN_REQUIRED
        events = [item.event for item in logger.history(final.run_id)]
        assert events[-1] == Event.NO_PROGRESS
        assert events.count(Event.FAIL) == 4


class TestDesignDriver:
    def test_guard_stops_repeated_upstream_rework(
        self, logger: TransitionLogger
    ) -> None:
        run = make_run(current_state=State.DESIGN)
        logger.store.save_run(run)

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SEQUENCE,
                        reason="still mismatched",
                    ),
                ],
            }
        ).as_handlers()

        final = run_design(
            run,
            logger,
            default_design_stages(),
            handlers,
            guard=LoopGuard(
                logger.store,
                GuardLimits(max_upstream_rework=2, max_same_fingerprint=99),
            ),
        )

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.upstream_rework == 3
        assert [item.event for item in logger.history(final.run_id)][-1] == (
            Event.LOOP_DETECTED
        )

    def test_design_uses_a_guard_even_when_none_is_passed(
        self, logger: TransitionLogger
    ) -> None:
        """歯止め無しでは回さない。"""
        run = make_run(current_state=State.DESIGN)
        logger.store.save_run(run)

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SEQUENCE,
                        reason="still mismatched",
                    ),
                ],
            }
        ).as_handlers()

        final = run_design(run, logger, handlers=handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.status == RunStatus.WAITING

    def test_happy_path_is_not_disturbed_by_the_guard(
        self, logger: TransitionLogger
    ) -> None:
        run = make_run(current_state=State.DESIGN)
        logger.store.save_run(run)

        final = run_design(run, logger)

        assert final.current_state == State.IMPLEMENT
        assert logger.store.fingerprints(final.run_id) == {}
