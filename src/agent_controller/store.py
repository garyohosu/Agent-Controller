"""SQLite 永続化。

指示書 §10「SQLite の Event History を正本とする」および §21「SQLite に保持する情報」
に対応する。人間向けテキストログはここに入った行から生成する（transition_log.py）。

Worker が session limit で落ちても、PC を変えても、この DB と Git checkpoint から
run を再開できることを目的とする。
"""

from __future__ import annotations

import json
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
    AcceptanceContract,
    ContractStatus,
    Question,
    QuestionStatus,
    RecoveryAttempt,
    RunInput,
    RunState,
    Transition,
    utcnow,
)


_RUN_COLUMNS = (
    "run_id",
    "project_id",
    "task_type",
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
    "classification",
    "provisional_answer",
    "risk",
    "reversible",
    "blocking_scope",
    "recommended_human_action",
    "affected_artifacts",
    "requires_human_confirmation_before_complete",
    "policy_rule",
    "policy_scope",
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

    def save_input(self, run_input: RunInput) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO run_inputs (run_id, workspace, request, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_input.run_id, run_input.workspace, run_input.request,
                 run_input.created_at.isoformat()),
            )

    def load_input(self, run_id: str) -> RunInput | None:
        row = self._conn.execute(
            "SELECT * FROM run_inputs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return RunInput.model_validate({key: row[key] for key in row.keys()})

    # -- acceptance contracts -----------------------------------------------

    def save_contract(self, contract: AcceptanceContract) -> None:
        contract.updated_at = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO acceptance_contracts
                (contract_id, run_id, source_type, source_id, source_artifact,
                 source_revision, requirement_kind, target_artifact, target_scope,
                 verifier_kind, verifier_config, required, status, last_verified_at,
                 evidence, actual, failure_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract.contract_id, contract.run_id, contract.source_type,
                    contract.source_id, contract.source_artifact, contract.source_revision,
                    contract.requirement_kind, contract.target_artifact, contract.target_scope,
                    contract.verifier_kind, json.dumps(contract.verifier_config, ensure_ascii=False),
                    int(contract.required), contract.status.value,
                    contract.last_verified_at.isoformat() if contract.last_verified_at else None,
                    contract.evidence, contract.actual, contract.failure_code,
                    contract.created_at.isoformat(), contract.updated_at.isoformat(),
                ),
            )

    def contracts(self, run_id: str) -> list[AcceptanceContract]:
        rows = self._conn.execute(
            "SELECT * FROM acceptance_contracts WHERE run_id = ? ORDER BY contract_id",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = {key: row[key] for key in row.keys()}
            data["verifier_config"] = json.loads(data["verifier_config"] or "{}")
            data["required"] = bool(data["required"])
            result.append(AcceptanceContract.model_validate(data))
        return result

    def contract(self, contract_id: str) -> AcceptanceContract | None:
        rows = self._conn.execute(
            "SELECT * FROM acceptance_contracts WHERE contract_id = ?", (contract_id,)
        ).fetchall()
        if not rows:
            return None
        data = {key: rows[0][key] for key in rows[0].keys()}
        data["verifier_config"] = json.loads(data["verifier_config"] or "{}")
        data["required"] = bool(data["required"])
        return AcceptanceContract.model_validate(data)

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
                    ",".join(getattr(question, name))
                    if name in {"related_artifacts", "affected_artifacts"}
                    else _encode(getattr(question, name))
                    for name in _QUESTION_COLUMNS
                ),
            )

    def _question_from(self, row: sqlite3.Row) -> Question:
        data = {key: row[key] for key in row.keys()}
        data["related_artifacts"] = [
            item for item in str(data["related_artifacts"] or "").split(",") if item
        ]
        data["affected_artifacts"] = [
            item for item in str(data["affected_artifacts"] or "").split(",") if item
        ]
        if data.get("reversible") is not None:
            data["reversible"] = bool(data["reversible"])
        data["requires_human_confirmation_before_complete"] = bool(
            data.get("requires_human_confirmation_before_complete")
        )
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

    # -- recovery log (指示書 018 §4.3) --------------------------------------

    def save_recovery_attempt(self, attempt: RecoveryAttempt) -> int:
        """Auto-Recovery が試した 1 回を追記する。Decision Log と同じく正本は SQLite。"""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO recovery_attempts
                    (run_id, timestamp, error_code, failed_worker, failed_role,
                     capability_mismatch, fallback_worker, attempt_number,
                     final_outcome, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.run_id, attempt.timestamp.isoformat(), attempt.error_code,
                    _encode(attempt.failed_worker), _encode(attempt.failed_role),
                    int(attempt.capability_mismatch), _encode(attempt.fallback_worker),
                    attempt.attempt_number, attempt.final_outcome, attempt.reason,
                ),
            )
        return int(cursor.lastrowid)

    def recovery_attempts(self, run_id: str) -> list[RecoveryAttempt]:
        rows = self._conn.execute(
            "SELECT * FROM recovery_attempts WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        result = []
        for row in rows:
            data = {key: row[key] for key in row.keys() if key != "id"}
            data["capability_mismatch"] = bool(data["capability_mismatch"])
            result.append(RecoveryAttempt.model_validate(data))
        return result

    # -- artifacts -------------------------------------------------------------

    def artifacts(self, run_id: str) -> dict[ArtifactKind, ArtifactState]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
        result: dict[ArtifactKind, ArtifactState] = {}
        for row in rows:
            artifact = ArtifactState.model_validate({key: row[key] for key in row.keys()})
            result[artifact.kind] = artifact
        return result
