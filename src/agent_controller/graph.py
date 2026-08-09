"""小さな Main Graph（指示書 §17-5）。

トップレベル State だけを持つ。SPEC / USECASE / CLASS などの反復は
ここには出さず、§17-6 の DocumentStage Subgraph に入れる。

LangGraph は実行の入れ物にすぎない。次にどこへ行くかを決めるのは常に
transitions.py の遷移表であって、node でも AI でもない。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from agent_controller.guards import LoopGuard, apply_guard
from agent_controller.complete import CompleteGate
from agent_controller.design import default_design_stages, run_design
from agent_controller.git_checkpoint import GitCheckpointManager
from agent_controller.models import (
    ArtifactKind,
    ArtifactState,
    ArtifactStatus,
    Event,
    Phase,
    Role,
    RunState,
    State,
    Worker,
)
from agent_controller.transition_log import TransitionLogger
from agent_controller.worker import WorkerAdapter, phase_handlers_from_worker
from agent_controller.qanda import QandaFile

EXECUTABLE_STATES: tuple[State, ...] = (
    State.IDLE,
    State.DESIGN,
    State.IMPLEMENT,
    State.TEST,
    State.REVIEW,
    State.DOC_SYNC,
)
"""node を持つ State。COMPLETE / ABORT / HUMAN_REQUIRED / WAIT_RESOURCE は
graph を抜ける（終了、または人間・リソース待ちで停止）。"""


class StageResult(BaseModel):
    """State の処理結果。

    §17-11 で Worker interface を作るとき、AI Worker の応答がこの形に入る。
    今は stub がこれを返す。
    """

    event: Event
    role: Role | None = None
    worker: Worker | None = None
    reason: str | None = None
    handled: bool = False


StageHandler = Callable[[RunState], StageResult]


class UnexpectedStateError(RuntimeError):
    """node に、その node が担当していない State の run が渡された。"""

    def __init__(self, expected: State, actual: State) -> None:
        super().__init__(
            f"node for {expected.value} received a run in {actual.value}"
        )


def stub_handlers() -> dict[State, StageHandler]:
    """AI Worker を呼ばない stub。happy path をそのまま流す。

    §17-11 / §17-12 で実 Worker adapter に差し替える。
    """
    happy_path: dict[State, Event] = {
        State.IDLE: Event.START,
        State.DESIGN: Event.PASS,
        State.IMPLEMENT: Event.DONE,
        State.TEST: Event.PASS,
        State.REVIEW: Event.PASS,
        State.DOC_SYNC: Event.PASS,
    }

    def make(event: Event) -> StageHandler:
        def handler(run: RunState) -> StageResult:
            return StageResult(event=event, role=Role.CONTROLLER, reason="stub")

        return handler

    return {state: make(event) for state, event in happy_path.items()}


def _default_test_runner(run: RunState, workspace: str) -> int:
    del run
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode


def wired_handlers(
    logger: TransitionLogger,
    *,
    workspace: str | Path,
    design_phase_handlers: dict | None = None,
    implementer: WorkerAdapter | None = None,
    reviewer: WorkerAdapter | None = None,
    git: GitCheckpointManager | None = None,
    test_runner: Callable[[RunState], int] | None = None,
    readme_status: str = "NOT_REQUIRED",
    design_stages: list | None = None,
    qanda: Any | None = None,
) -> dict[State, StageHandler]:
    """Build the real Main Graph handlers from existing Controller components."""
    workspace = str(Path(workspace).resolve())
    git = git if git is not None else GitCheckpointManager(workspace, logger.store)
    test_runner = test_runner or (lambda run: _default_test_runner(run, workspace))
    qanda = qanda if qanda is not None else QandaFile(logger.store, workspace)

    def design_handler(run: RunState) -> StageResult:
        updated = run_design(
            run,
            logger,
            stages=design_stages or default_design_stages(),
            handlers=design_phase_handlers,
            workspace=workspace,
            qanda=qanda,
            emit_completion_event=False,
        )
        # run_design uses the document-stage graph and returns a new validated
        # RunState.  Copy it back so the outer Main Graph cannot continue from
        # a stale DESIGN object after HUMAN_REQUIRED/WAIT_RESOURCE.
        run.__dict__.update(updated.__dict__)
        if run.current_state != State.DESIGN:
            return StageResult(event=run.last_event or Event.WORKER_ERROR, handled=True)
        return StageResult(event=Event.PASS, role=Role.CONTROLLER)

    def implement_handler(run: RunState) -> StageResult:
        if implementer is None:
            return StageResult(event=Event.WORKER_ERROR, reason="IMPLEMENT worker is not configured")
        try:
            handlers = phase_handlers_from_worker(
                implementer, workspace, [], "CODE", git=git
            )
            result = handlers[Phase.GENERATE](run)
        except Exception as error:
            return StageResult(event=Event.WORKER_ERROR, reason=f"IMPLEMENT failed: {error}")
        if result.event == Event.QUESTION and qanda is not None:
            qanda.open_question(
                run,
                question=result.question or result.reason or "(no question text)",
                context=result.reason,
                asked_role=result.role,
                asked_worker=result.worker,
            )
        if result.event == Event.DONE:
            logger.store.save_artifact(ArtifactState(
                run_id=run.run_id, kind=ArtifactKind.CODE,
                status=ArtifactStatus.VALID, path="CODE",
            ))
        return StageResult(
            event=result.event, role=result.role, worker=result.worker,
            reason=result.reason, handled=False,
        )

    def test_handler(run: RunState) -> StageResult:
        try:
            exit_code = test_runner(run)
        except OSError as error:
            return StageResult(event=Event.WORKER_ERROR, role=Role.CONTROLLER,
                               reason=f"test runner failed: {error}")
        return StageResult(
            event=Event.PASS if exit_code == 0 else Event.FAIL,
            role=Role.CONTROLLER,
            reason=f"test runner exit_code={exit_code}",
        )

    def review_handler(run: RunState) -> StageResult:
        if reviewer is None:
            return StageResult(event=Event.WORKER_ERROR, reason="REVIEW worker is not configured")
        try:
            handlers = phase_handlers_from_worker(
                reviewer, workspace, ["CODE"], "CODE", git=git
            )
            result = handlers[Phase.REVIEW_LIGHT](run)
        except Exception as error:
            return StageResult(event=Event.WORKER_ERROR, reason=f"REVIEW failed: {error}")
        if result.event == Event.QUESTION and qanda is not None:
            qanda.open_question(
                run,
                question=result.question or result.reason or "(no question text)",
                context=result.reason,
                asked_role=result.role,
                asked_worker=result.worker,
            )
        return StageResult(
            event=result.event, role=result.role, worker=result.worker,
            reason=result.reason,
        )

    def doc_sync_handler(run: RunState) -> StageResult:
        if readme_status not in {"LATEST", "NOT_REQUIRED"}:
            return StageResult(event=Event.FAIL, role=Role.CONTROLLER,
                               reason="invalid structured README status")
        logger.store.save_artifact(ArtifactState(
            run_id=run.run_id, kind=ArtifactKind.README,
            status=ArtifactStatus.VALID, path="README.md", reason=readme_status,
        ))
        try:
            git.push()
        except Exception as error:
            return StageResult(
                event=Event.WORKER_ERROR,
                role=Role.CONTROLLER,
                reason=f"git push failed: {error}",
            )
        return StageResult(event=Event.PASS, role=Role.CONTROLLER,
                           reason=f"README={readme_status}")

    return {
        State.IDLE: stub_handlers()[State.IDLE],
        State.DESIGN: design_handler,
        State.IMPLEMENT: implement_handler,
        State.TEST: test_handler,
        State.REVIEW: review_handler,
        State.DOC_SYNC: doc_sync_handler,
    }


class ScriptedHandlers:
    """State ごとに返す Event をあらかじめ並べておく stub。

    分岐（TEST FAIL → IMPLEMENT など）を含む経路をテストするために使う。
    並べた分を使い切ったら、その State は最後の Event を返し続ける。
    """

    def __init__(self, script: dict[State, list[Event | StageResult]]) -> None:
        self._script = {
            state: [
                item if isinstance(item, StageResult) else StageResult(event=item)
                for item in results
            ]
            for state, results in script.items()
        }
        self._cursor: dict[State, int] = {state: 0 for state in self._script}

    def __call__(self, state: State) -> StageHandler:
        def handler(run: RunState) -> StageResult:
            results = self._script[state]
            index = min(self._cursor[state], len(results) - 1)
            self._cursor[state] += 1
            return results[index]

        return handler

    def as_handlers(self) -> dict[State, StageHandler]:
        return {state: self(state) for state in self._script}


def _make_node(
    state: State,
    logger: TransitionLogger,
    handlers: dict[State, StageHandler],
    guard: LoopGuard | None,
) -> Callable[[RunState], dict[str, Any]]:
    def node(run: RunState) -> dict[str, Any]:
        if run.current_state != state:
            raise UnexpectedStateError(state, run.current_state)

        result = handlers[state](run)
        if result.handled:
            return run.model_dump()
        transition = logger.record(
            run,
            result.event,
            role=result.role,
            worker=result.worker,
            reason=result.reason,
        )
        apply_guard(
            logger,
            guard,
            run,
            result.event,
            state,
            reason=result.reason,
            worker=transition.worker,
        )
        return run.model_dump()

    return node


def _route(run: RunState) -> str:
    """遷移表が決めた次 State の node へ渡す。ここに判断は無い。"""
    if run.current_state in EXECUTABLE_STATES:
        return run.current_state.value
    return END


def build_main_graph(
    logger: TransitionLogger,
    handlers: dict[State, StageHandler] | None = None,
    guard: LoopGuard | None = None,
    complete_gate: CompleteGate | None = None,
) -> Any:
    """トップレベル Graph を組んで compile する。"""
    if complete_gate is not None:
        logger.complete_gate = complete_gate
    handlers = handlers if handlers is not None else stub_handlers()
    missing = [state.value for state in EXECUTABLE_STATES if state not in handlers]
    if missing:
        raise ValueError(f"handlers missing for: {', '.join(missing)}")

    builder = StateGraph(RunState)
    for state in EXECUTABLE_STATES:
        builder.add_node(state.value, _make_node(state, logger, handlers, guard))

    destinations = {state.value: state.value for state in EXECUTABLE_STATES}
    destinations[END] = END

    # 入口も遷移表に従わせる。これにより WAIT_RESOURCE / HUMAN_REQUIRED から
    # 復帰した run を、止まった State からそのまま再開できる（§12）。
    builder.add_conditional_edges(START, _route, destinations)
    for state in EXECUTABLE_STATES:
        builder.add_conditional_edges(state.value, _route, destinations)

    return builder.compile()


def run_graph(
    run: RunState,
    logger: TransitionLogger,
    handlers: dict[State, StageHandler] | None = None,
    guard: LoopGuard | None = None,
    recursion_limit: int = 100,
    complete_gate: CompleteGate | None = None,
) -> RunState:
    """run を停止 State まで進め、最終 RunState を返す。

    通常のループ停止は guard（guards.py）が行う。recursion_limit はその後ろに
    残してある最後の非常停止装置で、guard が取りこぼした場合にだけ働く。
    """
    graph = build_main_graph(logger, handlers, guard, complete_gate)
    result = graph.invoke(run, config={"recursion_limit": recursion_limit})
    return RunState.model_validate(result)
