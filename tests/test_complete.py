from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_controller.complete import (
    CompleteBlockerCode,
    CompleteGate,
    CompleteGateError,
)
from agent_controller.models import (
    ArtifactKind,
    ArtifactState,
    ArtifactStatus,
    Event,
    RunState,
    State,
    Transition,
)
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger


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


def seed_ready(store: Store, root: Path) -> RunState:
    run = RunState(project_id="p", run_id="r", current_state=State.DOC_SYNC)
    store.save_run(run)
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    for kind in (
        ArtifactKind.SPEC, ArtifactKind.USECASE, ArtifactKind.SEQUENCE,
        ArtifactKind.CLASS, ArtifactKind.TESTCASE,
    ):
        store.save_artifact(ArtifactState(run_id="r", kind=kind, updated_at=old))
    store.save_artifact(ArtifactState(
        run_id="r", kind=ArtifactKind.CODE, updated_at=old,
    ))
    store.save_artifact(ArtifactState(
        run_id="r", kind=ArtifactKind.README, reason="LATEST", updated_at=old,
    ))
    for state in (State.TEST, State.REVIEW):
        store.append_transition(Transition(
            run_id="r", state=state, from_state=state, to_state=state,
            event=Event.PASS,
        ))
    return run


def codes(result) -> set[CompleteBlockerCode]:
    return {item.code for item in result.blockers}


def test_all_evidence_is_required_and_ready_when_present(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        result = CompleteGate(store, root).check(run)
        assert result.ready
        assert result.blockers == []


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ArtifactKind.CLASS, CompleteBlockerCode.ARTIFACT_NOT_VALID),
        (ArtifactKind.CODE, CompleteBlockerCode.CODE_NOT_LATEST),
        (ArtifactKind.README, CompleteBlockerCode.README_NOT_SYNCED),
    ],
)
def test_artifact_blockers_are_structured(tmp_path: Path, kind, expected) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        artifact = store.artifacts("r")[kind]
        artifact.status = ArtifactStatus.STALE
        store.save_artifact(artifact)
        assert expected in codes(CompleteGate(store, root).check(run))


def test_test_and_review_pass_must_be_current(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        code = store.artifacts("r")[ArtifactKind.CODE]
        code.updated_at = datetime.now(timezone.utc)
        store.save_artifact(code)
        result = CompleteGate(store, root).check(run)
        assert {CompleteBlockerCode.TEST_NOT_PASS, CompleteBlockerCode.REVIEW_NOT_PASS} <= codes(result)


def test_questions_table_is_authoritative_not_qanda_markdown(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        (root / "QandA.md").write_text("- Waiting on a human: 99\n")
        result = CompleteGate(store, root).check(run)
        assert CompleteBlockerCode.OPEN_QUESTION not in codes(result)
        assert CompleteBlockerCode.HUMAN_REQUIRED_QUESTION not in codes(result)


def test_open_and_human_questions_block_separately(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        from agent_controller.qanda import QandaFile

        QandaFile(store, root).open_question(run, "open")
        assert CompleteBlockerCode.OPEN_QUESTION in codes(CompleteGate(store, root).check(run))


def test_dirty_and_not_pushed_are_git_blockers(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        (root / "README.md").write_text("dirty\n")
        result = CompleteGate(store, root).check(run)
        assert CompleteBlockerCode.GIT_DIRTY in codes(result)
        git(root, "checkout", "--", "README.md")
        git(root, "commit", "--allow-empty", "-qm", "local")
        assert CompleteBlockerCode.GIT_NOT_PUSHED in codes(CompleteGate(store, root).check(run))


def test_complete_transition_requires_gate(tmp_path: Path) -> None:
    root = pushed_repo(tmp_path)
    with Store(tmp_path / "controller.db") as store:
        run = seed_ready(store, root)
        logger = TransitionLogger(store, CompleteGate(store, root))
        with pytest.raises(CompleteGateError):
            # Seeded run is ready, so make the gate fail before attempting the event.
            readme = store.artifacts("r")[ArtifactKind.README]
            readme.reason = "STALE"
            store.save_artifact(readme)
            logger.record(run, Event.PASS)
