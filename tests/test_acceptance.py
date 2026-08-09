from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_controller.acceptance import verify_contract, verify_contracts
from agent_controller.complete import CompleteBlockerCode, CompleteGate
from agent_controller.models import AcceptanceContract, ContractStatus, RunState, State
from agent_controller.store import Store


def test_json_shape_contract_records_failure_and_pass(tmp_path: Path) -> None:
    (tmp_path / ".todo.json").write_text(json.dumps({"next_id": 1, "todos": []}), encoding="utf-8")
    contract = AcceptanceContract(
        contract_id="AC-0001", run_id="r", source_type="QANDA",
        requirement_kind="json_schema_subset", target_artifact=".todo.json",
        verifier_kind="json_shape", verifier_config={"required_keys": ["next_id", "items"]},
    )
    failed = verify_contract(contract, tmp_path)
    assert failed.status is ContractStatus.FAIL
    assert failed.failure_code == "JSON_REQUIRED_KEY_MISSING"
    (tmp_path / ".todo.json").write_text(json.dumps({"next_id": 1, "items": []}), encoding="utf-8")
    passed = verify_contract(failed, tmp_path)
    assert passed.status is ContractStatus.PASS


def test_command_contract_checks_stdout(tmp_path: Path) -> None:
    contract = AcceptanceContract(
        contract_id="AC-0002", run_id="r", source_type="QANDA",
        requirement_kind="cli_stdout", target_artifact="todo_cli", verifier_kind="command",
        verifier_config={"argv": [sys.executable, "-c", "print('Added: 1 sample')"],
                         "exit_code": 0, "stdout_pattern": r"Added: {id} sample"},
    )
    assert verify_contract(contract, tmp_path).status is ContractStatus.PASS


def test_contracts_persist_and_complete_gate_blocks_failure(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    with Store(tmp_path / "controller.db") as store:
        run = RunState(project_id="p", run_id="r", current_state=State.DOC_SYNC)
        store.save_run(run)
        contract = AcceptanceContract(
            contract_id="AC-0001", run_id="r", source_type="QANDA",
            requirement_kind="json_schema_subset", target_artifact="missing.json",
            verifier_kind="json_shape", verifier_config={"required_keys": ["items"]},
        )
        store.save_contract(contract)
        checked = verify_contracts(store, run, root)
        assert checked[0].status is ContractStatus.FAIL
        assert CompleteBlockerCode.ACCEPTANCE_CONTRACT_FAILED in {
            item.code for item in CompleteGate(store, root).check(run).blockers
        }
