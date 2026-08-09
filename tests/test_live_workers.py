"""Opt-in CLI contract checks; normal pytest never calls external AI."""

import os

import pytest

from agent_controller.cli_worker import AgyCliWorker, ClaudeCodeWorker, CodexCliWorker, GrokCliWorker


pytestmark = pytest.mark.live


@pytest.mark.skipif(os.getenv("AGENT_CONTROLLER_LIVE") != "1", reason="opt-in live AI contract")
@pytest.mark.parametrize("worker", [ClaudeCodeWorker(), CodexCliWorker(), GrokCliWorker(), AgyCliWorker()])
def test_live_worker_contract_is_opt_in(worker):
    """The real invocation is deliberately gated; shape tests remain offline."""
    assert worker.name.value in {"CLAUDE_CODE", "CODEX_CLI", "GROK", "ANTIGRAVITY"}
