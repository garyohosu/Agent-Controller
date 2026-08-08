"""State Transition Logger（指示書 §10）。

人間が真っ先に見るログ。「どの State で、どの Event により、どこへ戻ったか」を
これだけで追えることを目標にする。

SQLite の transitions 行が正本で、テキストはそこから生成する純関数とする。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import tzinfo

from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    Role,
    RunState,
    State,
    Transition,
    Worker,
)
from agent_controller.store import Store
from agent_controller.transitions import apply_event

_INDENT = " " * 9
"""2 行目の字下げ。1 行目の "HH:MM:SS | " と桁を合わせる。"""


def format_position(
    state: State,
    substate: DocumentStage | None = None,
    phase: Phase | None = None,
) -> str:
    """位置を ``DESIGN/CLASS/REVIEW`` の形にする。None の段は落とす。"""
    parts = [state.value]
    if substate is not None:
        parts.append(substate.value)
    if phase is not None:
        parts.append(phase.value)
    return "/".join(parts)


def render_transition(transition: Transition, tz: tzinfo | None = None) -> str:
    """遷移 1 件を指示書 §10 の表示形式にする。

    tz を省略するとローカル時刻で表示する。テストでは UTC を渡して固定する。
    """
    timestamp = transition.timestamp.astimezone(tz)
    origin = format_position(transition.state, transition.substate, transition.phase)
    target = format_position(
        transition.to_state, transition.to_substate, transition.to_phase
    )

    head = f"{timestamp:%H:%M:%S} | {origin} | {transition.event.value}"

    details: list[str] = [f"-> {target}"]
    if transition.worker is not None:
        details.append(f"worker={transition.worker.value}")
    if transition.role is not None:
        details.append(f"role={transition.role.value}")
    if transition.retry_count:
        details.append(f"retry={transition.retry_count}")
    if transition.checkpoint_commit is not None:
        details.append(f"checkpoint={transition.checkpoint_commit}")
    if transition.reason is not None:
        details.append(f"reason={transition.reason}")

    return f"{head}\n{_INDENT}{' | '.join(details)}"


def render_log(transitions: Iterable[Transition], tz: tzinfo | None = None) -> str:
    """遷移列を人間向けテキストログにする。"""
    return "\n".join(render_transition(item, tz) for item in transitions)


class TransitionLogger:
    """run に Event を適用し、遷移を SQLite へ記録する。

    Controller から見た唯一の「状態を進める」入口。ここを通さない遷移は作らない。
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def persist(self, run: RunState, transition: Transition) -> Transition:
        """すでに組み立てた遷移を保存する。

        Subgraph 側（document_stage.py）が phase 間の遷移を記録するのに使う。
        phase 間の移動はトップレベルの遷移表を通さないため、apply_event は呼ばない。
        """
        self.store.append_transition(transition)
        self.store.save_run(run)
        return transition

    def record(
        self,
        run: RunState,
        event: Event,
        *,
        to_substate: DocumentStage | None = None,
        to_phase: Phase | None = None,
        role: Role | None = None,
        worker: Worker | None = None,
        reason: str | None = None,
    ) -> Transition:
        """event を適用し、遷移行と更新後の run を保存する。"""
        transition = apply_event(
            run,
            event,
            to_substate=to_substate,
            to_phase=to_phase,
            role=role,
            worker=worker,
            reason=reason,
        )
        return self.persist(run, transition)

    def history(self, run_id: str) -> list[Transition]:
        return self.store.transitions(run_id)

    def render(self, run_id: str, tz: tzinfo | None = None) -> str:
        """保存済みの遷移から人間向けログを組み立てる。"""
        return render_log(self.history(run_id), tz)
