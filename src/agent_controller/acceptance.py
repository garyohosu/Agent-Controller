"""Deterministic acceptance-contract verifiers."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable

from agent_controller.models import AcceptanceContract, ContractStatus, RunState
from agent_controller.store import Store


def _result(contract: AcceptanceContract, status: ContractStatus, *, evidence: str = "", actual: str = "", failure: str | None = None) -> AcceptanceContract:
    contract.status = status
    contract.last_verified_at = datetime.now().astimezone()
    contract.evidence = evidence
    contract.actual = actual
    contract.failure_code = failure
    return contract


def verify_json_shape(contract: AcceptanceContract, workspace: Path) -> AcceptanceContract:
    path = workspace / contract.target_artifact
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _result(contract, ContractStatus.FAIL, actual="missing", failure="JSON_MISSING")
    except (OSError, json.JSONDecodeError) as error:
        return _result(contract, ContractStatus.FAIL, actual=str(error), failure="JSON_INVALID")
    required = contract.verifier_config.get("required_keys", [])
    missing = [key for key in required if key not in data]
    if missing:
        return _result(contract, ContractStatus.FAIL, actual=f"keys={sorted(data)}", failure="JSON_REQUIRED_KEY_MISSING")
    return _result(contract, ContractStatus.PASS, evidence=f"keys={sorted(data)}", actual=f"keys={sorted(data)}")


def verify_command(contract: AcceptanceContract, workspace: Path) -> AcceptanceContract:
    config = contract.verifier_config
    argv = config.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        return _result(contract, ContractStatus.UNSUPPORTED, failure="COMMAND_CONFIG_INVALID")
    try:
        for relative in config.get("reset_paths", []):
            path = (workspace / str(relative)).resolve()
            if path.parent == workspace and path.name == ".todo.json" and path.exists():
                path.unlink()
        setup = config.get("setup_argv")
        if setup:
            subprocess.run(setup, cwd=workspace, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=float(config.get("timeout", 30)), check=False)
        process = subprocess.run(
            argv, cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=float(config.get("timeout", 30)), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _result(contract, ContractStatus.FAIL, actual=str(error), failure="COMMAND_EXECUTION_FAILED")
    stdout = process.stdout.strip()
    stderr = process.stderr.strip()
    expected_code = config.get("exit_code", 0)
    pattern = config.get("stdout_pattern")
    expected_stderr = config.get("stderr_pattern")
    stdout_ok = True if pattern is None else bool(re.fullmatch(str(pattern).replace("{id}", r"\d+"), stdout))
    stderr_ok = True if expected_stderr is None else bool(re.search(str(expected_stderr), stderr))
    ok = process.returncode == expected_code and stdout_ok and stderr_ok
    evidence = f"exit={process.returncode}; stdout={stdout!r}; stderr={stderr!r}"
    return _result(contract, ContractStatus.PASS if ok else ContractStatus.FAIL,
                   evidence=evidence, actual=evidence,
                   failure=None if ok else "COMMAND_EXPECTATION_MISMATCH")


def verify_contract(contract: AcceptanceContract, workspace: str | Path) -> AcceptanceContract:
    root = Path(workspace).resolve()
    if contract.verifier_kind == "json_shape":
        return verify_json_shape(contract, root)
    if contract.verifier_kind == "command":
        return verify_command(contract, root)
    return _result(contract, ContractStatus.UNSUPPORTED, failure="VERIFIER_UNSUPPORTED")


def verify_contracts(store: Store, run: RunState, workspace: str | Path) -> list[AcceptanceContract]:
    results = []
    contracts = store.contracts(run.run_id)
    # Command contracts can create the fixture used by later shape checks.
    contracts.sort(key=lambda item: (item.verifier_kind != "command", item.contract_id))
    for contract in contracts:
        checked = verify_contract(contract, workspace)
        store.save_contract(checked)
        results.append(checked)
    return results


def ensure_todo_contracts(store: Store, run: RunState, workspace: str | Path | None = None) -> list[AcceptanceContract]:
    """Create the deterministic MVP contracts from a settled TODO Q&A answer."""
    existing = store.contracts(run.run_id)
    if existing:
        return existing
    questions = store.questions(run.run_id)
    answer = " ".join(item.answer or "" for item in questions)
    if workspace is not None:
        root = Path(workspace).resolve()
        document_text = []
        for name in ("QandA.md", "README.md", "instructions/result-2026-08-10-001.md"):
            path = root / name
            if path.is_file():
                document_text.append(path.read_text(encoding="utf-8", errors="replace"))
        answer += " " + " ".join(document_text)
    if "todo_cli" not in answer or ".todo.json" not in answer:
        return []
    expected_items = "items" in answer
    expected_add = "Added:" in answer
    expected_done = "Done:" in answer
    contracts = [
        AcceptanceContract(
            contract_id=f"AC-{len(existing) + 1:04d}", run_id=run.run_id,
            source_type="QANDA", source_id="Q-0001", source_artifact="QandA.md",
            source_revision="QANDA_ANSWER", requirement_kind="json_schema_subset",
            target_artifact=".todo.json", verifier_kind="json_shape",
            verifier_config={"required_keys": ["next_id", "items"] if expected_items else ["next_id"]},
        ),
        AcceptanceContract(
            contract_id="AC-0002", run_id=run.run_id,
            source_type="QANDA", source_id="Q-0001", source_artifact="QandA.md",
            source_revision="QANDA_ANSWER", requirement_kind="cli_stdout",
            target_artifact="todo_cli", verifier_kind="command",
            verifier_config={"argv": ["python", "-m", "todo_cli", "add", "sample"],
                             "exit_code": 0, "stdout_pattern": r"Added: {id} sample" if expected_add else r"Added.*sample"},
        ),
        AcceptanceContract(
            contract_id="AC-0003", run_id=run.run_id,
            source_type="QANDA", source_id="Q-0001", source_artifact="QandA.md",
            source_revision="QANDA_ANSWER", requirement_kind="cli_stdout",
            target_artifact="todo_cli", verifier_kind="command",
            verifier_config={"argv": ["python", "-m", "todo_cli", "done", "1"],
                             "exit_code": 0, "stdout_pattern": r"Done: {id}" if expected_done else r"(Done|Completed).*",
                             "setup_argv": ["python", "-m", "todo_cli", "add", "sample"],
                             "reset_paths": [".todo.json"]},
        ),
    ]
    for contract in contracts:
        store.save_contract(contract)
    return contracts
