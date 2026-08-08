from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_controller.models import RunState
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as store:
        yield store


@pytest.fixture
def logger(store: Store) -> TransitionLogger:
    return TransitionLogger(store)


@pytest.fixture
def run(store: Store) -> RunState:
    run = RunState(project_id="agent-controller", run_id="run-0001")
    store.save_run(run)
    return run
