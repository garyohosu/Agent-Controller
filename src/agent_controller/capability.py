"""Worker Capability Profile（指示書 2026-08-17-018 §5）。

Role 名だけで「書けるはず」を仮定しない。成果物生成（GENERATE / FIX）は
can_write=True の候補だけに絞り、Reviewer は原則 read-only のまま広げない。

今回は静的な Capability と、この run 内で観測した失敗の回数だけで十分とする
（学習型ルーターは範囲外）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from agent_controller.models import Event, Phase, Worker


class WorkerCapability(BaseModel):
    """Worker 1 件の静的な能力宣言。"""

    worker: Worker
    can_read: bool = True
    can_write: bool = True
    can_review: bool = True
    can_implement: bool = True
    can_answer: bool = True
    supports_plan_mode: bool = False
    supports_structured_output: bool = True


DEFAULT_CAPABILITIES: dict[Worker, WorkerCapability] = {
    Worker.CLAUDE_CODE: WorkerCapability(
        worker=Worker.CLAUDE_CODE, supports_plan_mode=True,
    ),
    Worker.CODEX_CLI: WorkerCapability(worker=Worker.CODEX_CLI),
    Worker.GROK: WorkerCapability(worker=Worker.GROK),
    Worker.ANTIGRAVITY: WorkerCapability(worker=Worker.ANTIGRAVITY),
    Worker.HUMAN: WorkerCapability(
        worker=Worker.HUMAN,
        can_read=False, can_write=False, can_review=False,
        can_implement=False, can_answer=True,
        supports_structured_output=False,
    ),
}
"""既定の Capability。すべて明示的な宣言であり、実測とは別に静的な既定値として持つ。

CLAUDE_CODE / CODEX_CLI / GROK / ANTIGRAVITY はいずれも汎用 CLI エージェントで
read/write/review/implement/answer をすべて行えるため既定値のまま。
Reviewer 用に read-only な Worker を追加する場合は、ここへ can_write=False の
プロファイルを足す（既定値を書き換えない）。
"""


def capability_for(worker: Worker, profiles: dict[Worker, WorkerCapability] | None = None) -> WorkerCapability:
    """未登録の Worker には何も仮定しない、最も保守的な Capability を返す。"""
    table = profiles if profiles is not None else DEFAULT_CAPABILITIES
    return table.get(worker, WorkerCapability(
        worker=worker, can_read=False, can_write=False, can_review=False,
        can_implement=False, can_answer=False, supports_structured_output=False,
    ))


WRITE_REQUIRED_PHASES: frozenset[Phase] = frozenset({Phase.GENERATE, Phase.FIX})
"""成果物を書く phase。ここへの候補は can_write=True でなければならない。"""

READ_ONLY_PHASES: frozenset[Phase] = frozenset({Phase.REVIEW_LIGHT, Phase.REVIEW_DEEP})
"""レビューは既定で read-only のまま。can_read=True であればよい。"""


def required_capability_flag(phase: Phase) -> str | None:
    """その phase の候補に最低限必要な Capability フィールド名。無ければ None。"""
    if phase in WRITE_REQUIRED_PHASES:
        return "can_write"
    if phase in READ_ONLY_PHASES:
        return "can_read"
    if phase is Phase.QANDA:
        return "can_answer"
    return None


def satisfies(capability: WorkerCapability, phase: Phase) -> bool:
    flag = required_capability_flag(phase)
    if flag is None:
        return True
    return bool(getattr(capability, flag))


# --- error classification -------------------------------------------------

_WRITE_DENIED_PATTERNS: tuple[str, ...] = (
    "write permission",
    "permission denied",
    "writes were denied",
    "could not be updated",
    "write blocked",
    "write-blocked",
    "filesystem writes were denied",
    "eacces",
    "read-only file system",
    "access is denied",
    "書き込み",
    "書込み",
    "書けません",
    "拒否",
    "禁止されて",
    "権限がありません",
)
"""指示書 018 §1.3 の実測（needs-detector, WRITE_PERMISSION_DENIED）に基づく手がかり。

CLI ごとの終了コードが安定して分かるまでは文字列一致に頼る。外したら
「本来 fallback すべきなのに WORKER_ERROR のまま人間へ渡る」だけなので安全側に倒れる。
"""

_READ_DENIED_PATTERNS: tuple[str, ...] = (
    "read permission",
    "cannot read",
    "could not be read",
    "access denied",
    "読み取れません",
    "読み込み拒否",
)

_TIMEOUT_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def classify_worker_error(reason: str | None, raw_output: str = "") -> str | None:
    """WORKER_ERROR の reason/raw_output から機械判定可能な finding_code を割り当てる。

    None を返す場合は既存の未分類 WORKER_ERROR のまま（後方互換）。
    分類できたコードは §4.2 の Auto-Recovery 規則が参照する。
    """
    text = " ".join(part for part in (reason or "", raw_output) if part)
    if not text.strip():
        return None
    if _matches(text, _TIMEOUT_PATTERNS):
        return "TIMEOUT"
    if _matches(text, _WRITE_DENIED_PATTERNS):
        return "WRITE_PERMISSION_DENIED"
    if _matches(text, _READ_DENIED_PATTERNS):
        return "READ_PERMISSION_DENIED"
    return None


_STRIP_TAGS = re.compile(r"\s+")


def shorten_directive(directive: str, limit: int = 800) -> str:
    """TIMEOUT 再試行時の短縮版指示（§4.2 の「短縮版があれば使う」用の最小実装）。

    意味を変えずに空白を畳み、上限で切る。専用の短縮契約を持つ工程を
    増やすまでの、常に使える既定の縮め方。
    """
    collapsed = _STRIP_TAGS.sub(" ", directive).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
