"""QandA.md（指示書 §9 / §10 / §17-13）。

Agent 間の正式な問い合わせチャネル。レビュー専用ではなく、
Implementer / Reviewer / Designer のいずれもが判断不能な内容をここへ出す。

```text
Worker
  ↓ QUESTION
QandA.md へ追記
  ↓
Controller
  ↓
Director / Answerer
  ├─ 回答できた      → 元の位置へ復帰
  ├─ 上位変更が必要  → 影響範囲分析へ
  └─ 回答できない    → HUMAN_REQUIRED
```

**Markdown を状態にしない。** 制御に必要な情報は SQLite の questions 行が正本で、
QandA.md はそこから毎回まるごと生成する。遷移ログ（§10）と同じ扱い。
生成したファイルを読み戻して解釈することはしない。
"""

from __future__ import annotations

from pathlib import Path

from agent_controller.models import (
    Phase,
    Question,
    QuestionStatus,
    Role,
    RunState,
    Worker,
)
from agent_controller.store import Store

QANDA_FILENAME = "QandA.md"

_STATUS_MARK = {
    QuestionStatus.OPEN: "🔴 OPEN",
    QuestionStatus.ANSWERED: "🟢 ANSWERED",
    QuestionStatus.HUMAN_REQUIRED: "🟡 HUMAN_REQUIRED",
}


def render_question(question: Question) -> str:
    """1 件を Markdown にする。人間と AI の両方が読む。"""
    lines = [
        f"## {question.question_id} — {_STATUS_MARK[question.status]}",
        "",
        f"- **Asked at:** {question.position()}",
    ]
    if question.asked_role is not None:
        lines.append(f"- **Asked by:** {question.asked_role.value}"
                     + (f" ({question.asked_worker.value})" if question.asked_worker else ""))
    if question.related_artifacts:
        lines.append(f"- **Related:** {', '.join(question.related_artifacts)}")

    lines += ["", "### Question", "", question.question]

    if question.context:
        lines += ["", "### Context", "", question.context]

    if question.answer:
        by = f" ({question.answered_by.value})" if question.answered_by else ""
        lines += ["", f"### Answer{by}", "", question.answer]
    elif question.status == QuestionStatus.HUMAN_REQUIRED:
        lines += [
            "",
            "### Answer",
            "",
            "_No existing document settles this. A human decision is required._",
        ]

    return "\n".join(lines)


def render_qanda(questions: list[Question]) -> str:
    """questions 行から QandA.md をまるごと組み立てる純関数。"""
    counts = {status: 0 for status in QuestionStatus}
    for item in questions:
        counts[item.status] += 1

    header = [
        "# QandA",
        "",
        "Questions raised by workers that could not be answered from the documents "
        "they were given.",
        "",
        f"- Total: {len(questions)}",
        f"- Open: {counts[QuestionStatus.OPEN]}",
        # 人が見るファイルなので「誰かの返事待ち」を数えて出す。
        # OPEN=0 でも人間待ちが残っていることはある。
        f"- Waiting on a human: {counts[QuestionStatus.HUMAN_REQUIRED]}",
        "",
    ]
    if not questions:
        return "\n".join([*header, "_No questions have been raised._", ""])

    body = "\n\n".join(render_question(question) for question in questions)
    return "\n".join([*header, body, ""])


class QandaFile:
    """QandA.md の置き場所を知っている層。

    Store にファイル I/O を持ち込まないため、書き出しはここに閉じる。
    """

    def __init__(self, store: Store, workspace: str | Path | None) -> None:
        self.store = store
        self.workspace = Path(workspace) if workspace is not None else None

    @property
    def path(self) -> Path | None:
        return self.workspace / QANDA_FILENAME if self.workspace is not None else None

    def refresh(self, run_id: str) -> None:
        """SQLite の内容で書き直す。差分更新はしない。"""
        target = self.path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_qanda(self.store.questions(run_id)), encoding="utf-8")

    # -- lifecycle -----------------------------------------------------------

    def open_question(
        self,
        run: RunState,
        question: str,
        context: str | None = None,
        asked_role: Role | None = None,
        asked_worker: Worker | None = None,
        related_artifacts: list[str] | None = None,
        return_phase: Phase | None = None,
    ) -> Question:
        """質問を 1 件立てる。戻り先は質問した時点の位置を控えておく。"""
        record = Question(
            question_id=self.store.next_question_id(run.run_id),
            run_id=run.run_id,
            question=question,
            context=context,
            asked_role=asked_role,
            asked_worker=asked_worker,
            related_artifacts=related_artifacts or [],
            source_state=run.current_state,
            source_stage=run.substate,
            source_phase=run.phase,
            return_state=run.current_state,
            return_phase=return_phase if return_phase is not None else run.phase,
        )
        self.store.save_question(record)
        self.refresh(run.run_id)
        return record

    def answer(
        self,
        question: Question,
        answer: str,
        answered_by: Worker | None = None,
    ) -> Question:
        question.status = QuestionStatus.ANSWERED
        question.answer = answer
        question.answered_by = answered_by
        self.store.save_question(question)
        self.refresh(question.run_id)
        return question

    def escalate_to_human(self, question: Question, reason: str | None = None) -> Question:
        """既存成果物から答えられない。推測で埋めずに人間へ渡す（§9）。"""
        question.status = QuestionStatus.HUMAN_REQUIRED
        if reason:
            question.context = (
                f"{question.context}\n\n{reason}" if question.context else reason
            )
        self.store.save_question(question)
        self.refresh(question.run_id)
        return question

    def oldest_open(self, run_id: str) -> Question | None:
        questions = self.store.open_questions(run_id)
        return questions[0] if questions else None
