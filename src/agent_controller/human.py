"""人間の回答から run を再開する（指示書 2026-08-09-001 §1 / §2）。

AI 同士で解決できなかった質問だけが人間まで上がってくる。その回答を受け取り、
質問を閉じ、止まっていた run を元の位置から続ける。

人間も Worker と同じ扱いにする。自由文の解釈を Controller にさせないため、
「上位仕様の変更が必要」という判断は文章からではなく `upstream_target` の指定で受け取る。

```text
HUMAN_REQUIRED
  ↓ answer("Q-0003", "禁止する")
questions 行 ANSWERED
  ↓ HUMAN_ANSWER
元の State / Stage / Phase へ復帰

HUMAN_REQUIRED
  ↓ answer("Q-0003", "禁止する。SPEC に追加", upstream=SPEC)
questions 行 ANSWERED
  ↓ UPSTREAM_CHANGE_REQUIRED
影響範囲分析へ
```
"""

from __future__ import annotations

from pathlib import Path

from agent_controller.models import (
    DocumentStage,
    Event,
    Question,
    QuestionStatus,
    RunState,
    Transition,
    Worker,
)
from agent_controller.qanda import QandaFile
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger


class AnswerRejected(ValueError):
    """人間の回答を受け付けられない。"""


def find_question(store: Store, run_id: str, question_id: str) -> Question:
    """質問を取り出す。別 run の ID や存在しない ID は受け付けない。"""
    for question in store.questions(run_id):
        if question.question_id == question_id:
            return question

    for run in store.list_runs():
        if run.run_id == run_id:
            continue
        if any(item.question_id == question_id for item in store.questions(run.run_id)):
            raise AnswerRejected(
                f"{question_id} belongs to run {run.run_id}, not {run_id}"
            )

    raise AnswerRejected(f"{question_id} does not exist in run {run_id}")


def answer_question(
    store: Store,
    logger: TransitionLogger,
    run_id: str,
    question_id: str,
    answer: str,
    upstream_target: DocumentStage | None = None,
    workspace: str | Path | None = None,
) -> tuple[RunState, Question, Transition]:
    """人間の回答を書き戻し、run を再開する。

    別プロセスから呼ばれる前提なので、run は必ず DB から読み直す。
    """
    if not answer.strip():
        raise AnswerRejected("the answer is empty")

    run = store.load_run(run_id)
    if run is None:
        raise AnswerRejected(f"run {run_id} does not exist")

    question = find_question(store, run_id, question_id)
    if question.status != QuestionStatus.HUMAN_REQUIRED:
        raise AnswerRejected(
            f"{question_id} is {question.status.value}; only HUMAN_REQUIRED "
            "questions are waiting for a person"
        )

    qanda = QandaFile(store, workspace)
    qanda.answer(question, answer.strip(), answered_by=Worker.HUMAN)

    # 復帰位置は questions 行を正とする。run が持っているのは「stage を抜けた場所」
    # （= QANDA）だが、そこへ戻しても答え終わった質問をもう一度探すだけになる。
    # 戻るべきなのは質問した工程。実 AI で 1 周させて分かった。
    notes = [f"question={question.question_id}", "answered_by=HUMAN"]
    if question.return_phase is not None:
        if run.return_phase != question.return_phase:
            notes.append(
                f"resume {question.return_phase.value} (asked there; "
                f"run was suspended in {run.return_phase.value if run.return_phase else '-'})"
            )
        run.return_phase = question.return_phase

    if upstream_target is not None:
        # 上位成果物が変わるなら、止まっていた phase へは戻さない。
        # その文書はやり直しになるので、途中から再開すると古い前提のまま進む。
        run.pending_upstream_stage = upstream_target
        run.return_phase = None
        notes.append(f"upstream={upstream_target.value}")
        transition = logger.record(
            run,
            Event.HUMAN_ANSWER,
            to_substate=question.source_stage,
            reason=" | ".join([*notes, "upstream change required"]),
        )
        transition = logger.record(
            run,
            Event.UPSTREAM_CHANGE_REQUIRED,
            to_substate=question.source_stage,
            reason=" | ".join(notes),
        )
    else:
        transition = logger.record(
            run,
            Event.HUMAN_ANSWER,
            to_substate=question.source_stage,
            reason=" | ".join(notes),
        )

    return run, question, transition


def complete_blockers(store: Store, run: RunState) -> list[str]:
    """COMPLETE を許さない理由の一覧（指示書 2026-08-08-001 §15）。

    Q&A の条件だけ実装してある。OPEN = 0 だけでは足りない。
    人間待ちの質問が残ったまま完了できてしまうため。
    """
    blockers: list[str] = []

    counts = {status: 0 for status in QuestionStatus}
    for question in store.questions(run.run_id):
        counts[question.status] += 1

    if counts[QuestionStatus.OPEN]:
        blockers.append(f"{counts[QuestionStatus.OPEN]} question(s) still OPEN")
    if counts[QuestionStatus.HUMAN_REQUIRED]:
        blockers.append(
            f"{counts[QuestionStatus.HUMAN_REQUIRED]} question(s) waiting on a human"
        )

    return blockers


COMPLETE_TODO: tuple[str, ...] = (
    "design artifacts all VALID",
    "CODE latest",
    "README latest",
    "TEST PASS",
    "REVIEW PASS",
    "working tree clean",
    "committed",
    "pushed",
)
"""§15 の残りの完了条件。まだ検査していない。§17-14 / §17-15 の担当。"""
