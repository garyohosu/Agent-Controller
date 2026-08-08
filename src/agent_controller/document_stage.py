"""共通 Document Stage Subgraph（指示書 §17-6）。

SPEC / USECASE / SEQUENCE / CLASS / UI / TESTCASE ごとに別の状態機械を作らない。
1 つの Subgraph を設定値（DocumentStageConfig）で振る舞い分けする。

```text
GENERATE
    ↓ DONE
REVIEW_LIGHT ──PASS──────────→ COMPLETE
    ├─ LOCAL_FIX ─→ FIX ─DONE─→ REVIEW_LIGHT
    ├─ QUESTION  ─→ QANDA ─DONE→ 質問元 phase
    └─ SERIOUS_ISSUE ─────────→ REVIEW_DEEP
```

トップレベル State は増えない。stage の中の往復は substate / phase だけで回る。

重要: stage の中の遷移はトップレベルの遷移表を通さない。
stage の PASS は「この文書が通った」であって「DESIGN が終わった」ではないため、
両者を同じ表で扱うと DESIGN + PASS -> IMPLEMENT に化けてしまう。
stage を抜ける Event だけがトップレベルへ伝播する。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agent_controller.models import (
    REVIEW_PHASES,
    DocumentStage,
    Event,
    Phase,
    ReviewLevel,
    Role,
    RunState,
    Transition,
    Worker,
    transition_key,
    utcnow,
)
from agent_controller.guards import LoopGuard, apply_guard
from agent_controller.transition_log import TransitionLogger

EXIT: Final = "EXIT"
"""stage を抜け、Event をトップレベルの遷移表へ渡す。"""

ENTER_REVIEW: Final = "ENTER_REVIEW"
"""その stage で有効なレビュー強度（review_phase）へ入る。"""

RESUME_QUESTION: Final = "RESUME_QUESTION"
"""QANDA へ入る前の phase へ戻る。"""

PhaseTarget = Phase | Literal["EXIT", "ENTER_REVIEW", "RESUME_QUESTION"]


STAGE_TRANSITIONS: Final[dict[tuple[Phase, Event], PhaseTarget]] = {
    # --- GENERATE -----------------------------------------------------------
    (Phase.GENERATE, Event.DONE): ENTER_REVIEW,
    (Phase.GENERATE, Event.QUESTION): Phase.QANDA,
    (Phase.GENERATE, Event.UPSTREAM_CHANGE_REQUIRED): EXIT,
    (Phase.GENERATE, Event.WORKER_ERROR): EXIT,
    (Phase.GENERATE, Event.WORKER_RESOURCE_LIMIT): EXIT,
    (Phase.GENERATE, Event.ABORT_REQUESTED): EXIT,
    # --- REVIEW_LIGHT（FAST PATH。通常はここだけを通る）----------------------
    (Phase.REVIEW_LIGHT, Event.PASS): Phase.COMPLETE,
    (Phase.REVIEW_LIGHT, Event.LOCAL_FIX): Phase.FIX,
    (Phase.REVIEW_LIGHT, Event.QUESTION): Phase.QANDA,
    (Phase.REVIEW_LIGHT, Event.SERIOUS_ISSUE): Phase.REVIEW_DEEP,
    (Phase.REVIEW_LIGHT, Event.UPSTREAM_CHANGE_REQUIRED): EXIT,
    (Phase.REVIEW_LIGHT, Event.RETRY_LIMIT): EXIT,
    (Phase.REVIEW_LIGHT, Event.WORKER_ERROR): EXIT,
    (Phase.REVIEW_LIGHT, Event.WORKER_RESOURCE_LIMIT): EXIT,
    (Phase.REVIEW_LIGHT, Event.ABORT_REQUESTED): EXIT,
    # --- REVIEW_DEEP（SERIOUS_ISSUE が出た時だけ）---------------------------
    (Phase.REVIEW_DEEP, Event.PASS): Phase.COMPLETE,
    (Phase.REVIEW_DEEP, Event.LOCAL_FIX): Phase.FIX,
    (Phase.REVIEW_DEEP, Event.QUESTION): Phase.QANDA,
    # DEEP でさらに重大な問題が出たなら、それは上位工程の問題として扱う。
    (Phase.REVIEW_DEEP, Event.SERIOUS_ISSUE): EXIT,
    (Phase.REVIEW_DEEP, Event.UPSTREAM_CHANGE_REQUIRED): EXIT,
    (Phase.REVIEW_DEEP, Event.RETRY_LIMIT): EXIT,
    (Phase.REVIEW_DEEP, Event.WORKER_ERROR): EXIT,
    (Phase.REVIEW_DEEP, Event.WORKER_RESOURCE_LIMIT): EXIT,
    (Phase.REVIEW_DEEP, Event.ABORT_REQUESTED): EXIT,
    # --- FIX ----------------------------------------------------------------
    (Phase.FIX, Event.DONE): ENTER_REVIEW,
    (Phase.FIX, Event.QUESTION): Phase.QANDA,
    (Phase.FIX, Event.UPSTREAM_CHANGE_REQUIRED): EXIT,
    (Phase.FIX, Event.WORKER_ERROR): EXIT,
    (Phase.FIX, Event.WORKER_RESOURCE_LIMIT): EXIT,
    (Phase.FIX, Event.ABORT_REQUESTED): EXIT,
    # --- QANDA --------------------------------------------------------------
    (Phase.QANDA, Event.DONE): RESUME_QUESTION,
    (Phase.QANDA, Event.LOCAL_FIX): Phase.FIX,
    (Phase.QANDA, Event.CANNOT_ANSWER): EXIT,
    (Phase.QANDA, Event.UPSTREAM_CHANGE_REQUIRED): EXIT,
    (Phase.QANDA, Event.WORKER_ERROR): EXIT,
    (Phase.QANDA, Event.WORKER_RESOURCE_LIMIT): EXIT,
    (Phase.QANDA, Event.ABORT_REQUESTED): EXIT,
}

WORKING_PHASES: Final[tuple[Phase, ...]] = (
    Phase.GENERATE,
    Phase.REVIEW_LIGHT,
    Phase.REVIEW_DEEP,
    Phase.FIX,
    Phase.QANDA,
)
"""node を持つ phase。COMPLETE に入ると stage を抜ける。"""


class DocumentStageConfig(BaseModel):
    """1 つの Document Stage の設定（指示書 §3）。

    ここを変えるだけで SPEC / CLASS / TESTCASE の振る舞いを分ける。
    stage ごとに状態機械を書かない。
    """

    name: DocumentStage
    inputs: list[str] = Field(default_factory=list)
    output: str
    review_level: ReviewLevel = ReviewLevel.LIGHT
    max_review_retry: int = 2


ESCALATING_EVENTS: Final[frozenset[Event]] = frozenset(
    {Event.UPSTREAM_CHANGE_REQUIRED, Event.SERIOUS_ISSUE}
)
"""上位工程へ戻すための離脱。この stage はやり直しになるので再開位置を残さない。

これに対し WORKER_RESOURCE_LIMIT / WORKER_ERROR / CANNOT_ANSWER / RETRY_LIMIT は
「同じ場所へ戻ってくる」中断なので return_phase を残す。
"""


class StageResult(BaseModel):
    """phase の処理結果。§17-11 で AI Worker の応答がこの形に入る。"""

    event: Event
    role: Role | None = None
    worker: Worker | None = None
    reason: str | None = None

    upstream_target: DocumentStage | None = None
    """UPSTREAM_CHANGE_REQUIRED のとき、どの上位工程が問題かを Worker が指す。

    Controller 側で「1 つ上の工程だろう」と推測しない。指定が無ければ受け付けない。
    """


PhaseHandler = Callable[[RunState], StageResult]


class UnknownPhaseTransitionError(LookupError):
    """stage の遷移表に無い (phase, event)。"""

    def __init__(self, phase: Phase, event: Event) -> None:
        self.phase = phase
        self.event = event
        super().__init__(f"no stage transition for {phase.value} + {event.value}")


class MissingUpstreamTargetError(ValueError):
    """UPSTREAM_CHANGE_REQUIRED なのに、どの上位工程かが指定されていない。

    仕様の空白を Controller が推測で埋めないための拒否（指示書 §8 の考え方）。
    """

    def __init__(self, phase: Phase) -> None:
        self.phase = phase
        super().__init__(
            f"{phase.value} raised UPSTREAM_CHANGE_REQUIRED without an upstream_target"
        )


def next_phase(current: Phase, event: Event) -> PhaseTarget:
    """phase + event から次の phase（または EXIT / 動的マーカー）を返す。"""
    try:
        return STAGE_TRANSITIONS[(current, event)]
    except KeyError:
        raise UnknownPhaseTransitionError(current, event) from None


def allowed_stage_events(current: Phase) -> frozenset[Event]:
    return frozenset(event for phase, event in STAGE_TRANSITIONS if phase == current)


def start_stage(
    run: RunState,
    config: DocumentStageConfig,
    entry_phase: Phase = Phase.GENERATE,
    reason: str | None = None,
) -> Transition:
    """stage の開始位置に run を置き、その記録を返す。

    return_phase が残っていれば、そちらが優先される。中断前の review 強度と
    review_retry は捨てない。捨てると、resource limit を挟むだけで
    RETRY_LIMIT を回避できてしまう。

    entry_phase は「新しく始めるときにどこから入るか」。影響範囲分析が
    REVIEW_REQUIRED と判定した工程は、生成をやり直さずレビューから入る（§6）。
    """
    from_substate = run.substate
    resuming = run.return_phase is not None and run.substate == config.name

    run.substate = config.name
    if resuming:
        run.phase = run.return_phase
        run.review_phase = run.review_phase or config.review_level.phase
    else:
        run.phase = entry_phase
        # レビューだけで入る場合は軽量レビュー固定（§6「軽量レビューのみ」）。
        run.review_phase = (
            entry_phase if entry_phase in REVIEW_PHASES else config.review_level.phase
        )
        run.review_retry = 0
        run.question_source_phase = None

    run.return_phase = None
    run.transition_count += 1
    # stage をまたいで repeat が繋がらないようにする。stage の再実行そのものは
    # upstream_rework が数える。
    run.last_transition_key = None
    run.repeat = 0
    run.updated_at = utcnow()

    return Transition(
        run_id=run.run_id,
        state=run.current_state,
        substate=from_substate,
        phase=None,
        from_state=run.current_state,
        from_substate=from_substate,
        event=Event.START,
        to_state=run.current_state,
        to_substate=run.substate,
        to_phase=run.phase,
        role=run.active_role,
        worker=run.active_worker,
        reason="resume" if resuming else reason,
        state_retry=run.state_retry,
        review_retry=run.review_retry,
        repeat=run.repeat,
        checkpoint_commit=run.checkpoint_commit,
    )


def resolve_phase(run: RunState, target: PhaseTarget) -> Phase:
    """動的マーカーを実際の phase に解決する。"""
    if target == ENTER_REVIEW:
        return run.review_phase or Phase.REVIEW_LIGHT
    if target == RESUME_QUESTION:
        if run.question_source_phase is None:
            raise RuntimeError("QANDA has no question_source_phase to return to")
        return run.question_source_phase
    if isinstance(target, Phase):
        return target
    raise RuntimeError(f"{target} is not a phase")


def apply_stage_event(
    run: RunState,
    event: Event,
    to_phase: Phase,
    *,
    role: Role | None = None,
    worker: Worker | None = None,
    reason: str | None = None,
) -> Transition:
    """stage 内の phase 遷移を run に適用し、記録すべき Transition を返す。

    トップレベル State は変わらない。永続化はしない。
    """
    from_phase = run.phase
    if from_phase is None:
        raise RuntimeError("run is not inside a document stage")

    # SERIOUS_ISSUE で DEEP に上がったら、以後この stage は DEEP で回す（§4）。
    if to_phase in REVIEW_PHASES:
        run.review_phase = to_phase
    if to_phase == Phase.QANDA:
        run.question_source_phase = from_phase
    elif from_phase == Phase.QANDA:
        run.question_source_phase = None

    key = transition_key(
        run.current_state,
        run.substate,
        from_phase,
        event,
        run.current_state,
        run.substate,
        to_phase,
    )
    run.repeat = run.repeat + 1 if key == run.last_transition_key else 0
    run.last_transition_key = key

    run.phase = to_phase
    run.last_event = event
    run.transition_count += 1
    run.updated_at = utcnow()

    if role is not None:
        run.active_role = role
    if worker is not None:
        run.active_worker = worker

    return Transition(
        run_id=run.run_id,
        state=run.current_state,
        substate=run.substate,
        phase=from_phase,
        from_state=run.current_state,
        from_substate=run.substate,
        event=event,
        to_state=run.current_state,
        to_substate=run.substate,
        to_phase=to_phase,
        role=role if role is not None else run.active_role,
        worker=worker if worker is not None else run.active_worker,
        reason=reason,
        state_retry=run.state_retry,
        review_retry=run.review_retry,
        repeat=run.repeat,
        checkpoint_commit=run.checkpoint_commit,
    )


def stub_phase_handlers() -> dict[Phase, PhaseHandler]:
    """AI Worker を呼ばない stub。FAST PATH をそのまま通す。"""
    happy_path: dict[Phase, Event] = {
        Phase.GENERATE: Event.DONE,
        Phase.REVIEW_LIGHT: Event.PASS,
        Phase.REVIEW_DEEP: Event.PASS,
        Phase.FIX: Event.DONE,
        Phase.QANDA: Event.DONE,
    }

    def make(event: Event) -> PhaseHandler:
        def handler(run: RunState) -> StageResult:
            return StageResult(event=event, role=Role.CONTROLLER, reason="stub")

        return handler

    return {phase: make(event) for phase, event in happy_path.items()}


class ScriptedPhaseHandlers:
    """phase ごとに返す Event を並べておく stub。使い切ったら最後の Event を返し続ける。"""

    def __init__(self, script: dict[Phase, list[Event | StageResult]]) -> None:
        self._script = {
            phase: [
                item if isinstance(item, StageResult) else StageResult(event=item)
                for item in results
            ]
            for phase, results in script.items()
        }
        self._cursor: dict[Phase, int] = {phase: 0 for phase in self._script}

    def as_handlers(self) -> dict[Phase, PhaseHandler]:
        base = stub_phase_handlers()

        def make(phase: Phase) -> PhaseHandler:
            def handler(run: RunState) -> StageResult:
                results = self._script[phase]
                index = min(self._cursor[phase], len(results) - 1)
                self._cursor[phase] += 1
                return results[index]

            return handler

        return {**base, **{phase: make(phase) for phase in self._script}}


def _make_phase_node(
    phase: Phase,
    config: DocumentStageConfig,
    logger: TransitionLogger,
    handlers: dict[Phase, PhaseHandler],
    guard: LoopGuard | None,
) -> Callable[[RunState], dict[str, Any]]:
    def node(run: RunState) -> dict[str, Any]:
        state = run.current_state
        substate = run.substate
        result = handlers[phase](run)
        event = result.event
        target = next_phase(phase, event)

        # FIX からレビューへ戻るたびに数え、上限を超えたら RETRY_LIMIT で stage を抜ける。
        # 回数の超過だけを見る。同じ指摘の繰り返し（NO_PROGRESS）は §17-9。
        if target == ENTER_REVIEW and phase == Phase.FIX:
            run.review_retry += 1
            if run.review_retry > config.max_review_retry:
                event = Event.RETRY_LIMIT
                target = EXIT

        if target == EXIT:
            if event == Event.UPSTREAM_CHANGE_REQUIRED:
                if result.upstream_target is None:
                    raise MissingUpstreamTargetError(phase)
                run.pending_upstream_stage = result.upstream_target

            # 同じ場所へ戻ってくる中断だけ再開位置を残す。
            run.return_phase = None if event in ESCALATING_EVENTS else phase

            # ここだけトップレベルの遷移表へ渡す。substate は残し、phase は外す。
            logger.record(
                run,
                event,
                to_substate=run.substate,
                role=result.role,
                worker=result.worker,
                reason=result.reason,
            )
            return run.model_dump()

        transition = apply_stage_event(
            run,
            event,
            resolve_phase(run, target),
            role=result.role,
            worker=result.worker,
            reason=result.reason,
        )
        logger.persist(run, transition)

        # 起きたことを記録してから歯止めを見る。順序を逆にすると、
        # 実際に起きた遷移がログから消える。
        apply_guard(
            logger,
            guard,
            run,
            event,
            state,
            substate,
            phase,
            result.reason,
            transition.worker,
        )
        return run.model_dump()

    return node


def _route(run: RunState) -> str:
    """phase を持ち、かつトップレベル State が変わっていない間だけ回り続ける。"""
    if run.phase in WORKING_PHASES:
        return run.phase.value
    return END


def build_document_stage(
    config: DocumentStageConfig,
    logger: TransitionLogger,
    handlers: dict[Phase, PhaseHandler] | None = None,
    guard: LoopGuard | None = None,
) -> Any:
    """1 つの Document Stage の Subgraph を組んで compile する。"""
    handlers = handlers if handlers is not None else stub_phase_handlers()
    missing = [phase.value for phase in WORKING_PHASES if phase not in handlers]
    if missing:
        raise ValueError(f"phase handlers missing for: {', '.join(missing)}")

    builder = StateGraph(RunState)
    for phase in WORKING_PHASES:
        builder.add_node(
            phase.value, _make_phase_node(phase, config, logger, handlers, guard)
        )

    destinations = {phase.value: phase.value for phase in WORKING_PHASES}
    destinations[END] = END

    # 入口も遷移表に従わせる。中断した stage を止まった phase から再開できる。
    builder.add_conditional_edges(START, _route, destinations)
    for phase in WORKING_PHASES:
        builder.add_conditional_edges(phase.value, _route, destinations)

    return builder.compile()


def run_document_stage(
    run: RunState,
    config: DocumentStageConfig,
    logger: TransitionLogger,
    handlers: dict[Phase, PhaseHandler] | None = None,
    guard: LoopGuard | None = None,
    entry_phase: Phase = Phase.GENERATE,
    entry_reason: str | None = None,
    recursion_limit: int = 100,
) -> RunState:
    """stage を COMPLETE か EXIT まで進める。

    run がまだこの stage に入っていなければ entry_phase から始める。
    return_phase が残っていればそこから再開する。
    """
    if run.substate != config.name or run.phase is None:
        logger.persist(run, start_stage(run, config, entry_phase, entry_reason))

    graph = build_document_stage(config, logger, handlers, guard)
    result = graph.invoke(run, config={"recursion_limit": recursion_limit})
    return RunState.model_validate(result)


def stage_completed(run: RunState, config: DocumentStageConfig) -> bool:
    """その stage を通過したか。EXIT で抜けた場合は False。"""
    return run.substate == config.name and run.phase == Phase.COMPLETE
