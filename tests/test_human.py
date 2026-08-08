"""人間の回答からの復帰（指示書 2026-08-09-001 §1 / §2 / §3）。

狙いは「人間が AI 間のメッセンジャーをしない」こと。
人間が出てくるのは、どの成果物からも答えが出ない質問だけ。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_controller.cli import main as cli_main
from agent_controller.design import default_design_stages, run_design
from agent_controller.document_stage import (
    DocumentStageConfig,
    ScriptedPhaseHandlers,
    StageResult,
    run_document_stage,
)
from agent_controller.guards import GuardLimits, LoopGuard, check_counters
from agent_controller.human import (
    AnswerRejected,
    answer_question,
    complete_blockers,
    find_question,
)
from agent_controller.models import (
    ArtifactStatus,
    DocumentStage,
    Event,
    Phase,
    QuestionStatus,
    RunState,
    State,
    Worker,
)
from agent_controller.qanda import QANDA_FILENAME, QandaFile
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

CLASS_STAGE = DocumentStageConfig(
    name=DocumentStage.CLASS, inputs=["SEQUENCE.md"], output="CLASS.md"
)

RUN_ID = "run-human"


def stuck_run(db: Path, workspace: Path) -> tuple[Store, TransitionLogger]:
    """CANNOT_ANSWER で人間待ちになっているところまで進めた run を作る。"""
    store = Store(db)
    logger = TransitionLogger(store)
    run = RunState(project_id="greet", run_id=RUN_ID, current_state=State.DESIGN)
    store.save_run(run)

    handlers = ScriptedPhaseHandlers(
        {
            Phase.GENERATE: [Event.DONE],
            Phase.REVIEW_LIGHT: [
                StageResult(
                    event=Event.QUESTION,
                    worker=Worker.CODEX_CLI,
                    question="Are duplicate names allowed?",
                    reason="SPEC.md does not say",
                ),
                Event.PASS,
            ],
            Phase.QANDA: [
                StageResult(
                    event=Event.CANNOT_ANSWER,
                    worker=Worker.CLAUDE_CODE,
                    reason="no artifact defines duplicate-name behaviour",
                )
            ],
            Phase.FIX: [Event.DONE],
        }
    ).as_handlers()

    final = run_document_stage(
        run, CLASS_STAGE, logger, handlers, qanda=QandaFile(store, workspace)
    )
    assert final.current_state == State.HUMAN_REQUIRED
    return store, logger


class TestRejections:
    def test_unknown_question_id(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        with pytest.raises(AnswerRejected, match="does not exist"):
            answer_question(store, logger, RUN_ID, "Q-9999", "no")
        store.close()

    def test_a_question_from_another_run(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        other = RunState(project_id="p", run_id="other-run", current_state=State.DESIGN)
        store.save_run(other)
        headless = QandaFile(store, None)
        headless.open_question(other, "first")
        headless.open_question(other, "second")  # Q-0002 は other-run にしかない

        with pytest.raises(AnswerRejected, match="belongs to run other-run"):
            answer_question(store, logger, RUN_ID, "Q-0002", "no", workspace=tmp_path)
        store.close()

    def test_answering_twice_is_refused(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        answer_question(store, logger, RUN_ID, "Q-0001", "No.", workspace=tmp_path)

        with pytest.raises(AnswerRejected, match="ANSWERED"):
            answer_question(store, logger, RUN_ID, "Q-0001", "No, again", workspace=tmp_path)
        store.close()

    def test_an_open_question_is_not_a_human_one(self, tmp_path: Path) -> None:
        """まだ AI が答える番の質問に人間が割り込まない。"""
        store = Store(tmp_path / "c.db")
        logger = TransitionLogger(store)
        run = RunState(project_id="p", run_id=RUN_ID, current_state=State.DESIGN)
        store.save_run(run)
        QandaFile(store, tmp_path).open_question(run, "still the AI's turn")

        with pytest.raises(AnswerRejected, match="OPEN"):
            answer_question(store, logger, RUN_ID, "Q-0001", "hi", workspace=tmp_path)
        store.close()

    def test_an_empty_answer_is_refused(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        with pytest.raises(AnswerRejected, match="empty"):
            answer_question(store, logger, RUN_ID, "Q-0001", "   ", workspace=tmp_path)
        store.close()

    def test_find_question_needs_a_real_run(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        with pytest.raises(AnswerRejected, match="run nope does not exist"):
            answer_question(store, logger, "nope", "Q-0001", "hi")
        store.close()


class TestAnswer:
    def test_the_question_closes_and_the_run_resumes(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        run, question, transition = answer_question(
            store, logger, RUN_ID, "Q-0001", "Duplicates are forbidden.", workspace=tmp_path
        )

        assert question.status == QuestionStatus.ANSWERED
        assert question.answer == "Duplicates are forbidden."
        assert question.answered_by == Worker.HUMAN

        assert transition.event == Event.HUMAN_ANSWER
        assert run.current_state == State.DESIGN
        assert run.substate == DocumentStage.CLASS
        # CANNOT_ANSWER は「同じ場所へ戻ってくる中断」なので位置が残っている。
        assert run.return_phase == Phase.QANDA
        store.close()

    def test_the_answer_reaches_the_markdown(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        answer_question(
            store, logger, RUN_ID, "Q-0001", "Duplicates are forbidden.", workspace=tmp_path
        )

        content = (tmp_path / QANDA_FILENAME).read_text(encoding="utf-8")
        assert "Duplicates are forbidden." in content
        assert "HUMAN" in content
        assert "- Waiting on a human: 0" in content
        store.close()

    def test_the_log_names_the_question_and_the_answerer(self, tmp_path: Path) -> None:
        """§9: どこで何が起き、なぜどこへ戻ったかが読めること。"""
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        answer_question(store, logger, RUN_ID, "Q-0001", "No.", workspace=tmp_path)

        last = logger.history(RUN_ID)[-1]
        assert last.event == Event.HUMAN_ANSWER
        assert "question=Q-0001" in (last.reason or "")
        assert "answered_by=HUMAN" in (last.reason or "")
        assert last.from_state == State.HUMAN_REQUIRED
        assert last.to_state == State.DESIGN
        store.close()

    def test_it_works_from_a_fresh_process(self, tmp_path: Path) -> None:
        """CLI は別プロセスから呼ばれる。run はメモリではなく DB から読む。"""
        db = tmp_path / "c.db"
        store, _ = stuck_run(db, tmp_path)
        store.close()

        with Store(db) as reopened:
            logger = TransitionLogger(reopened)
            run, question, _ = answer_question(
                reopened, logger, RUN_ID, "Q-0001", "No.", workspace=tmp_path
            )
            assert run.current_state == State.DESIGN
            assert question.status == QuestionStatus.ANSWERED


class TestUpstreamAnswer:
    """人間の回答が上位成果物の変更を要する場合（§7）。"""

    def test_it_does_not_resume_the_stale_phase(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        run, _, transition = answer_question(
            store,
            logger,
            RUN_ID,
            "Q-0001",
            "Forbidden. Add it to SPEC.md.",
            upstream_target=DocumentStage.SPEC,
            workspace=tmp_path,
        )

        assert transition.event == Event.UPSTREAM_CHANGE_REQUIRED
        assert run.pending_upstream_stage == DocumentStage.SPEC
        # 前提が変わるので、止まっていた phase へは戻さない。
        assert run.return_phase is None
        store.close()

    def test_the_next_design_pass_reruns_from_spec(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        run, _, _ = answer_question(
            store,
            logger,
            RUN_ID,
            "Q-0001",
            "Forbidden. Add it to SPEC.md.",
            upstream_target=DocumentStage.SPEC,
            workspace=tmp_path,
        )

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [Event.PASS],
            }
        ).as_handlers()
        final = run_design(run, logger, default_design_stages(), handlers)

        history = logger.history(RUN_ID)
        answered_at = next(
            index for index, item in enumerate(history) if item.event == Event.HUMAN_ANSWER
        )
        entered = [
            (item.to_substate, item.to_phase)
            for item in history[answered_at:]
            if item.event == Event.START
        ]
        # 回答後は SPEC からやり直している。CLASS の QANDA には戻っていない。
        assert entered[0] == (DocumentStage.SPEC, Phase.GENERATE)
        assert (DocumentStage.CLASS, Phase.QANDA) not in entered
        assert final.current_state == State.IMPLEMENT

        # 影響範囲分析の結果がログに残っている（§6）。
        assert any("impact:" in (item.reason or "") for item in history[answered_at:])
        store.close()


class TestCompleteGate:
    def test_open_questions_block_completion(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "c.db")
        run = RunState(project_id="p", run_id=RUN_ID, current_state=State.DOC_SYNC)
        store.save_run(run)
        QandaFile(store, None).open_question(run, "unanswered")

        assert any("OPEN" in item for item in complete_blockers(store, run))
        store.close()

    def test_open_zero_is_not_enough(self, tmp_path: Path) -> None:
        """§3: OPEN=0 でも人間待ちが残っていれば COMPLETE できない。"""
        store, _ = stuck_run(tmp_path / "c.db", tmp_path)
        run = store.load_run(RUN_ID)
        assert run is not None

        assert store.open_questions(RUN_ID) == []
        blockers = complete_blockers(store, run)
        assert blockers
        assert any("waiting on a human" in item for item in blockers)
        store.close()

    def test_answering_clears_the_blocker(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        run, _, _ = answer_question(
            store, logger, RUN_ID, "Q-0001", "No.", workspace=tmp_path
        )
        assert complete_blockers(store, run) == []
        store.close()


class TestLoopGuard:
    """§8: Q&A が入っても歯止めが誤作動しないこと。"""

    def test_waiting_for_a_human_is_not_a_loop(self, tmp_path: Path) -> None:
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        run, _, _ = answer_question(
            store, logger, RUN_ID, "Q-0001", "No.", workspace=tmp_path
        )

        assert run.state_retry == 0
        assert run.repeat == 0
        assert check_counters(run, GuardLimits()) is None
        store.close()

    def test_a_human_answer_is_not_a_worker_switch(self, tmp_path: Path) -> None:
        """人間は指摘を出さないので「Worker を替えても同じ失敗」に数えない。"""
        store, logger = stuck_run(tmp_path / "c.db", tmp_path)
        answer_question(store, logger, RUN_ID, "Q-0001", "No.", workspace=tmp_path)

        workers = {
            worker
            for _, seen in store.fingerprints(RUN_ID).values()
            for worker in seen
        }
        assert Worker.HUMAN.value not in workers
        store.close()


class TestCli:
    def test_answer_command(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "c.db"
        store, _ = stuck_run(db, tmp_path)
        store.close()

        code = cli_main(
            [
                "--db", str(db), "--run", RUN_ID,
                "answer", "Q-0001", "Duplicates are forbidden.",
                "--workspace", str(tmp_path),
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "Q-0001 answered" in out
        assert "DESIGN/CLASS" in out

        with Store(db) as reopened:
            assert reopened.questions(RUN_ID)[0].status == QuestionStatus.ANSWERED

    def test_answer_command_rejects_bad_ids(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "c.db"
        store, _ = stuck_run(db, tmp_path)
        store.close()

        code = cli_main(["--db", str(db), "--run", RUN_ID, "answer", "Q-0404", "x"])
        assert code == 2
        assert "rejected" in capsys.readouterr().err

    def test_status_reports_blockers(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "c.db"
        store, _ = stuck_run(db, tmp_path)
        store.close()

        assert cli_main(["--db", str(db), "--run", RUN_ID, "status"]) == 0
        out = capsys.readouterr().out
        assert "HUMAN_REQUIRED" in out
        assert "waiting on a human" in out

    def test_questions_command_lists_them(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "c.db"
        store, _ = stuck_run(db, tmp_path)
        store.close()

        assert cli_main(["--db", str(db), "--run", RUN_ID, "questions"]) == 0
        out = capsys.readouterr().out
        assert "Q-0001" in out
        assert "Are duplicate names allowed?" in out
