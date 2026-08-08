"""無限ループ防止（指示書 §11 / §17-9）。

AI に「これは無限ループか」を判定させない。Controller が機械的な上限を持つ。

歯止めは 2 種類ある。

- **回数の上限** — 何回繰り返したかだけを見る。純関数で判定できる
- **NO_PROGRESS** — 同じ失敗が繰り返されているかを指紋で見る。履歴が要る

どちらも発火すると Event を返し、呼び出し側がそれを 1 遷移として記録する。
つまり「起きたこと」と「歯止めが働いたこと」は別の行としてログに残る。

LangGraph の recursion_limit はここには関係しない。あれは最後の非常停止装置であって、
通常のループ停止はこちらで行う。
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    RunState,
    State,
    Worker,
)
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger
from agent_controller.transitions import TRANSITIONS

TRACKED_EVENTS: frozenset[Event] = frozenset(
    {
        Event.FAIL,
        Event.LOCAL_FIX,
        Event.QUESTION,
        Event.SERIOUS_ISSUE,
        Event.UPSTREAM_CHANGE_REQUIRED,
        Event.WORKER_ERROR,
    }
)
"""指紋を取る Event。

「問題が起きた」ことを表すものだけを数える。DONE や PASS を数えても
進んでいるだけなので意味がない（§11 の「同一 test failure / review finding の
繰り返し検出」）。
"""


class GuardLimits(BaseModel):
    """run 全体にかかる上限（指示書 §11）。すべて設定可能にする。

    Document Stage 内のレビュー往復の上限は stage ごとに変えたいので
    DocumentStageConfig.max_review_retry 側に置いてある。
    """

    max_state_retry: int = 5
    """同じトップレベル State を続けて実行できる回数。"""

    max_same_transition: int = 3
    """まったく同じ遷移を続けられる回数。"""

    max_total_transitions: int = 500
    """1 run の総遷移数。"""

    max_upstream_rework: int = 3
    """上位工程へ戻せる回数。"""

    max_same_fingerprint: int = 3
    """同じ失敗の指紋を許す回数。"""


class GuardVerdict(BaseModel):
    """歯止めが働いたときに、どの Event をどんな理由で出すか。"""

    event: Event
    reason: str


def check_counters(run: RunState, limits: GuardLimits) -> GuardVerdict | None:
    """回数の上限だけを見る純関数。履歴も store も要らない。

    複数同時に超えることがあるので順序を決めてある。具体的な事象を表すものを先に、
    包括的なものを後に置く。最初に一致したものを返す。
    """
    if run.upstream_rework > limits.max_upstream_rework:
        return GuardVerdict(
            event=Event.LOOP_DETECTED,
            reason=f"upstream rework {run.upstream_rework} > {limits.max_upstream_rework}",
        )

    if run.repeat > limits.max_same_transition:
        return GuardVerdict(
            event=Event.LOOP_DETECTED,
            reason=f"same transition repeated {run.repeat} > {limits.max_same_transition}",
        )

    if run.state_retry > limits.max_state_retry:
        return GuardVerdict(
            event=Event.RETRY_LIMIT,
            reason=f"state retry {run.state_retry} > {limits.max_state_retry}",
        )

    if run.transition_count > limits.max_total_transitions:
        return GuardVerdict(
            event=Event.LOOP_DETECTED,
            reason=(
                f"transition count {run.transition_count} > {limits.max_total_transitions}"
            ),
        )

    return None


def failure_fingerprint(
    state: State,
    substate: DocumentStage | None,
    phase: Phase | None,
    event: Event,
    reason: str | None,
    finding_code: str | None = None,
    finding_subject: str | None = None,
) -> str:
    """同じ失敗かどうかを判定するための指紋。

    機械判定は構造化された項目で行い、reason は人間向けに残す。

        機械判定 -> finding_code / finding_subject
        人間向け -> reason

    自由文を指紋にすると「2 tests failed」と「2 failing tests」が別物になり、
    実 AI を繋いだ途端に NO_PROGRESS が働かなくなる（実測で確認した）。

    finding_code が無い場合は reason で代用する。厳しく拒否して
    HUMAN_REQUIRED にすると、Worker の書き方の揺れだけで run が止まる。
    検出が鈍るだけの方がましなので、そちらへ倒す。
    """
    if finding_code:
        signature = "|".join(
            (
                _normalize(finding_code),
                _normalize(finding_subject),
            )
        )
    else:
        signature = _normalize(reason)

    parts = (
        state.value,
        substate.value if substate is not None else "-",
        phase.value if phase is not None else "-",
        event.value,
        signature,
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _normalize(value: str | None) -> str:
    return (value or "").strip().upper()


def describe_finding(
    event: Event,
    state: State,
    substate: DocumentStage | None,
    phase: Phase | None,
    finding_code: str | None,
    finding_subject: str | None,
) -> str:
    """歯止めの理由に書く「何が繰り返されたか」。"""
    where = "/".join(
        part
        for part in (
            state.value,
            substate.value if substate is not None else None,
            phase.value if phase is not None else None,
        )
        if part is not None
    )
    if finding_code:
        subject = f" on {finding_subject}" if finding_subject else ""
        return f"{finding_code}{subject} ({event.value} at {where})"
    if finding_subject:
        # code は無いが対象は分かる場合（同じ質問の繰り返しなど）。
        return f"{event.value} at {where}: {finding_subject}"
    return f"{event.value} at {where}"


class NoProgressTracker:
    """同じ失敗の繰り返しを見張る（指示書 §11）。

    Worker を替えても同じ失敗が出るなら、それは Worker の調子ではなく
    仕様か設計の問題なので、回数を待たずに人間へ渡す。
    """

    def __init__(self, store: Store, limits: GuardLimits | None = None) -> None:
        self.store = store
        self.limits = limits if limits is not None else GuardLimits()

    def observe(
        self,
        run: RunState,
        event: Event,
        state: State,
        substate: DocumentStage | None,
        phase: Phase | None,
        reason: str | None,
        worker: Worker | None,
        finding_code: str | None = None,
        finding_subject: str | None = None,
    ) -> GuardVerdict | None:
        if event not in TRACKED_EVENTS:
            return None

        fingerprint = failure_fingerprint(
            state, substate, phase, event, reason, finding_code, finding_subject
        )
        # 人間は指摘を出さないので、「Worker を替えても同じ失敗」の判定に混ぜない。
        tracked_worker = (
            worker.value if worker is not None and worker != Worker.HUMAN else None
        )
        occurrences, workers = self.store.observe_fingerprint(
            run.run_id, fingerprint, worker=tracked_worker, reason=reason
        )

        # どの失敗が繰り返されたのかログから分かるようにする。歯止めの行は
        # 遷移した後に記録されるので、位置を書かないと元の失敗を辿れない。
        what = describe_finding(
            event, state, substate, phase, finding_code, finding_subject
        )

        if len(workers) > 1 and occurrences > 1:
            return GuardVerdict(
                event=Event.NO_PROGRESS,
                reason=(
                    f"{what} unchanged after worker switch "
                    f"({', '.join(sorted(workers))})"
                ),
            )

        if occurrences > self.limits.max_same_fingerprint:
            return GuardVerdict(
                event=Event.NO_PROGRESS,
                reason=(
                    f"{what} repeated {occurrences} times "
                    f"> {self.limits.max_same_fingerprint}"
                ),
            )

        return None


class LoopGuard:
    """回数の上限と NO_PROGRESS をまとめて見る。

    Controller の各 driver（Main Graph の node、Document Stage の node、
    DESIGN のループ）が遷移を記録した直後にこれを呼ぶ。
    """

    def __init__(self, store: Store, limits: GuardLimits | None = None) -> None:
        self.limits = limits if limits is not None else GuardLimits()
        self.no_progress = NoProgressTracker(store, self.limits)

    def check(
        self,
        run: RunState,
        event: Event,
        state: State,
        substate: DocumentStage | None = None,
        phase: Phase | None = None,
        reason: str | None = None,
        worker: Worker | None = None,
        finding_code: str | None = None,
        finding_subject: str | None = None,
    ) -> GuardVerdict | None:
        """歯止めが働くなら発火すべき Event を返す。

        Worker を替えても同じ失敗、が最優先。あとは回数の上限を見る。
        """
        verdict = self.no_progress.observe(
            run, event, state, substate, phase, reason, worker,
            finding_code, finding_subject,
        )
        if verdict is not None:
            return verdict
        return check_counters(run, self.limits)


def apply_guard(
    logger: TransitionLogger,
    guard: LoopGuard | None,
    run: RunState,
    event: Event,
    state: State,
    substate: DocumentStage | None = None,
    phase: Phase | None = None,
    reason: str | None = None,
    worker: Worker | None = None,
    finding_code: str | None = None,
    finding_subject: str | None = None,
) -> GuardVerdict | None:
    """遷移を記録した直後に呼ぶ。歯止めが働いたら、それも 1 遷移として記録する。

    「起きたこと」と「歯止めが働いたこと」を別の行にするので、ログを読むと
    どこまで正常に進み、どこで機械的に止められたかが分かる。

    すでに run が停止 State に入っている場合は何もしない。
    その Event を受け付けない State に無理やり遷移させないため。
    """
    if guard is None:
        return None

    verdict = guard.check(
        run, event, state, substate, phase, reason, worker,
        finding_code, finding_subject,
    )
    if verdict is None:
        return None
    if (run.current_state, verdict.event) not in TRANSITIONS:
        return None

    logger.record(
        run,
        verdict.event,
        to_substate=run.substate,
        reason=verdict.reason,
    )
    return verdict
