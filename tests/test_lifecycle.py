from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_controller.cli import main
from agent_controller.lifecycle import RunStartError, start_run
from agent_controller.models import Event, State
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger


def git_workspace(path: Path) -> Path:
    path.mkdir()
    commands = [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Test User"],
    ]
    for command in commands:
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


def test_start_run_persists_input_and_enters_design(tmp_path: Path) -> None:
    workspace = git_workspace(tmp_path / "project")
    db = tmp_path / "controller.db"

    with Store(db) as store:
        run, formal_input = start_run(
            store,
            TransitionLogger(store),
            run_id="TODO001",
            workspace=workspace,
            request="Build a CLI TODO app",
        )
        assert run.current_state == State.DESIGN
        assert store.load_run("TODO001") is not None
        assert store.load_input("TODO001") == formal_input
        assert formal_input.workspace == str(workspace.resolve())
        assert formal_input.request == "Build a CLI TODO app"
        assert store.transitions("TODO001")[0].event == Event.START

    diagnostic = workspace.parent / "agent-controller-diagnostics.jsonl"
    text = diagnostic.read_text(encoding="utf-8")
    assert '"event": "run_start"' in text
    assert "Build a CLI TODO app" not in text


def test_cli_init_is_public_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = git_workspace(tmp_path / "project")
    db = tmp_path / "controller.db"

    assert main([
        "--db", str(db), "--run", "CLI001", "--workspace", str(workspace),
        "init", "--request", "Create a small CLI",
    ]) == 0
    assert "created CLI001" in capsys.readouterr().out


def test_start_run_rejects_dirty_workspace_without_creating_run(tmp_path: Path) -> None:
    workspace = git_workspace(tmp_path / "project")
    (workspace / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    db = tmp_path / "controller.db"

    with Store(db) as store:
        with pytest.raises(RunStartError, match="not clean"):
            start_run(
                store,
                TransitionLogger(store),
                run_id="DIRTY001",
                workspace=workspace,
                request="request",
            )
        assert store.load_run("DIRTY001") is None


def test_start_run_rejects_non_git_and_empty_request(tmp_path: Path) -> None:
    workspace = tmp_path / "not-git"
    workspace.mkdir()
    with Store(tmp_path / "controller.db") as store:
        with pytest.raises(RunStartError, match="not a Git repository"):
            start_run(store, TransitionLogger(store), run_id="NOPE", workspace=workspace, request="request")

    git = git_workspace(tmp_path / "git")
    with Store(tmp_path / "empty.db") as store:
        with pytest.raises(RunStartError, match="must not be empty"):
            start_run(store, TransitionLogger(store), run_id="EMPTY", workspace=git, request="  ")


def test_start_run_rejects_duplicate_id(tmp_path: Path) -> None:
    workspace = git_workspace(tmp_path / "project")
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        start_run(store, logger, run_id="DUP001", workspace=workspace, request="first")
        with pytest.raises(RunStartError, match="already exists"):
            start_run(store, logger, run_id="DUP001", workspace=workspace, request="second")
        assert store.load_input("DUP001").request == "first"
