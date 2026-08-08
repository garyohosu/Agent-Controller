"""小さな Main Graph（指示書 §17-5）。

トップレベル State だけを持つ。SPEC / USECASE / CLASS などの反復は
ここには出さず、§17-6 の DocumentStage Subgraph に入れる。

LangGraph は実行の入れ物にすぎない。次にどこへ行くかを決めるのは常に
transitions.py の遷移表であって、node でも AI でもない。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from agent_controller.models import Event, Role, RunState, State, Worker
from agent_controller.transition_log import TransitionLogger

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
) -> Callable[[RunState], dict[str, Any]]:
    def node(run: RunState) -> dict[str, Any]:
        if run.current_state != state:
            raise UnexpectedStateError(state, run.current_state)

        result = handlers[state](run)
        logger.record(
            run,
            result.event,
            role=result.role,
            worker=result.worker,
            reason=result.reason,
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
) -> Any:
    """トップレベル Graph を組んで compile する。"""
    handlers = handlers if handlers is not None else stub_handlers()
    missing = [state.value for state in EXECUTABLE_STATES if state not in handlers]
    if missing:
        raise ValueError(f"handlers missing for: {', '.join(missing)}")

    builder = StateGraph(RunState)
    for state in EXECUTABLE_STATES:
        builder.add_node(state.value, _make_node(state, logger, handlers))

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
    recursion_limit: int = 100,
) -> RunState:
    """run を停止 State まで進め、最終 RunState を返す。

    recursion_limit は LangGraph 側の暴走止め。工程としての loop guard は
    §17-9 で Controller 側に入れる（ここでは未実装）。
    """
    graph = build_main_graph(logger, handlers)
    result = graph.invoke(run, config={"recursion_limit": recursion_limit})
    return RunState.model_validate(result)
