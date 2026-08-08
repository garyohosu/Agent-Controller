"""Main Graph の受け入れテスト（指示書 §17-5 の到達点）。

happy path 1 本と分岐 1 本を stub Worker で流し、SQLite の遷移行と
人間向けログの両方を確認する。
"""

from __future__ import annotations

from datetime import timezone

import pytest

from agent_controller.graph import (
    EXECUTABLE_STATES,
    ScriptedHandlers,
    StageResult,
    build_main_graph,
    run_graph,
    stub_handlers,
)
from agent_controller.models import Event, Role, RunState, RunStatus, State, Worker
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

UTC = timezone.utc

HAPPY_PATH = [
    State.DESIGN,
    State.IMPLEMENT,
    State.TEST,
    State.REVIEW,
    State.DOC_SYNC,
    State.COMPLETE,
]


class TestHappyPath:
    def test_reaches_complete(self, logger: TransitionLogger, run: RunState) -> None:
        final = run_graph(run, logger)

        assert final.current_state == State.COMPLETE
        assert final.status == RunStatus.COMPLETED

    def test_transition_rows_record_the_whole_route(
        self, logger: TransitionLogger, run: RunState
    ) -> None:
        run_graph(run, logger)

        history = logger.history(run.run_id)
        assert [item.to_state for item in history] == HAPPY_PATH
        assert [item.from_state for item in history] == [State.IDLE, *HAPPY_PATH[:-1]]
        assert [item.event for item in history] == [
            Event.START,
            Event.PASS,
            Event.DONE,
            Event.PASS,
            Event.PASS,
            Event.PASS,
        ]

    def test_final_run_is_persisted(
        self, store: Store, logger: TransitionLogger, run: RunState
    ) -> None:
        run_graph(run, logger)

        reloaded = store.load_run(run.run_id)
        assert reloaded is not None
        assert reloaded.current_state == State.COMPLETE
        assert reloaded.transition_count == len(HAPPY_PATH)


class TestBranch:
    def test_test_failure_loops_back_through_implement(
        self, logger: TransitionLogger, run: RunState
    ) -> None:
        """TEST FAIL → IMPLEMENT → TEST → REVIEW → ... → COMPLETE。"""
        handlers = ScriptedHandlers(
            {
                State.IDLE: [Event.START],
                State.DESIGN: [Event.PASS],
                State.IMPLEMENT: [
                    StageResult(
                        event=Event.DONE,
                        role=Role.IMPLEMENTER,
                        worker=Worker.CLAUDE_CODE,
                    )
                ],
                State.TEST: [
                    StageResult(event=Event.FAIL, reason="1 failing test"),
                    StageResult(event=Event.PASS),
                ],
                State.REVIEW: [
                    StageResult(event=Event.PASS, worker=Worker.CODEX_CLI),
                ],
                State.DOC_SYNC: [Event.PASS],
            }
        ).as_handlers()

        final = run_graph(run, logger, handlers)

        assert final.current_state == State.COMPLETE
        assert [item.to_state for item in logger.history(run.run_id)] == [
            State.DESIGN,
            State.IMPLEMENT,
            State.TEST,
            State.IMPLEMENT,
            State.TEST,
            State.REVIEW,
            State.DOC_SYNC,
            State.COMPLETE,
        ]

    def test_failure_reason_reaches_the_human_log(
        self, logger: TransitionLogger, run: RunState
    ) -> None:
        handlers = ScriptedHandlers(
            {
                State.IDLE: [Event.START],
                State.DESIGN: [Event.PASS],
                State.IMPLEMENT: [Event.DONE],
                State.TEST: [
                    StageResult(event=Event.FAIL, reason="1 failing test"),
                    StageResult(event=Event.PASS),
                ],
                State.REVIEW: [Event.PASS],
                State.DOC_SYNC: [Event.PASS],
            }
        ).as_handlers()

        run_graph(run, logger, handlers)

        rendered = logger.render(run.run_id, UTC)
        assert "TEST | FAIL" in rendered
        assert "reason=1 failing test" in rendered
        assert "-> IMPLEMENT" in rendered

    def test_run_stops_at_human_required(
        self, store: Store, logger: TransitionLogger, run: RunState
    ) -> None:
        handlers = ScriptedHandlers(
            {
                State.IDLE: [Event.START],
                State.DESIGN: [Event.PASS],
                State.IMPLEMENT: [
                    StageResult(event=Event.CANNOT_ANSWER, reason="spec silent on retry policy")
                ],
                State.TEST: [Event.PASS],
                State.REVIEW: [Event.PASS],
                State.DOC_SYNC: [Event.PASS],
            }
        ).as_handlers()

        final = run_graph(run, logger, handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.status == RunStatus.WAITING
        assert final.return_state == State.IMPLEMENT

        reloaded = store.load_run(run.run_id)
        assert reloaded is not None
        assert reloaded.return_state == State.IMPLEMENT

    def test_run_stops_at_wait_resource(
        self, logger: TransitionLogger, run: RunState
    ) -> None:
        handlers = ScriptedHandlers(
            {
                State.IDLE: [Event.START],
                State.DESIGN: [Event.PASS],
                State.IMPLEMENT: [
                    StageResult(
                        event=Event.WORKER_RESOURCE_LIMIT,
                        worker=Worker.CLAUDE_CODE,
                        reason="session limit",
                    )
                ],
                State.TEST: [Event.PASS],
                State.REVIEW: [Event.PASS],
                State.DOC_SYNC: [Event.PASS],
            }
        ).as_handlers()

        final = run_graph(run, logger, handlers)

        assert final.current_state == State.WAIT_RESOURCE
        assert final.return_state == State.IMPLEMENT
        assert "reason=session limit" in logger.render(run.run_id, UTC)


class TestGraphWiring:
    def test_default_handlers_cover_every_executable_state(self) -> None:
        assert set(stub_handlers()) == set(EXECUTABLE_STATES)

    def test_missing_handler_is_rejected(self, logger: TransitionLogger) -> None:
        handlers = stub_handlers()
        del handlers[State.REVIEW]
        with pytest.raises(ValueError, match="REVIEW"):
            build_main_graph(logger, handlers)

    def test_resumed_run_continues_from_its_state(
        self, logger: TransitionLogger, store: Store
    ) -> None:
        """WAIT_RESOURCE で止めた run を、Worker を替えて同じ State から再開する（§12）。"""
        run = RunState(project_id="p", run_id="run-resume")
        store.save_run(run)

        stopping = ScriptedHandlers(
            {
                State.IDLE: [Event.START],
                State.DESIGN: [Event.PASS],
                State.IMPLEMENT: [
                    StageResult(event=Event.WORKER_RESOURCE_LIMIT, worker=Worker.CLAUDE_CODE)
                ],
                State.TEST: [Event.PASS],
                State.REVIEW: [Event.PASS],
                State.DOC_SYNC: [Event.PASS],
            }
        ).as_handlers()
        stopped = run_graph(run, logger, stopping)
        assert stopped.current_state == State.WAIT_RESOURCE

        # Worker を切り替えてから同じ State を再実行する。
        logger.record(stopped, Event.RESOURCE_AVAILABLE, worker=Worker.CODEX_CLI)
        assert stopped.current_state == State.IMPLEMENT

        resumed = run_graph(stopped, logger, stub_handlers())
        assert resumed.current_state == State.COMPLETE
        assert resumed.active_worker == Worker.CODEX_CLI

        events = [item.event for item in logger.history(run.run_id)]
        assert Event.WORKER_RESOURCE_LIMIT in events
        assert Event.RESOURCE_AVAILABLE in events
