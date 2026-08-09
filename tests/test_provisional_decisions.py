from __future__ import annotations

from pathlib import Path

from agent_controller.complete import CompleteBlockerCode, CompleteGate
from agent_controller.human import answer_batch
from agent_controller.models import DocumentStage, Event, Phase, QuestionStatus, RunState, State
from agent_controller.qanda import QandaFile
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger


def test_low_risk_provisional_is_persisted_and_normal_complete_blocks(tmp_path: Path) -> None:
    with Store(tmp_path / "controller.db") as store:
        run = RunState(
            project_id="p",
            run_id="r",
            current_state=State.DESIGN,
            substate=DocumentStage.SPEC,
            phase=Phase.REVIEW_DEEP,
        )
        store.save_run(run)
        qanda = QandaFile(store, tmp_path)
        question = qanda.open_question(run, "What does an omitted argument mean?")
        qanda.provisional_decision(
            question,
            "Treat omission as the empty value for this run.",
            classification="LOW_RISK_REVERSIBLE",
            risk="LOW",
            reversible=True,
            affected_artifacts=["SPEC.md"],
        )

        stored = store.questions("r")[0]
        assert stored.status is QuestionStatus.PROVISIONAL
        assert stored.provisional_answer.startswith("Treat omission")
        assert "PROVISIONAL" in (tmp_path / "QandA.md").read_text(encoding="utf-8")
        normal = CompleteGate(store, tmp_path).check(run)
        scratch = CompleteGate(store, tmp_path, allow_provisional=True).check(run)
        assert any(item.code is CompleteBlockerCode.UNAPPROVED_PROVISIONAL_DECISION for item in normal.blockers)
        assert not any(item.code is CompleteBlockerCode.UNAPPROVED_PROVISIONAL_DECISION for item in scratch.blockers)


def test_answer_batch_answers_all_questions_before_resume(tmp_path: Path) -> None:
    with Store(tmp_path / "controller.db") as store:
        run = RunState(project_id="p", run_id="r", current_state=State.HUMAN_REQUIRED)
        store.save_run(run)
        qanda = QandaFile(store, tmp_path)
        first = qanda.open_question(run, "first?")
        second = qanda.open_question(run, "second?")
        for question in (first, second):
            question.source_state = State.DESIGN
            question.return_state = State.DESIGN
            store.save_question(question)
        run.return_state = State.DESIGN
        store.save_run(run)
        qanda.escalate_to_human(first)
        qanda.escalate_to_human(second)

        results = answer_batch(
            store,
            TransitionLogger(store),
            "r",
            [
                {"question_id": first.question_id, "answer": "yes"},
                {"question_id": second.question_id, "answer": "no"},
            ],
            workspace=tmp_path,
        )

        assert len(results) == 2
        assert {q.status for q in store.questions("r")} == {QuestionStatus.ANSWERED}
        assert store.transitions("r")[-1].event is Event.HUMAN_ANSWER
