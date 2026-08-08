"""SQLite 永続化のテスト。"""

from __future__ import annotations

from pathlib import Path

from agent_controller.models import (
    ArtifactKind,
    ArtifactState,
    ArtifactStatus,
    DocumentStage,
    Event,
    Phase,
    Role,
    RunState,
    RunStatus,
    State,
    Transition,
    Worker,
)
from agent_controller.store import Store


class TestRuns:
    def test_round_trip(self, store: Store) -> None:
        run = RunState(
            project_id="agent-controller",
            run_id="run-a",
            current_state=State.DESIGN,
            substate=DocumentStage.CLASS,
            phase=Phase.REVIEW_LIGHT,
            active_role=Role.REVIEWER,
            active_worker=Worker.CODEX_CLI,
            checkpoint_commit="deadbee",
            state_retry=2,
            transition_count=7,
        )
        store.save_run(run)

        loaded = store.load_run("run-a")
        assert loaded is not None
        assert loaded.current_state == State.DESIGN
        assert loaded.substate == DocumentStage.CLASS
        assert loaded.phase == Phase.REVIEW_LIGHT
        assert loaded.active_worker == Worker.CODEX_CLI
        assert loaded.checkpoint_commit == "deadbee"
        assert loaded.state_retry == 2
        assert loaded.transition_count == 7
        assert loaded.status == RunStatus.RUNNING

    def test_save_is_upsert(self, store: Store, run: RunState) -> None:
        run.current_state = State.IMPLEMENT
        store.save_run(run)

        assert len(store.list_runs()) == 1
        reloaded = store.load_run(run.run_id)
        assert reloaded is not None
        assert reloaded.current_state == State.IMPLEMENT

    def test_missing_run_is_none(self, store: Store) -> None:
        assert store.load_run("nope") is None

    def test_survives_reopening_the_file(self, tmp_path: Path) -> None:
        db = tmp_path / "nested" / "controller.db"
        with Store(db) as store:
            store.save_run(RunState(project_id="p", run_id="run-b", current_state=State.TEST))

        with Store(db) as reopened:
            loaded = reopened.load_run("run-b")
            assert loaded is not None
            assert loaded.current_state == State.TEST


class TestTransitions:
    def test_appended_in_order(self, store: Store, run: RunState) -> None:
        for event, to_state in (
            (Event.START, State.DESIGN),
            (Event.PASS, State.IMPLEMENT),
        ):
            store.append_transition(
                Transition(
                    run_id=run.run_id,
                    state=State.IDLE,
                    from_state=State.IDLE,
                    event=event,
                    to_state=to_state,
                )
            )

        history = store.transitions(run.run_id)
        assert [item.event for item in history] == [Event.START, Event.PASS]

    def test_all_fifteen_log_fields_survive(self, store: Store, run: RunState) -> None:
        store.append_transition(
            Transition(
                run_id=run.run_id,
                state=State.DESIGN,
                substate=DocumentStage.CLASS,
                phase=Phase.REVIEW_LIGHT,
                from_state=State.DESIGN,
                from_substate=DocumentStage.CLASS,
                event=Event.LOCAL_FIX,
                to_state=State.DESIGN,
                to_substate=DocumentStage.CLASS,
                to_phase=Phase.FIX,
                role=Role.REVIEWER,
                worker=Worker.CODEX_CLI,
                reason="naming mismatch",
                state_retry=1,
                checkpoint_commit="abc1234",
            )
        )

        stored = store.transitions(run.run_id)[0]
        assert stored.substate == DocumentStage.CLASS
        assert stored.phase == Phase.REVIEW_LIGHT
        assert stored.to_phase == Phase.FIX
        assert stored.worker == Worker.CODEX_CLI
        assert stored.reason == "naming mismatch"
        assert stored.state_retry == 1
        assert stored.checkpoint_commit == "abc1234"

    def test_scoped_to_run(self, store: Store, run: RunState) -> None:
        other = RunState(project_id="p", run_id="run-other")
        store.save_run(other)
        store.append_transition(
            Transition(
                run_id=other.run_id,
                state=State.IDLE,
                from_state=State.IDLE,
                event=Event.START,
                to_state=State.DESIGN,
            )
        )

        assert store.transitions(run.run_id) == []
        assert len(store.transitions(other.run_id)) == 1


class TestArtifacts:
    def test_status_upsert(self, store: Store, run: RunState) -> None:
        store.save_artifact(
            ArtifactState(run_id=run.run_id, kind=ArtifactKind.CLASS, path="CLASS.md")
        )
        assert store.artifacts(run.run_id)[ArtifactKind.CLASS].status == ArtifactStatus.VALID

        store.save_artifact(
            ArtifactState(
                run_id=run.run_id,
                kind=ArtifactKind.CLASS,
                path="CLASS.md",
                status=ArtifactStatus.STALE,
                reason="SPEC changed",
            )
        )

        artifacts = store.artifacts(run.run_id)
        assert len(artifacts) == 1
        assert artifacts[ArtifactKind.CLASS].status == ArtifactStatus.STALE
        assert artifacts[ArtifactKind.CLASS].reason == "SPEC changed"

    def test_three_statuses_coexist(self, store: Store, run: RunState) -> None:
        expected = {
            ArtifactKind.USECASE: ArtifactStatus.VALID,
            ArtifactKind.SEQUENCE: ArtifactStatus.REVIEW_REQUIRED,
            ArtifactKind.CODE: ArtifactStatus.STALE,
        }
        for kind, status in expected.items():
            store.save_artifact(ArtifactState(run_id=run.run_id, kind=kind, status=status))

        stored = {kind: item.status for kind, item in store.artifacts(run.run_id).items()}
        assert stored == expected
