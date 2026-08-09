"""Controller-owned Git checkpoints for safe Worker execution.

The Worker may edit files, but it never decides what to stage, commit, or reset.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_controller.models import Event, RunState
from agent_controller.store import Store


class GitCheckpointError(RuntimeError):
    """A repository cannot be used safely for a Worker operation."""


class DirtyWorkingTreeError(GitCheckpointError):
    """Tracked user changes were present before the Worker started."""


@dataclass(frozen=True)
class GitObservation:
    head: str
    changed_files: tuple[str, ...]


def _run(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode:
        raise GitCheckpointError(process.stderr.strip() or f"git {' '.join(args)} failed")
    return process.stdout


class GitCheckpointManager:
    """Take, validate, commit, and roll back Controller-owned Worker changes."""

    def __init__(self, workspace: str | Path, store: Store | None = None) -> None:
        self.repo = Path(workspace).resolve()
        self.store = store
        self._baseline_untracked: set[str] = set()

    def head(self) -> str:
        return _run(self.repo, "rev-parse", "HEAD").strip()

    def status(self) -> tuple[str, ...]:
        raw = _run(self.repo, "status", "--porcelain=v1", "-z")
        entries = raw.split("\0")
        return tuple(item for item in entries if item and len(item) >= 3)

    def begin(self, run: RunState) -> str:
        """Record the pre-Worker HEAD, refusing pre-existing tracked changes."""
        statuses = self.status()
        tracked = [
            item for item in statuses if not item.startswith("?? ")
        ]
        if tracked:
            raise DirtyWorkingTreeError(
                "tracked working-tree changes exist before Worker start: "
                + ", ".join(tracked)
            )
        self._baseline_untracked = {item[3:] for item in statuses if item.startswith("?? ")}
        checkpoint = self.head()
        run.checkpoint_commit = checkpoint
        if self.store is not None:
            self.store.save_run(run)
        return checkpoint

    def observe(self) -> GitObservation:
        current = self.status()
        changed = tuple(
            item for item in current
            if item[3:] not in self._baseline_untracked
        )
        return GitObservation(self.head(), changed)

    def verified_files_changed(self, claimed: list[str] | None = None) -> list[str]:
        """Return Git's paths, with Worker claims retained only as diagnostics."""
        del claimed  # claims are intentionally never used as the source of truth
        return [item[3:] for item in self.observe().changed_files]

    def commit_success(self, event: Event, message: str | None = None) -> GitObservation:
        observation = self.observe()
        if observation.changed_files:
            paths = [item[3:] for item in observation.changed_files]
            _run(self.repo, "add", "--", *paths)
            _run(self.repo, "commit", "-m", message or f"controller: Worker {event.value}")
            observation = self.observe()
        return observation

    def rollback(self, checkpoint: str) -> GitObservation:
        """Reset tracked state to checkpoint; never run git clean."""
        _run(self.repo, "reset", "--hard", checkpoint)
        return self.observe()

    def push(self) -> None:
        """Publish Controller commits so the COMPLETE gate can verify HEAD/upstream."""
        _run(self.repo, "push")
