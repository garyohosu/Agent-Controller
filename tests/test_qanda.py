"""QandA.md のテスト（指示書 §9 / §17-13）。

原則は §10 と同じ。**SQLite が正本、Markdown は生成物。**
生成した Markdown を読み戻して解釈することはしない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_controller.document_stage import (
    DocumentStageConfig,
    ScriptedPhaseHandlers,
    StageResult,
    run_document_stage,
    stage_completed,
)
from agent_controller.guards import GuardLimits, LoopGuard, failure_fingerprint
from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    Question,
    QuestionStatus,
    Role,
    RunState,
    State,
    Worker,
)
from agent_controller.qanda import QANDA_FILENAME, QandaFile, render_qanda
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

CLASS_STAGE = DocumentStageConfig(
    name=DocumentStage.CLASS, inputs=["SEQUENCE.md", "SPEC.md"], output="CLASS.md"
)


@pytest.fixture
def design_run(store: Store) -> RunState:
    run = RunState(
        project_id="agent-controller",
        run_id="run-qanda",
        current_state=State.DESIGN,
        substate=DocumentStage.CLASS,
        phase=Phase.REVIEW_LIGHT,
    )
    store.save_run(run)
    return run


@pytest.fixture
def qanda(store: Store, tmp_path: Path) -> QandaFile:
    return QandaFile(store, tmp_path)


class TestRecord:
    def test_ids_are_readable_and_sequential(
        self, qanda: QandaFile, design_run: RunState
    ) -> None:
        """Q-0001 形式。QandA.md にも Worker への指示にも出るので UUID にしない。"""
        first = qanda.open_question(design_run, "why?")
        second = qanda.open_question(design_run, "and why?")
        assert (first.question_id, second.question_id) == ("Q-0001", "Q-0002")

    def test_the_return_position_is_captured(
        self, qanda: QandaFile, design_run: RunState
    ) -> None:
        question = qanda.open_question(design_run, "why?", return_phase=Phase.REVIEW_LIGHT)
        assert question.source_state == State.DESIGN
        assert question.source_stage == DocumentStage.CLASS
        assert question.return_state == State.DESIGN
        assert question.return_phase == Phase.REVIEW_LIGHT

    def test_round_trips_through_sqlite(
        self, store: Store, qanda: QandaFile, design_run: RunState
    ) -> None:
        qanda.open_question(
            design_run,
            "which service owns cancellation?",
            context="SEQUENCE.md and CLASS.md disagree",
            asked_role=Role.REVIEWER,
            asked_worker=Worker.CODEX_CLI,
            related_artifacts=["SEQUENCE.md", "CLASS.md"],
        )

        stored = store.questions(design_run.run_id)[0]
        assert stored.question == "which service owns cancellation?"
        assert stored.context == "SEQUENCE.md and CLASS.md disagree"
        assert stored.asked_worker == Worker.CODEX_CLI
        assert stored.related_artifacts == ["SEQUENCE.md", "CLASS.md"]
        assert stored.status == QuestionStatus.OPEN

    def test_open_questions_are_queryable(
        self, store: Store, qanda: QandaFile, design_run: RunState
    ) -> None:
        """§15 の COMPLETE 条件「QandA OPEN = 0」に使う。"""
        first = qanda.open_question(design_run, "a?")
        qanda.open_question(design_run, "b?")
        assert len(store.open_questions(design_run.run_id)) == 2

        qanda.answer(first, "because X")
        assert [item.question_id for item in store.open_questions(design_run.run_id)] == [
            "Q-0002"
        ]


class TestRendering:
    def test_empty_file_still_says_so(self) -> None:
        rendered = render_qanda([])
        assert "# QandA" in rendered
        assert "No questions" in rendered

    def test_shows_status_counts_and_content(
        self, qanda: QandaFile, design_run: RunState, store: Store
    ) -> None:
        asked = qanda.open_question(
            design_run,
            "which service owns cancellation?",
            asked_role=Role.REVIEWER,
            asked_worker=Worker.CODEX_CLI,
        )
        qanda.open_question(design_run, "still open?")
        qanda.answer(asked, "OrderService, per SPEC.md section 3", Worker.CLAUDE_CODE)

        rendered = render_qanda(store.questions(design_run.run_id))
        assert "Q-0001" in rendered and "Q-0002" in rendered
        assert "ANSWERED" in rendered and "OPEN" in rendered
        assert "- Open: 1" in rendered
        assert "which service owns cancellation?" in rendered
        assert "OrderService, per SPEC.md section 3" in rendered
        assert "DESIGN/CLASS/REVIEW_LIGHT" in rendered

    def test_human_required_says_a_decision_is_needed(
        self, qanda: QandaFile, design_run: RunState, store: Store
    ) -> None:
        question = qanda.open_question(design_run, "retry policy?")
        qanda.escalate_to_human(question, "no document mentions retries")

        rendered = render_qanda(store.questions(design_run.run_id))
        assert "HUMAN_REQUIRED" in rendered
        assert "human decision is required" in rendered
        assert "no document mentions retries" in rendered


class TestFile:
    def test_written_from_sqlite_every_time(
        self, qanda: QandaFile, design_run: RunState, tmp_path: Path
    ) -> None:
        question = qanda.open_question(design_run, "why?")
        target = tmp_path / QANDA_FILENAME
        assert "OPEN" in target.read_text(encoding="utf-8")

        qanda.answer(question, "because X")
        assert "because X" in target.read_text(encoding="utf-8")
        # 追記ではなく毎回まるごと書き直すので、古い状態は残らない。
        assert target.read_text(encoding="utf-8").count("Q-0001") == 1

    def test_edits_to_the_file_are_discarded(
        self, qanda: QandaFile, design_run: RunState, tmp_path: Path
    ) -> None:
        """Markdown を状態にしない。読み戻さないので手編集は次の更新で消える。"""
        question = qanda.open_question(design_run, "why?")
        target = tmp_path / QANDA_FILENAME
        target.write_text("I edited this by hand\n", encoding="utf-8")

        qanda.answer(question, "because X")
        assert "I edited this by hand" not in target.read_text(encoding="utf-8")

    def test_without_a_workspace_nothing_is_written(
        self, store: Store, design_run: RunState, tmp_path: Path
    ) -> None:
        headless = QandaFile(store, None)
        headless.open_question(design_run, "why?")
        assert not (tmp_path / QANDA_FILENAME).exists()
        assert len(store.questions(design_run.run_id)) == 1


class TestLifecycle:
    """Worker → QUESTION → QandA → 復帰 の一周。"""

    def _run(self, store, logger, tmp_path, qanda_script):
        run = RunState(
            project_id="p", run_id="run-life", current_state=State.DESIGN
        )
        store.save_run(run)
        qanda = QandaFile(store, tmp_path)
        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.QUESTION,
                        role=Role.REVIEWER,
                        worker=Worker.CODEX_CLI,
                        question="Should cancellation be idempotent?",
                        reason="SPEC.md does not say",
                    ),
                    Event.PASS,
                ],
                Phase.QANDA: qanda_script,
                Phase.FIX: [Event.DONE],
            }
        ).as_handlers()
        final = run_document_stage(run, CLASS_STAGE, logger, handlers, qanda=qanda)
        return final, qanda

    def test_answered_question_resumes_the_asking_phase(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        final, _ = self._run(
            store,
            logger,
            tmp_path,
            [
                StageResult(
                    event=Event.DONE,
                    role=Role.DIRECTOR,
                    worker=Worker.CLAUDE_CODE,
                    answer="Yes. SPEC.md requires exit 0 on repeat.",
                )
            ],
        )

        assert stage_completed(final, CLASS_STAGE)
        question = store.questions("run-life")[0]
        assert question.status == QuestionStatus.ANSWERED
        assert question.answer == "Yes. SPEC.md requires exit 0 on repeat."
        assert question.answered_by == Worker.CLAUDE_CODE
        assert store.open_questions("run-life") == []

    def test_the_question_text_reaches_the_record(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        self._run(store, logger, tmp_path, [StageResult(event=Event.DONE, answer="yes")])

        question = store.questions("run-life")[0]
        assert question.question == "Should cancellation be idempotent?"
        assert question.context == "SPEC.md does not say"
        assert question.source_phase == Phase.REVIEW_LIGHT
        assert question.related_artifacts == ["SEQUENCE.md", "SPEC.md", "CLASS.md"]

    def test_an_answer_that_needs_a_fix_still_closes_the_question(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        """LOCAL_FIX でも回答はできている。OPEN のまま放置しない。"""
        final, _ = self._run(
            store,
            logger,
            tmp_path,
            [StageResult(event=Event.LOCAL_FIX, answer="No — CLASS.md must be changed")],
        )

        question = store.questions("run-life")[0]
        assert question.status == QuestionStatus.ANSWERED
        assert question.answer == "No — CLASS.md must be changed"
        assert stage_completed(final, CLASS_STAGE)

    def test_unanswerable_question_goes_to_a_human(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        final, _ = self._run(
            store,
            logger,
            tmp_path,
            [
                StageResult(
                    event=Event.CANNOT_ANSWER,
                    reason="no document mentions idempotency",
                )
            ],
        )

        assert final.current_state == State.HUMAN_REQUIRED
        question = store.questions("run-life")[0]
        assert question.status == QuestionStatus.HUMAN_REQUIRED
        assert "no document mentions idempotency" in (question.context or "")
        # 人が読むファイルにも残っている。
        assert "HUMAN_REQUIRED" in (tmp_path / QANDA_FILENAME).read_text(encoding="utf-8")

    def test_the_file_reflects_the_finished_state(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        self._run(
            store,
            logger,
            tmp_path,
            [StageResult(event=Event.DONE, answer="Yes, per SPEC.md")],
        )
        content = (tmp_path / QANDA_FILENAME).read_text(encoding="utf-8")
        assert "- Open: 0" in content
        assert "Yes, per SPEC.md" in content


class TestRepeatedQuestions:
    def test_asking_the_same_thing_again_trips_no_progress(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        """§11「同一 Q&A の再発検出」。専用の検出器は作らず既存の指紋に載せた。"""
        run = RunState(project_id="p", run_id="run-repeat", current_state=State.DESIGN)
        store.save_run(run)

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.QUESTION,
                        worker=Worker.CODEX_CLI,
                        question="Should cancellation be idempotent?",
                        reason=f"asked again ({attempt})",
                    )
                    for attempt in range(5)
                ],
                Phase.QANDA: [StageResult(event=Event.DONE, answer="see SPEC.md")],
            }
        ).as_handlers()

        final = run_document_stage(
            run,
            CLASS_STAGE,
            logger,
            handlers,
            guard=LoopGuard(store, GuardLimits(max_same_fingerprint=2)),
            qanda=QandaFile(store, tmp_path),
        )

        assert final.current_state == State.HUMAN_REQUIRED
        last = logger.history("run-repeat")[-1]
        assert last.event == Event.NO_PROGRESS
        assert "Should cancellation be idempotent?" in (last.reason or "")


class TestHeaderCounts:
    def test_human_required_is_counted_separately_from_open(
        self, qanda: QandaFile, design_run: RunState, store: Store
    ) -> None:
        """OPEN=0 でも人間待ちが残っていることはある。§15 の条件とは別に出す。"""
        answered = qanda.open_question(design_run, "a?")
        stuck = qanda.open_question(design_run, "b?")
        qanda.answer(answered, "yes")
        qanda.escalate_to_human(stuck)

        rendered = render_qanda(store.questions(design_run.run_id))
        assert "- Open: 0" in rendered
        assert "- Waiting on a human: 1" in rendered


class TestQuestionIdentity:
    def test_the_same_question_from_a_different_phase_is_the_same_question(self) -> None:
        """実 AI で取りこぼした形。GENERATE で聞いた質問を REVIEW でまた聞く。"""
        text = "Should a NAME containing a newline be a usage error?"
        asked_while_writing = failure_fingerprint(
            State.DESIGN, DocumentStage.USECASE, Phase.GENERATE,
            Event.QUESTION, "not specified", finding_subject=text,
        )
        asked_while_reviewing = failure_fingerprint(
            State.DESIGN, DocumentStage.USECASE, Phase.REVIEW_LIGHT,
            Event.QUESTION, "still not specified", finding_subject=text,
        )
        assert asked_while_writing == asked_while_reviewing

    def test_other_findings_still_distinguish_the_phase(self) -> None:
        """指摘は phase ごとに別物として扱う。緩めるのは質問だけ。"""
        generating = failure_fingerprint(
            State.DESIGN, DocumentStage.USECASE, Phase.GENERATE,
            Event.LOCAL_FIX, "x", finding_code="MISSING_SECTION", finding_subject="UC-4",
        )
        reviewing = failure_fingerprint(
            State.DESIGN, DocumentStage.USECASE, Phase.REVIEW_LIGHT,
            Event.LOCAL_FIX, "x", finding_code="MISSING_SECTION", finding_subject="UC-4",
        )
        assert generating != reviewing

    def test_a_question_repeated_across_phases_trips_the_guard(
        self, store: Store, logger: TransitionLogger, tmp_path: Path
    ) -> None:
        run = RunState(project_id="p", run_id="run-across", current_state=State.DESIGN)
        store.save_run(run)
        text = "Should a NAME containing a newline be a usage error?"

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [
                    StageResult(event=Event.QUESTION, worker=Worker.CODEX_CLI, question=text),
                    Event.DONE,
                ],
                Phase.QANDA: [StageResult(event=Event.DONE, answer="yes, per SPEC")],
                Phase.REVIEW_LIGHT: [
                    StageResult(event=Event.QUESTION, worker=Worker.CODEX_CLI, question=text),
                ],
            }
        ).as_handlers()

        final = run_document_stage(
            run, CLASS_STAGE, logger, handlers,
            guard=LoopGuard(store, GuardLimits(max_same_fingerprint=1)),
            qanda=QandaFile(store, tmp_path),
        )

        assert final.current_state == State.HUMAN_REQUIRED
        assert logger.history("run-across")[-1].event == Event.NO_PROGRESS
