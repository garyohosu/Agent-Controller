"""DB マイグレーションのテスト（§17-11A）。

実 Worker を繋ぐと DB は本物の運用データになる。schema を変えるたびに
作り直していては「途中から再開できる」という性質が壊れるので、そこを守る。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_controller.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    VERSION_TABLE,
    FutureSchemaError,
    Migration,
    UnknownSchemaError,
    applied_versions,
    current_version,
    migrate,
)
from agent_controller.models import RunState, State
from agent_controller.store import Store

# 本物の履歴は汚さず、runner だけを試すための架空の v2。
SYNTHETIC_NEXT = Migration(
    version=99,
    name="synthetic-test-only",
    statements=["ALTER TABLE runs ADD COLUMN operator TEXT"],
)


class TestFreshDatabase:
    def test_starts_at_the_latest_version(self, tmp_path: Path) -> None:
        with Store(tmp_path / "controller.db") as store:
            assert store.schema_version == LATEST_VERSION

    def test_in_memory_is_migrated_too(self) -> None:
        with Store(":memory:") as store:
            assert store.schema_version == LATEST_VERSION

    def test_history_records_every_applied_migration(self, tmp_path: Path) -> None:
        db = tmp_path / "controller.db"
        with Store(db):
            pass

        conn = sqlite3.connect(db)
        history = applied_versions(conn)
        conn.close()

        assert [version for version, _ in history] == [
            migration.version for migration in MIGRATIONS
        ]
        assert all(applied_at for _, applied_at in history)

    def test_reopening_does_not_reapply(self, tmp_path: Path) -> None:
        db = tmp_path / "controller.db"
        with Store(db) as store:
            store.save_run(RunState(project_id="p", run_id="r", current_state=State.DESIGN))

        with Store(db) as reopened:
            assert reopened.schema_version == LATEST_VERSION
            loaded = reopened.load_run("r")
            assert loaded is not None
            assert loaded.current_state == State.DESIGN


class TestRunner:
    def test_applies_a_new_version_and_keeps_the_data(self, tmp_path: Path) -> None:
        """v1 で書いたデータが v2 適用後も残ること。"""
        db = tmp_path / "controller.db"
        with Store(db) as store:
            store.save_run(RunState(project_id="p", run_id="kept", current_state=State.TEST))

        conn = sqlite3.connect(db)
        version = migrate(conn, str(db), [*MIGRATIONS, SYNTHETIC_NEXT])
        assert version == SYNTHETIC_NEXT.version
        assert current_version(conn) == SYNTHETIC_NEXT.version

        row = conn.execute(
            "SELECT current_state, operator FROM runs WHERE run_id = ?", ("kept",)
        ).fetchone()
        conn.close()

        assert row[0] == State.TEST.value
        assert row[1] is None

    def test_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "controller.db"
        with Store(db):
            pass

        conn = sqlite3.connect(db)
        migrate(conn, str(db), [*MIGRATIONS, SYNTHETIC_NEXT])
        migrate(conn, str(db), [*MIGRATIONS, SYNTHETIC_NEXT])
        history = applied_versions(conn)
        conn.close()

        assert [version for version, _ in history] == [
            *(m.version for m in MIGRATIONS),
            SYNTHETIC_NEXT.version,
        ]

    def test_a_failing_migration_leaves_no_version_row(self, tmp_path: Path) -> None:
        """中途半端に適用された状態を残さない。"""
        db = tmp_path / "controller.db"
        with Store(db):
            pass

        broken = Migration(
            version=99,
            name="broken",
            statements=[
                "ALTER TABLE runs ADD COLUMN half_applied TEXT",
                "THIS IS NOT SQL",
            ],
        )

        conn = sqlite3.connect(db)
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, str(db), [*MIGRATIONS, broken])

        assert current_version(conn) == LATEST_VERSION
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        conn.close()

        assert "half_applied" not in columns


class TestRefusals:
    def test_a_database_from_before_versioning_is_refused(self, tmp_path: Path) -> None:
        """どの形か判定できない DB を、推測して ALTER しない。"""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
        conn.commit()

        with pytest.raises(UnknownSchemaError) as error:
            migrate(conn, str(db))
        conn.close()

        assert VERSION_TABLE in str(error.value)
        assert "runs" in str(error.value)

    def test_a_newer_database_is_refused(self, tmp_path: Path) -> None:
        """古いコードで新しい DB を開いて壊さない。"""
        db = tmp_path / "controller.db"
        conn = sqlite3.connect(db)
        migrate(conn, str(db), [*MIGRATIONS, SYNTHETIC_NEXT])

        with pytest.raises(FutureSchemaError) as error:
            migrate(conn, str(db), MIGRATIONS)
        conn.close()

        assert f"version {SYNTHETIC_NEXT.version}" in str(error.value)

    def test_store_surfaces_the_refusal(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE artifacts (run_id TEXT)")
        conn.commit()
        conn.close()

        with pytest.raises(UnknownSchemaError):
            Store(db)


class TestBaseline:
    def test_versions_are_unique_and_ordered(self) -> None:
        versions = [migration.version for migration in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(versions) == len(set(versions))

    def test_baseline_creates_every_table_the_store_uses(self, tmp_path: Path) -> None:
        db = tmp_path / "controller.db"
        with Store(db):
            pass

        conn = sqlite3.connect(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        conn.close()

        assert {"runs", "transitions", "artifacts", "fingerprints", VERSION_TABLE} <= tables
