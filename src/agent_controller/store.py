"""SQLite 永続化。

指示書 §10「SQLite の Event History を正本とする」および §21「SQLite に保持する情報」
に対応する。人間向けテキストログはここに入った行から生成する（transition_log.py）。

Worker が session limit で落ちても、PC を変えても、この DB と Git checkpoint から
run を再開できることを目的とする。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_controller.migrations import migrate
from agent_controller.models import (
    ArtifactKind,
    ArtifactState,
    Question,
    QuestionStatus,
    RunState,
    Transition,
    utcnow,
)


_RUN_COLUMNS = (
    "run_id",
    "project_id",
    "current_state",
    "substate",
    "phase",
    "previous_state",
    "previous_substate",
    "return_state",
    "return_phase",
    "pending_upstream_stage",
    "question_source_state",
    "question_source_phase",
    "resume_role",
    "review_phase",
    "review_retry",
    "last_event",
    "active_role",
    "active_worker",
    "checkpoint_commit",
    "state_retry",
    "repeat",
    "last_transition_key",
    "upstream_rework",
    "transition_count",
    "status",
    "started_at",
    "updated_at",
)

_TRANSITION_COLUMNS = (
    "timestamp",
    "run_id",
    "state",
    "substate",
    "phase",
    "from_state",
    "from_substate",
    "event",
    "to_state",
    "to_substate",
    "to_phase",
    "role",
    "worker",
    "reason",
    "state_retry",
    "review_retry",
    "repeat",
    "checkpoint_commit",
)


_QUESTION_COLUMNS = (
    "question_id",
    "run_id",
    "status",
    "question",
    "context",
    "answer",
    "answered_by",
    "related_artifacts",
    "asked_role",
    "asked_worker",
    "source_state",
    "source_stage",
    "source_phase",
    "return_state",
    "return_phase",
    "created_at",
    "updated_at",
)


def _encode(value: Any) -> Any:
    """pydantic の値を SQLite が扱える型へ落とす。"""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_values(model: Any, columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(_encode(getattr(model, name)) for name in columns)


class Store:
    """run / transition / artifact の永続化。

    1 接続を保持する薄いラッパー。接続の寿命は呼び出し側が持つ。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.schema_version = migrate(self._conn, self.path)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # -- runs ----------------------------------------------------------------

    def save_run(self, run: RunState) -> None:
        """run を作成または更新する（run_id が主キー）。"""
        run.updated_at = utcnow()
        placeholders = ", ".join("?" for _ in _RUN_COLUMNS)
        columns = ", ".join(_RUN_COLUMNS)
        with self.transaction() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO runs ({columns}) VALUES ({placeholders})",
                _row_values(run, _RUN_COLUMNS),
            )

    def load_run(self, run_id: str) -> RunState | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return RunState.model_validate({key: row[key] for key in row.keys()})

    def list_runs(self) -> list[RunState]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        return [
            RunState.model_validate({key: row[key] for key in row.keys()}) for row in rows
        ]

    # -- transitions ---------------------------------------------------------

    def append_transition(self, transition: Transition) -> int:
        """状態遷移を 1 行追記する。ここが遷移ログの正本。"""
        placeholders = ", ".join("?" for _ in _TRANSITION_COLUMNS)
        columns = ", ".join(_TRANSITION_COLUMNS)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"INSERT INTO transitions ({columns}) VALUES ({placeholders})",
                _row_values(transition, _TRANSITION_COLUMNS),
            )
        return int(cursor.lastrowid)

    def transitions(self, run_id: str) -> list[Transition]:
        """記録順の遷移一覧。"""
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            Transition.model_validate(
                {key: row[key] for key in row.keys() if key != "id"}
            )
            for row in rows
        ]

    # -- artifacts -----------------------------------------------------------

    def save_artifact(self, artifact: ArtifactState) -> None:
        artifact.updated_at = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO artifacts
                    (run_id, kind, status, path, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.run_id,
                    artifact.kind.value,
                    artifact.status.value,
                    artifact.path,
                    artifact.reason,
                    artifact.updated_at.isoformat(),
                ),
            )

    # -- questions -----------------------------------------------------------

    def next_question_id(self, run_id: str) -> str:
        """Q-0001 形式の連番。QandA.md と Worker への指示に出るので読める形にする。"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM questions WHERE run_id = ?", (run_id,)
        ).fetchone()
        return f"Q-{int(row[0]) + 1:04d}"

    def save_question(self, question: Question) -> None:
        question.updated_at = utcnow()
        with self.transaction() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO questions ({', '.join(_QUESTION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _QUESTION_COLUMNS)})",
                tuple(
                    ",".join(question.related_artifacts)
                    if name == "related_artifacts"
                    else _encode(getattr(question, name))
                    for name in _QUESTION_COLUMNS
                ),
            )

    def _question_from(self, row: sqlite3.Row) -> Question:
        data = {key: row[key] for key in row.keys()}
        data["related_artifacts"] = [
            item for item in str(data["related_artifacts"] or "").split(",") if item
        ]
        return Question.model_validate(data)

    def questions(self, run_id: str) -> list[Question]:
        rows = self._conn.execute(
            "SELECT * FROM questions WHERE run_id = ? ORDER BY question_id", (run_id,)
        ).fetchall()
        return [self._question_from(row) for row in rows]

    def open_questions(self, run_id: str) -> list[Question]:
        """未解決の質問。§15 の COMPLETE 条件「QandA OPEN = 0」に使う。"""
        rows = self._conn.execute(
            "SELECT * FROM questions WHERE run_id = ? AND status = ? ORDER BY question_id",
            (run_id, QuestionStatus.OPEN.value),
        ).fetchall()
        return [self._question_from(row) for row in rows]

    # -- fingerprints --------------------------------------------------------

    def observe_fingerprint(
        self,
        run_id: str,
        fingerprint: str,
        worker: str | None = None,
        reason: str | None = None,
    ) -> tuple[int, set[str]]:
        """同じ失敗の指紋を 1 件数え、(累計回数, これまでの Worker) を返す。

        Worker を替えても同じ失敗が出るかを判定できるよう、指紋ごとに
        担当した Worker を覚えておく（指示書 §11）。
        """
        now = utcnow().isoformat()
        row = self._conn.execute(
            "SELECT occurrences, workers FROM fingerprints WHERE run_id = ? AND fingerprint = ?",
            (run_id, fingerprint),
        ).fetchone()

        workers = set(filter(None, row["workers"].split(","))) if row is not None else set()
        if worker is not None:
            workers.add(worker)
        occurrences = (row["occurrences"] if row is not None else 0) + 1
        joined = ",".join(sorted(workers))

        with self.transaction() as conn:
            if row is None:
                conn.execute(
                    """
                    INSERT INTO fingerprints
                        (run_id, fingerprint, occurrences, workers, reason, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, fingerprint, occurrences, joined, reason, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE fingerprints
                       SET occurrences = ?, workers = ?, reason = ?, last_seen = ?
                     WHERE run_id = ? AND fingerprint = ?
                    """,
                    (occurrences, joined, reason, now, run_id, fingerprint),
                )

        return occurrences, workers

    def fingerprints(self, run_id: str) -> dict[str, tuple[int, set[str]]]:
        rows = self._conn.execute(
            "SELECT fingerprint, occurrences, workers FROM fingerprints WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        return {
            row["fingerprint"]: (
                row["occurrences"],
                set(filter(None, row["workers"].split(","))),
            )
            for row in rows
        }

    def artifacts(self, run_id: str) -> dict[ArtifactKind, ArtifactState]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
        result: dict[ArtifactKind, ArtifactState] = {}
        for row in rows:
            artifact = ArtifactState.model_validate({key: row[key] for key in row.keys()})
            result[artifact.kind] = artifact
        return result
