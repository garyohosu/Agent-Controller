"""DB スキーマのマイグレーション（§17-11A）。

Agent Controller の値は「途中から再開できる」ことにある。実 Worker を繋いだ後の
DB には実行履歴・Q&A・checkpoint・retry・成果物状態という本物の運用データが載る。
そこで schema を変えるたびに DB を作り直していては、その性質そのものが壊れる。

適用済みのバージョンは 1 行ずつ記録する。更新ではなく追記なので、
「いつどれを当てたか」がそのまま履歴になる。

v1 は現在の schema をそのまま基準とする。v1 以前の DB は存在しないため、
過去 5 回分の変更を人工的な履歴として再構成することはしない。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel

VERSION_TABLE = "schema_version"


class Migration(BaseModel):
    """1 つのスキーマ変更。文は 1 つずつ実行する。"""

    version: int
    name: str
    statements: list[str]


_V1_STATEMENTS = [
    """
    CREATE TABLE runs (
        run_id                 TEXT PRIMARY KEY,
        project_id             TEXT NOT NULL,
        current_state          TEXT NOT NULL,
        substate               TEXT,
        phase                  TEXT,
        previous_state         TEXT,
        previous_substate      TEXT,
        return_state           TEXT,
        return_phase           TEXT,
        pending_upstream_stage TEXT,
        question_source_state  TEXT,
        question_source_phase  TEXT,
        resume_role            TEXT,
        review_phase           TEXT,
        review_retry           INTEGER NOT NULL DEFAULT 0,
        last_event             TEXT,
        active_role            TEXT,
        active_worker          TEXT,
        checkpoint_commit      TEXT,
        state_retry            INTEGER NOT NULL DEFAULT 0,
        repeat                 INTEGER NOT NULL DEFAULT 0,
        last_transition_key    TEXT,
        upstream_rework        INTEGER NOT NULL DEFAULT 0,
        transition_count       INTEGER NOT NULL DEFAULT 0,
        status                 TEXT NOT NULL,
        started_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE transitions (
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
        state_retry       INTEGER NOT NULL DEFAULT 0,
        review_retry      INTEGER NOT NULL DEFAULT 0,
        repeat            INTEGER NOT NULL DEFAULT 0,
        checkpoint_commit TEXT,
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
    "CREATE INDEX idx_transitions_run ON transitions (run_id, id)",
    """
    CREATE TABLE artifacts (
        run_id     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        status     TEXT NOT NULL,
        path       TEXT,
        reason     TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (run_id, kind),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
    """
    CREATE TABLE fingerprints (
        run_id      TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        occurrences INTEGER NOT NULL DEFAULT 0,
        workers     TEXT NOT NULL DEFAULT '',
        reason      TEXT,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        PRIMARY KEY (run_id, fingerprint),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
]

_V2_STATEMENTS = [
    """
    CREATE TABLE questions (
        question_id       TEXT NOT NULL,
        run_id            TEXT NOT NULL,
        status            TEXT NOT NULL,
        question          TEXT NOT NULL,
        context           TEXT,
        answer            TEXT,
        answered_by       TEXT,
        related_artifacts TEXT NOT NULL DEFAULT '',
        asked_role        TEXT,
        asked_worker      TEXT,
        source_state      TEXT NOT NULL,
        source_stage      TEXT,
        source_phase      TEXT,
        return_state      TEXT,
        return_phase      TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        PRIMARY KEY (run_id, question_id),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
    "CREATE INDEX idx_questions_open ON questions (run_id, status)",
]

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="baseline", statements=_V1_STATEMENTS),
    Migration(version=2, name="questions", statements=_V2_STATEMENTS),
]

LATEST_VERSION = max(migration.version for migration in MIGRATIONS)


class SchemaError(RuntimeError):
    """DB の schema をこのコードでは扱えない。"""


class UnknownSchemaError(SchemaError):
    """バージョン管理が始まる前に作られた DB。

    どの形なのか判定できないため、推測して ALTER しない。
    """

    def __init__(self, path: str, tables: list[str]) -> None:
        super().__init__(
            f"{path} has tables ({', '.join(sorted(tables))}) but no {VERSION_TABLE}. "
            "It predates schema versioning and cannot be migrated automatically."
        )


class FutureSchemaError(SchemaError):
    """このコードより新しい DB。読み書きすると壊しかねない。"""

    def __init__(self, path: str, found: int, supported: int) -> None:
        super().__init__(
            f"{path} is at schema version {found} but this build only supports "
            f"{supported}. Use a newer Agent Controller."
        )


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [row[0] for row in rows]


def current_version(conn: sqlite3.Connection) -> int:
    """適用済みの最新バージョン。まだ何も当たっていなければ 0。"""
    if VERSION_TABLE not in _table_names(conn):
        return 0
    row = conn.execute(f"SELECT MAX(version) FROM {VERSION_TABLE}").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def applied_versions(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """適用の履歴。人間が「いつどれを当てたか」を確認するため。"""
    if VERSION_TABLE not in _table_names(conn):
        return []
    rows = conn.execute(
        f"SELECT version, applied_at FROM {VERSION_TABLE} ORDER BY version"
    ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def migrate(
    conn: sqlite3.Connection,
    path: str = ":memory:",
    migrations: list[Migration] | None = None,
) -> int:
    """未適用のマイグレーションを順に当て、適用後のバージョンを返す。

    1 つのマイグレーションとそのバージョン行は同じトランザクションで書く。
    途中で失敗したら両方とも巻き戻る（中途半端に適用された状態を残さない）。
    """
    migrations = migrations if migrations is not None else MIGRATIONS
    latest = max(migration.version for migration in migrations)

    tables = _table_names(conn)
    if VERSION_TABLE not in tables:
        if tables:
            raise UnknownSchemaError(path, tables)
        conn.execute(
            f"CREATE TABLE {VERSION_TABLE} ("
            " version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL)"
        )
        conn.commit()

    version = current_version(conn)
    if version > latest:
        raise FutureSchemaError(path, version, latest)

    for migration in sorted(migrations, key=lambda item: item.version):
        if migration.version <= version:
            continue
        # BEGIN / COMMIT を明示する。python の sqlite3 は DML でしか暗黙の
        # トランザクションを張らないため、`with conn:` では DDL が巻き戻らない。
        # SQLite 自体は DDL もトランザクションに含められる。
        conn.execute("BEGIN")
        try:
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {VERSION_TABLE} (version, applied_at) VALUES (?, ?)",
                (migration.version, datetime.now(timezone.utc).isoformat()),
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        version = migration.version

    return version
