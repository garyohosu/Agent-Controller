"""State Transition Logger のテスト（指示書 §10）。"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    Role,
    RunState,
    State,
    Transition,
    Worker,
)
from agent_controller.store import Store
from agent_controller.transition_log import (
    TransitionLogger,
    format_position,
    render_log,
    render_transition,
)

UTC = timezone.utc


def at(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 8, 8, hour, minute, second, tzinfo=UTC)


class TestFormatPosition:
    def test_drops_missing_levels(self) -> None:
        assert format_position(State.IMPLEMENT) == "IMPLEMENT"
        assert format_position(State.DESIGN, DocumentStage.CLASS) == "DESIGN/CLASS"
        assert (
            format_position(State.DESIGN, DocumentStage.CLASS, Phase.REVIEW_LIGHT)
            == "DESIGN/CLASS/REVIEW_LIGHT"
        )


class TestRender:
    def test_counters_go_on_their_own_line(self) -> None:
        """意味の違うカウンタを 1 つの retry= に混ぜない。

        §10 の表示例は phase を REVIEW と書き、カウンタを retry= 1 つにまとめて
        いるが、どちらも後の決定で置き換わっている（REVIEW_LIGHT / REVIEW_DEEP、
        state_retry / review_retry / repeat）。
        """
        transition = Transition(
            timestamp=at(19, 10, 12),
            run_id="run-1",
            state=State.DESIGN,
            substate=DocumentStage.CLASS,
            phase=Phase.REVIEW_LIGHT,
            from_state=State.DESIGN,
            from_substate=DocumentStage.CLASS,
            event=Event.LOCAL_FIX,
            to_state=State.DESIGN,
            to_substate=DocumentStage.CLASS,
            to_phase=Phase.FIX,
            worker=Worker.CODEX_CLI,
            review_retry=1,
            repeat=1,
        )

        assert render_transition(transition, UTC) == (
            "19:10:12 | DESIGN/CLASS/REVIEW_LIGHT | LOCAL_FIX\n"
            "         -> DESIGN/CLASS/FIX | worker=CODEX_CLI\n"
            "         | review_retry=1 | repeat=1"
        )

    def test_zero_counters_are_omitted(self) -> None:
        """正常に進んでいる行は 2 行のまま。"""
        transition = Transition(
            timestamp=at(19, 10, 12),
            run_id="run-1",
            state=State.DESIGN,
            substate=DocumentStage.SPEC,
            phase=Phase.GENERATE,
            from_state=State.DESIGN,
            event=Event.DONE,
            to_state=State.DESIGN,
            to_substate=DocumentStage.SPEC,
            to_phase=Phase.REVIEW_DEEP,
        )

        assert render_transition(transition, UTC).count("\n") == 1
        assert "repeat" not in render_transition(transition, UTC)

    def test_each_counter_is_named(self) -> None:
        transition = Transition(
            timestamp=at(19, 10, 12),
            run_id="run-1",
            state=State.DESIGN,
            from_state=State.DESIGN,
            event=Event.UPSTREAM_CHANGE_REQUIRED,
            to_state=State.DESIGN,
            state_retry=2,
            review_retry=1,
            repeat=3,
        )

        rendered = render_transition(transition, UTC)
        assert "| state_retry=2 | review_retry=1 | repeat=3" in rendered

    def test_reason_is_shown_for_upstream_returns(self) -> None:
        transition = Transition(
            timestamp=at(19, 13, 44),
            run_id="run-1",
            state=State.DESIGN,
            substate=DocumentStage.CLASS,
            phase=Phase.REVIEW_LIGHT,
            from_state=State.DESIGN,
            event=Event.UPSTREAM_CHANGE_REQUIRED,
            to_state=State.DESIGN,
            to_substate=DocumentStage.SEQUENCE,
            reason="sequence responsibility mismatch",
        )

        rendered = render_transition(transition, UTC)
        assert "UPSTREAM_CHANGE_REQUIRED" in rendered
        assert "-> DESIGN/SEQUENCE" in rendered
        assert "reason=sequence responsibility mismatch" in rendered

    def test_from_phase_reads_as_the_mirror_of_to_phase(self) -> None:
        """from_state/from_substate/from_phase と to_* が対称に読めること。"""
        transition = Transition(
            timestamp=at(19, 10, 12),
            run_id="run-1",
            state=State.DESIGN,
            substate=DocumentStage.CLASS,
            phase=Phase.REVIEW_LIGHT,
            from_state=State.DESIGN,
            from_substate=DocumentStage.CLASS,
            event=Event.LOCAL_FIX,
            to_state=State.DESIGN,
            to_substate=DocumentStage.CLASS,
            to_phase=Phase.FIX,
        )

        assert transition.from_phase == Phase.REVIEW_LIGHT
        assert transition.from_phase == transition.phase

    def test_omits_empty_details(self) -> None:
        transition = Transition(
            timestamp=at(9, 0, 0),
            run_id="run-1",
            state=State.IDLE,
            from_state=State.IDLE,
            event=Event.START,
            to_state=State.DESIGN,
        )
        assert render_transition(transition, UTC) == (
            "09:00:00 | IDLE | START\n         -> DESIGN"
        )

    def test_render_log_joins_lines(self) -> None:
        transitions = [
            Transition(
                timestamp=at(9, 0, 0),
                run_id="run-1",
                state=State.IDLE,
                from_state=State.IDLE,
                event=Event.START,
                to_state=State.DESIGN,
            ),
            Transition(
                timestamp=at(9, 5, 0),
                run_id="run-1",
                state=State.DESIGN,
                from_state=State.DESIGN,
                event=Event.PASS,
                to_state=State.IMPLEMENT,
            ),
        ]
        assert render_log(transitions, UTC).count("\n") == 3


class TestLogger:
    def test_record_persists_transition_and_run(
        self, store: Store, logger: TransitionLogger, run: RunState
    ) -> None:
        logger.record(run, Event.START)
        logger.record(run, Event.PASS, role=Role.DIRECTOR, worker=Worker.CLAUDE_CODE)

        history = logger.history(run.run_id)
        assert [item.event for item in history] == [Event.START, Event.PASS]
        assert [item.to_state for item in history] == [State.DESIGN, State.IMPLEMENT]

        reloaded = store.load_run(run.run_id)
        assert reloaded is not None
        assert reloaded.current_state == State.IMPLEMENT
        assert reloaded.transition_count == 2
        assert reloaded.active_worker == Worker.CLAUDE_CODE

    def test_render_reads_back_from_sqlite(
        self, logger: TransitionLogger, run: RunState
    ) -> None:
        logger.record(run, Event.START)
        rendered = logger.render(run.run_id, UTC)
        assert "IDLE | START" in rendered
        assert "-> DESIGN" in rendered

    def test_return_path_is_traceable_from_the_log(
        self, logger: TransitionLogger, run: RunState
    ) -> None:
        """§最初の実証: どの State で、どの Event により、どこへ戻ったか。"""
        logger.record(run, Event.START)
        logger.record(run, Event.PASS)
        logger.record(run, Event.DONE)
        logger.record(run, Event.PASS)
        logger.record(run, Event.UPSTREAM_CHANGE_REQUIRED, reason="spec gap")

        last = logger.history(run.run_id)[-1]
        assert last.from_state == State.REVIEW
        assert last.event == Event.UPSTREAM_CHANGE_REQUIRED
        assert last.to_state == State.DESIGN
        assert last.reason == "spec gap"
