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

from agent_controller.models import (
    ArtifactKind,
    ArtifactState,
    RunState,
    Transition,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id                TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    current_state         TEXT NOT NULL,
    substate              TEXT,
    phase                 TEXT,
    previous_state        TEXT,
    previous_substate     TEXT,
    return_state          TEXT,
    return_phase          TEXT,
    pending_upstream_stage TEXT,
    question_source_state TEXT,
    question_source_phase TEXT,
    resume_role           TEXT,
    review_phase          TEXT,
    review_retry_count    INTEGER NOT NULL DEFAULT 0,
    last_event            TEXT,
    active_role           TEXT,
    active_worker         TEXT,
    checkpoint_commit     TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    transition_count      INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL,
    started_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    state             TEXT NOT NULL,
    substate          TEXT,
    phase             TEXT,
    from_state        TEXT NOT NULL,
    from_substate     TEXT,
    event             TEXT NOT NULL,
    to_state          TEXT NOT NULL,
    to_substate       TEXT,
    to_phase          TEXT,
    role              TEXT,
    worker            TEXT,
    reason            TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    checkpoint_commit TEXT,
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE INDEX IF NOT EXISTS idx_transitions_run ON transitions (run_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL,
    path       TEXT,
    reason     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, kind),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);
"""

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
    "review_retry_count",
    "last_event",
    "active_role",
    "active_worker",
    "checkpoint_commit",
    "retry_count",
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
    "retry_count",
    "checkpoint_commit",
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
        self._conn.executescript(SCHEMA)
        self._conn.commit()

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

    def artifacts(self, run_id: str) -> dict[ArtifactKind, ArtifactState]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
        result: dict[ArtifactKind, ArtifactState] = {}
        for row in rows:
            artifact = ArtifactState.model_validate({key: row[key] for key in row.keys()})
            result[artifact.kind] = artifact
        return result
