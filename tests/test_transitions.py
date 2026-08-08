"""遷移表そのもののテスト。LangGraph も SQLite も使わない。"""

from __future__ import annotations

import pytest

from agent_controller.models import (
    PAUSE_STATES,
    TERMINAL_STATES,
    Event,
    RunState,
    RunStatus,
    State,
)
from agent_controller.transitions import (
    RESUME,
    TRANSITIONS,
    MissingReturnStateError,
    UnknownTransitionError,
    allowed_events,
    apply_event,
    next_state,
)


def make_run(**overrides) -> RunState:
    return RunState(project_id="p", run_id="r", **overrides)


class TestTable:
    def test_happy_path_chain(self) -> None:
        assert next_state(State.IDLE, Event.START) == State.DESIGN
        assert next_state(State.DESIGN, Event.PASS) == State.IMPLEMENT
        assert next_state(State.IMPLEMENT, Event.DONE) == State.TEST
        assert next_state(State.TEST, Event.PASS) == State.REVIEW
        assert next_state(State.REVIEW, Event.PASS) == State.DOC_SYNC
        assert next_state(State.DOC_SYNC, Event.PASS) == State.COMPLETE

    def test_test_failure_returns_to_implement(self) -> None:
        assert next_state(State.TEST, Event.FAIL) == State.IMPLEMENT

    def test_review_findings_go_through_implementer(self) -> None:
        """指示書 §8: Reviewer の指摘は必ず Implementer の修正を経由する。"""
        assert next_state(State.REVIEW, Event.FAIL) == State.IMPLEMENT
        assert next_state(State.REVIEW, Event.LOCAL_FIX) == State.IMPLEMENT

    def test_upstream_change_returns_to_design(self) -> None:
        assert next_state(State.REVIEW, Event.UPSTREAM_CHANGE_REQUIRED) == State.DESIGN
        assert next_state(State.IMPLEMENT, Event.UPSTREAM_CHANGE_REQUIRED) == State.DESIGN

    def test_resource_limit_waits_instead_of_failing(self) -> None:
        assert next_state(State.IMPLEMENT, Event.WORKER_RESOURCE_LIMIT) == State.WAIT_RESOURCE

    def test_loop_and_no_progress_escalate_to_human(self) -> None:
        for state in (State.DESIGN, State.IMPLEMENT, State.TEST, State.REVIEW):
            assert next_state(state, Event.LOOP_DETECTED) == State.HUMAN_REQUIRED
            assert next_state(state, Event.NO_PROGRESS) == State.HUMAN_REQUIRED

    def test_unknown_transition_raises(self) -> None:
        with pytest.raises(UnknownTransitionError):
            next_state(State.TEST, Event.START)

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        for state in TERMINAL_STATES:
            assert allowed_events(state) == frozenset()

    def test_every_state_is_reachable(self) -> None:
        reachable = {target for target in TRANSITIONS.values() if target != RESUME}
        reachable.add(State.IDLE)
        assert reachable == set(State)

    def test_resume_targets_are_dynamic_only_for_pause_states(self) -> None:
        for (state, _event), target in TRANSITIONS.items():
            if target == RESUME:
                assert state in PAUSE_STATES


class TestResume:
    def test_human_answer_returns_to_saved_state(self) -> None:
        run = make_run(current_state=State.IMPLEMENT)
        apply_event(run, Event.CANNOT_ANSWER)

        assert run.current_state == State.HUMAN_REQUIRED
        assert run.return_state == State.IMPLEMENT
        assert run.status == RunStatus.WAITING

        apply_event(run, Event.HUMAN_ANSWER)

        assert run.current_state == State.IMPLEMENT
        assert run.return_state is None
        assert run.status == RunStatus.RUNNING

    def test_resource_wait_resumes_same_state(self) -> None:
        run = make_run(current_state=State.REVIEW)
        apply_event(run, Event.WORKER_RESOURCE_LIMIT)
        assert run.current_state == State.WAIT_RESOURCE

        apply_event(run, Event.RESOURCE_AVAILABLE)
        assert run.current_state == State.REVIEW

    def test_resume_without_return_state_raises(self) -> None:
        run = make_run(current_state=State.HUMAN_REQUIRED)
        with pytest.raises(MissingReturnStateError):
            apply_event(run, Event.HUMAN_ANSWER)


class TestApplyEvent:
    def test_records_origin_and_target(self) -> None:
        run = make_run(current_state=State.TEST)
        transition = apply_event(run, Event.FAIL, reason="2 failing tests")

        assert transition.from_state == State.TEST
        assert transition.state == State.TEST
        assert transition.to_state == State.IMPLEMENT
        assert transition.event == Event.FAIL
        assert transition.reason == "2 failing tests"
        assert run.previous_state == State.TEST
        assert run.current_state == State.IMPLEMENT
        assert run.transition_count == 1

    def test_retry_count_increments_on_self_loop_and_resets_on_move(self) -> None:
        run = make_run(current_state=State.DESIGN)

        assert apply_event(run, Event.LOCAL_FIX).retry_count == 1
        assert apply_event(run, Event.LOCAL_FIX).retry_count == 2
        assert apply_event(run, Event.PASS).retry_count == 0
        assert run.current_state == State.IMPLEMENT

    def test_status_tracks_state(self) -> None:
        run = make_run(current_state=State.DOC_SYNC)
        apply_event(run, Event.PASS)
        assert run.current_state == State.COMPLETE
        assert run.status == RunStatus.COMPLETED

        aborting = make_run(current_state=State.IDLE)
        apply_event(aborting, Event.ABORT_REQUESTED)
        assert aborting.status == RunStatus.ABORTED

    def test_checkpoint_commit_is_carried_into_the_log(self) -> None:
        run = make_run(current_state=State.IMPLEMENT, checkpoint_commit="abc1234")
        transition = apply_event(run, Event.DONE)
        assert transition.checkpoint_commit == "abc1234"
