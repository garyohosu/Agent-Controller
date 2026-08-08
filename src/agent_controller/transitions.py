"""トップレベルの遷移表。

指示書 §7 の

    current_state + event -> next_state

を担う Controller の中核。ここは純 Python であり、LangGraph にも AI Worker にも
依存しない。Controller の正しさはこの表と next_state() だけで検証できる。

遷移先が実行時にしか決まらないもの（HUMAN_REQUIRED / WAIT_RESOURCE からの復帰）は
RESUME を置き、next_state() が RunState.return_state を使って解決する。
"""

from __future__ import annotations

from typing import Final, Literal

from agent_controller.models import (
    PAUSE_STATES,
    DocumentStage,
    Event,
    Phase,
    Role,
    RunState,
    RunStatus,
    State,
    Transition,
    Worker,
    utcnow,
)

RESUME: Final = "RESUME"
"""動的遷移マーカー。実際の遷移先は return_state が持つ。"""

TransitionTarget = State | Literal["RESUME"]


TRANSITIONS: Final[dict[tuple[State, Event], TransitionTarget]] = {
    # --- IDLE ---------------------------------------------------------------
    (State.IDLE, Event.START): State.DESIGN,
    (State.IDLE, Event.ABORT_REQUESTED): State.ABORT,
    # --- DESIGN -------------------------------------------------------------
    # 設計工程の反復（LOCAL_FIX / QUESTION / SERIOUS_ISSUE / IMPACT_ANALYSIS）は
    # DESIGN の内側で閉じる。ここで自己ループになっているものは §17-6 以降で
    # DocumentStage Subgraph に置き換わり、トップレベルからは見えなくなる。
    (State.DESIGN, Event.PASS): State.IMPLEMENT,
    (State.DESIGN, Event.LOCAL_FIX): State.DESIGN,
    (State.DESIGN, Event.QUESTION): State.DESIGN,
    (State.DESIGN, Event.SERIOUS_ISSUE): State.DESIGN,
    (State.DESIGN, Event.UPSTREAM_CHANGE_REQUIRED): State.DESIGN,
    (State.DESIGN, Event.CANNOT_ANSWER): State.HUMAN_REQUIRED,
    (State.DESIGN, Event.RETRY_LIMIT): State.HUMAN_REQUIRED,
    (State.DESIGN, Event.NO_PROGRESS): State.HUMAN_REQUIRED,
    (State.DESIGN, Event.LOOP_DETECTED): State.HUMAN_REQUIRED,
    (State.DESIGN, Event.WORKER_ERROR): State.HUMAN_REQUIRED,
    (State.DESIGN, Event.WORKER_RESOURCE_LIMIT): State.WAIT_RESOURCE,
    (State.DESIGN, Event.ABORT_REQUESTED): State.ABORT,
    # --- IMPLEMENT ----------------------------------------------------------
    (State.IMPLEMENT, Event.DONE): State.TEST,
    # QUESTION は QandA.md → Director → Implementer で IMPLEMENT 内に閉じる（§8）。
    (State.IMPLEMENT, Event.QUESTION): State.IMPLEMENT,
    (State.IMPLEMENT, Event.UPSTREAM_CHANGE_REQUIRED): State.DESIGN,
    (State.IMPLEMENT, Event.CANNOT_ANSWER): State.HUMAN_REQUIRED,
    (State.IMPLEMENT, Event.RETRY_LIMIT): State.HUMAN_REQUIRED,
    (State.IMPLEMENT, Event.NO_PROGRESS): State.HUMAN_REQUIRED,
    (State.IMPLEMENT, Event.LOOP_DETECTED): State.HUMAN_REQUIRED,
    (State.IMPLEMENT, Event.WORKER_ERROR): State.HUMAN_REQUIRED,
    (State.IMPLEMENT, Event.WORKER_RESOURCE_LIMIT): State.WAIT_RESOURCE,
    (State.IMPLEMENT, Event.ABORT_REQUESTED): State.ABORT,
    # --- TEST ---------------------------------------------------------------
    (State.TEST, Event.PASS): State.REVIEW,
    (State.TEST, Event.FAIL): State.IMPLEMENT,
    (State.TEST, Event.RETRY_LIMIT): State.HUMAN_REQUIRED,
    (State.TEST, Event.NO_PROGRESS): State.HUMAN_REQUIRED,
    (State.TEST, Event.LOOP_DETECTED): State.HUMAN_REQUIRED,
    (State.TEST, Event.WORKER_ERROR): State.HUMAN_REQUIRED,
    (State.TEST, Event.WORKER_RESOURCE_LIMIT): State.WAIT_RESOURCE,
    (State.TEST, Event.ABORT_REQUESTED): State.ABORT,
    # --- REVIEW -------------------------------------------------------------
    (State.REVIEW, Event.PASS): State.DOC_SYNC,
    # 指摘は Reviewer に直接返さず、必ず Implementer の修正 → 再レビューにする（§8）。
    (State.REVIEW, Event.FAIL): State.IMPLEMENT,
    (State.REVIEW, Event.LOCAL_FIX): State.IMPLEMENT,
    # Director が既存成果物から回答できた場合はレビューを続行する（§9）。
    (State.REVIEW, Event.QUESTION): State.REVIEW,
    (State.REVIEW, Event.SERIOUS_ISSUE): State.DESIGN,
    (State.REVIEW, Event.UPSTREAM_CHANGE_REQUIRED): State.DESIGN,
    (State.REVIEW, Event.CANNOT_ANSWER): State.HUMAN_REQUIRED,
    (State.REVIEW, Event.RETRY_LIMIT): State.HUMAN_REQUIRED,
    (State.REVIEW, Event.NO_PROGRESS): State.HUMAN_REQUIRED,
    (State.REVIEW, Event.LOOP_DETECTED): State.HUMAN_REQUIRED,
    (State.REVIEW, Event.WORKER_ERROR): State.HUMAN_REQUIRED,
    (State.REVIEW, Event.WORKER_RESOURCE_LIMIT): State.WAIT_RESOURCE,
    (State.REVIEW, Event.ABORT_REQUESTED): State.ABORT,
    # --- DOC_SYNC -----------------------------------------------------------
    (State.DOC_SYNC, Event.PASS): State.COMPLETE,
    (State.DOC_SYNC, Event.FAIL): State.DOC_SYNC,
    (State.DOC_SYNC, Event.LOCAL_FIX): State.DOC_SYNC,
    (State.DOC_SYNC, Event.QUESTION): State.DOC_SYNC,
    (State.DOC_SYNC, Event.CANNOT_ANSWER): State.HUMAN_REQUIRED,
    (State.DOC_SYNC, Event.RETRY_LIMIT): State.HUMAN_REQUIRED,
    (State.DOC_SYNC, Event.NO_PROGRESS): State.HUMAN_REQUIRED,
    (State.DOC_SYNC, Event.LOOP_DETECTED): State.HUMAN_REQUIRED,
    (State.DOC_SYNC, Event.WORKER_ERROR): State.HUMAN_REQUIRED,
    (State.DOC_SYNC, Event.WORKER_RESOURCE_LIMIT): State.WAIT_RESOURCE,
    (State.DOC_SYNC, Event.ABORT_REQUESTED): State.ABORT,
    # --- HUMAN_REQUIRED -----------------------------------------------------
    (State.HUMAN_REQUIRED, Event.HUMAN_ANSWER): RESUME,
    (State.HUMAN_REQUIRED, Event.ABORT_REQUESTED): State.ABORT,
    # --- WAIT_RESOURCE ------------------------------------------------------
    # Worker 切替後、開始時 checkpoint へ rollback して同一 State を再実行する（§12）。
    (State.WAIT_RESOURCE, Event.RESOURCE_AVAILABLE): RESUME,
    (State.WAIT_RESOURCE, Event.ABORT_REQUESTED): State.ABORT,
}


class UnknownTransitionError(LookupError):
    """遷移表に無い (state, event) の組み合わせ。

    Controller は決定論的に動くので、未定義の遷移は黙って無視せず例外にする。
    """

    def __init__(self, state: State, event: Event) -> None:
        self.state = state
        self.event = event
        super().__init__(f"no transition defined for {state.value} + {event.value}")


class MissingReturnStateError(RuntimeError):
    """RESUME 遷移なのに return_state が無い。"""

    def __init__(self, state: State, event: Event) -> None:
        self.state = state
        self.event = event
        super().__init__(
            f"{state.value} + {event.value} resumes a previous state "
            "but return_state is not set"
        )


def next_state(
    current: State,
    event: Event,
    return_state: State | None = None,
) -> State:
    """current + event から次の State を返す。

    HUMAN_REQUIRED / WAIT_RESOURCE からの復帰だけは return_state が必要。
    """
    try:
        target = TRANSITIONS[(current, event)]
    except KeyError:
        raise UnknownTransitionError(current, event) from None

    if target == RESUME:
        if return_state is None:
            raise MissingReturnStateError(current, event)
        return return_state
    return target


def allowed_events(current: State) -> frozenset[Event]:
    """その State で受け付ける Event の一覧。"""
    return frozenset(event for state, event in TRANSITIONS if state == current)


def _status_for(state: State) -> RunStatus:
    if state == State.COMPLETE:
        return RunStatus.COMPLETED
    if state == State.ABORT:
        return RunStatus.ABORTED
    if state in PAUSE_STATES:
        return RunStatus.WAITING
    return RunStatus.RUNNING


def apply_event(
    run: RunState,
    event: Event,
    *,
    to_substate: DocumentStage | None = None,
    to_phase: Phase | None = None,
    role: Role | None = None,
    worker: Worker | None = None,
    reason: str | None = None,
) -> Transition:
    """run に event を適用し、記録すべき Transition を返す。

    run は破壊的に更新される。永続化は行わない（transition_log.py の責務）。

    retry_count は自己ループで増え、State が変わるとリセットされる。
    ここでは数えるだけで上限判定はしない（loop guard は §17-9）。
    """
    from_state = run.current_state
    from_substate = run.substate
    from_phase = run.phase

    target = next_state(from_state, event, run.return_state)

    run.previous_state = from_state
    run.previous_substate = from_substate
    run.current_state = target
    run.substate = to_substate
    run.phase = to_phase
    run.last_event = event
    run.transition_count += 1
    run.retry_count = run.retry_count + 1 if target == from_state else 0
    run.status = _status_for(target)
    run.updated_at = utcnow()

    if role is not None:
        run.active_role = role
    if worker is not None:
        run.active_worker = worker

    # 中断 State へ入るときは戻り先を保存し、復帰したら捨てる（§9 / §12）。
    if target in PAUSE_STATES:
        run.return_state = from_state
    elif from_state in PAUSE_STATES:
        run.return_state = None

    return Transition(
        run_id=run.run_id,
        state=from_state,
        substate=from_substate,
        phase=from_phase,
        from_state=from_state,
        from_substate=from_substate,
        event=event,
        to_state=target,
        to_substate=to_substate,
        to_phase=to_phase,
        role=role if role is not None else run.active_role,
        worker=worker if worker is not None else run.active_worker,
        reason=reason,
        retry_count=run.retry_count,
        checkpoint_commit=run.checkpoint_commit,
    )
