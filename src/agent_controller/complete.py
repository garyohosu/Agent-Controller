"""Machine-only COMPLETE gate (§17-15)."""

from __future__ import annotations

import subprocess
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from agent_controller.models import ArtifactKind, ArtifactStatus, Event, RunState, State
from agent_controller.models import ContractStatus
from agent_controller.store import Store


class CompleteBlockerCode(StrEnum):
    ARTIFACT_NOT_VALID = "ARTIFACT_NOT_VALID"
    CODE_NOT_LATEST = "CODE_NOT_LATEST"
    TEST_NOT_PASS = "TEST_NOT_PASS"
    REVIEW_NOT_PASS = "REVIEW_NOT_PASS"
    OPEN_QUESTION = "OPEN_QUESTION"
    HUMAN_REQUIRED_QUESTION = "HUMAN_REQUIRED_QUESTION"
    UNAPPROVED_PROVISIONAL_DECISION = "UNAPPROVED_PROVISIONAL_DECISION"
    README_NOT_SYNCED = "README_NOT_SYNCED"
    GIT_DIRTY = "GIT_DIRTY"
    GIT_UNCOMMITTED = "GIT_UNCOMMITTED"
    GIT_NOT_PUSHED = "GIT_NOT_PUSHED"
    ACCEPTANCE_CONTRACT_PENDING = "ACCEPTANCE_CONTRACT_PENDING"
    ACCEPTANCE_CONTRACT_FAILED = "ACCEPTANCE_CONTRACT_FAILED"
    ACCEPTANCE_CONTRACT_STALE = "ACCEPTANCE_CONTRACT_STALE"
    ACCEPTANCE_VERIFIER_UNSUPPORTED = "ACCEPTANCE_VERIFIER_UNSUPPORTED"


class CompleteBlocker(BaseModel):
    code: CompleteBlockerCode
    detail: str
    artifact: ArtifactKind | None = None
    status: ArtifactStatus | None = None


class CompleteCheckResult(BaseModel):
    ready: bool
    blockers: list[CompleteBlocker]


class CompleteGateError(RuntimeError):
    def __init__(self, result: CompleteCheckResult) -> None:
        self.result = result
        super().__init__("COMPLETE blocked: " + ", ".join(item.code.value for item in result.blockers))


def _parse_time(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


class CompleteGate:
    """Evaluate all completion evidence without reading Markdown content."""

    REQUIRED_DESIGN = (
        ArtifactKind.SPEC,
        ArtifactKind.USECASE,
        ArtifactKind.SEQUENCE,
        ArtifactKind.CLASS,
        ArtifactKind.TESTCASE,
    )

    def __init__(self, store: Store, workspace: str | Path = ".", *, allow_provisional: bool = False) -> None:
        self.store = store
        self.workspace = Path(workspace).resolve()
        self.allow_provisional = allow_provisional

    def _git(self, *args: str) -> str:
        process = subprocess.run(
            ["git", *args], cwd=self.workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "git command failed")
        return process.stdout.strip()

    def check(self, run: RunState) -> CompleteCheckResult:
        blockers: list[CompleteBlocker] = []
        artifacts = self.store.artifacts(run.run_id)

        required = list(self.REQUIRED_DESIGN)
        if ArtifactKind.UI in artifacts:
            required.append(ArtifactKind.UI)
        for kind in required:
            artifact = artifacts.get(kind)
            if artifact is None or artifact.status is not ArtifactStatus.VALID:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.ARTIFACT_NOT_VALID,
                    detail=f"{kind.value} is missing or not VALID",
                    artifact=kind,
                    status=artifact.status if artifact else None,
                ))

        code = artifacts.get(ArtifactKind.CODE)
        design_times = [
            _parse_time(artifacts[kind].updated_at)
            for kind in required if kind in artifacts
        ]
        latest_design = max(design_times) if design_times else None
        if code is None or code.status is not ArtifactStatus.VALID or (
            latest_design is not None and _parse_time(code.updated_at) < latest_design
        ):
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.CODE_NOT_LATEST,
                detail="CODE is missing, not VALID, or older than design artifacts",
                artifact=ArtifactKind.CODE,
                status=code.status if code else None,
            ))

        code_time = _parse_time(code.updated_at) if code else None
        history = self.store.transitions(run.run_id)
        latest_test = next((item for item in reversed(history) if item.state is State.TEST), None)
        if latest_test is None or latest_test.event is not Event.PASS or (
            code_time is not None and _parse_time(latest_test.timestamp) < code_time
        ):
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.TEST_NOT_PASS,
                detail="latest TEST transition is not PASS for current CODE",
            ))

        latest_review = next((item for item in reversed(history) if item.state is State.REVIEW), None)
        if latest_review is None or latest_review.event is not Event.PASS or (
            code_time is not None and _parse_time(latest_review.timestamp) < code_time
        ):
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.REVIEW_NOT_PASS,
                detail="latest REVIEW transition is not PASS for current CODE",
            ))

        questions = self.store.questions(run.run_id)
        if any(item.status.value == "OPEN" for item in questions):
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.OPEN_QUESTION,
                detail="questions table contains OPEN questions",
            ))
        if any(item.status.value == "HUMAN_REQUIRED" for item in questions):
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.HUMAN_REQUIRED_QUESTION,
                detail="questions table contains questions waiting on a human",
            ))
        if not self.allow_provisional and any(
            item.status.value == "PROVISIONAL"
            and item.requires_human_confirmation_before_complete
            for item in questions
        ):
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.UNAPPROVED_PROVISIONAL_DECISION,
                detail="questions table contains unapproved provisional decisions",
            ))

        readme = artifacts.get(ArtifactKind.README)
        if readme is None or readme.status is not ArtifactStatus.VALID or readme.reason not in {
            "LATEST", "NOT_REQUIRED"
        }:
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.README_NOT_SYNCED,
                detail="README must be structurally marked LATEST or NOT_REQUIRED",
                artifact=ArtifactKind.README,
                status=readme.status if readme else None,
            ))
        elif code is not None:
            readme_time = _parse_time(readme.updated_at)
            code_time_for_readme = _parse_time(code.updated_at)
            if readme_time is not None and code_time_for_readme is not None and readme_time < code_time_for_readme:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.README_NOT_SYNCED,
                    detail="README is older than the current CODE revision",
                    artifact=ArtifactKind.README,
                    status=readme.status,
                ))

        contracts = self.store.contracts(run.run_id)
        for contract in contracts:
            if not contract.required:
                continue
            if contract.status is ContractStatus.PENDING:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.ACCEPTANCE_CONTRACT_PENDING,
                    detail=f"{contract.contract_id} has not been verified",
                ))
            elif contract.status is ContractStatus.FAIL:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.ACCEPTANCE_CONTRACT_FAILED,
                    detail=f"{contract.contract_id}: {contract.failure_code or 'verification failed'}; {contract.actual or ''}",
                ))
            elif contract.status is ContractStatus.STALE:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.ACCEPTANCE_CONTRACT_STALE,
                    detail=f"{contract.contract_id} is stale",
                ))
            elif contract.status is ContractStatus.UNSUPPORTED:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.ACCEPTANCE_VERIFIER_UNSUPPORTED,
                    detail=f"{contract.contract_id}: verifier {contract.verifier_kind} is unsupported",
                ))

        try:
            dirty = bool(self._git("status", "--porcelain=v1"))
            if dirty:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.GIT_DIRTY,
                    detail="working tree is not clean",
                ))
            upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            head = self._git("rev-parse", "HEAD")
            remote_head = self._git("rev-parse", upstream)
            if head != remote_head:
                blockers.append(CompleteBlocker(
                    code=CompleteBlockerCode.GIT_NOT_PUSHED,
                    detail=f"HEAD {head} differs from {upstream} {remote_head}",
                ))
        except RuntimeError as error:
            blockers.append(CompleteBlocker(
                code=CompleteBlockerCode.GIT_NOT_PUSHED,
                detail=f"cannot verify upstream: {error}",
            ))

        return CompleteCheckResult(ready=not blockers, blockers=blockers)

    def require(self, run: RunState) -> CompleteCheckResult:
        result = self.check(run)
        if not result.ready:
            raise CompleteGateError(result)
        return result
