"""Task Complexity Router / Design Fast Path（指示書 2026-08-17-018 §3）。

全タスクを同じ SPEC -> USECASE -> SEQUENCE -> CLASS -> UI -> TESTCASE へ通さない。
ここは Fast Path を **文書を削る機能** としてだけ扱う。TEST / REVIEW / CompleteGate は
どの TaskType でも一切スキップしない（§3.3 / §10 の禁止事項）。

task_type が None（未分類）の run は、この router に一切触れずこれまで通り
default_design_stages() のフル経路を通る。既存呼び出し側の挙動を変えないための
既定であって、「不明なので安全側」という意味の判断はここではしない
（不明時の既定は classify_task_type 側で NEW_PRODUCT に倒す）。
"""

from __future__ import annotations

import re

from agent_controller.models import DocumentStage, TaskType

FAST_PATH_STAGES: dict[TaskType, tuple[DocumentStage, ...]] = {
    TaskType.NEW_PRODUCT: (
        DocumentStage.SPEC,
        DocumentStage.USECASE,
        DocumentStage.SEQUENCE,
        DocumentStage.CLASS,
        DocumentStage.TESTCASE,
    ),
    TaskType.NEW_FEATURE: (
        DocumentStage.SPEC,
        DocumentStage.USECASE,
        DocumentStage.TESTCASE,
    ),
    TaskType.SMALL_FEATURE: (
        DocumentStage.SPEC,
        DocumentStage.TESTCASE,
    ),
    TaskType.BUG_FIX: (
        DocumentStage.SPEC,
        DocumentStage.TESTCASE,
    ),
    TaskType.REFACTOR: (
        DocumentStage.SPEC,
        DocumentStage.TESTCASE,
    ),
    TaskType.DOC_ONLY: (
        DocumentStage.SPEC,
    ),
}
"""指示書 §3.2 の推奨経路。NEW_PRODUCT は UI を除く既存の既定経路と同じ
（UI は「UI が存在しない製品では必須にしない」設計を維持し、必要な run は
呼び出し側が include_ui=True で足す）。

NEW_FEATURE の SEQUENCE / CLASS / UI は「影響範囲に応じて必要時のみ」とあるが、
Controller は決定論的でなければならないため、この Fast Path は最小の必須集合を
固定で定義する。追加で必要になった工程は、通常の UPSTREAM_CHANGE_REQUIRED /
影響範囲分析の経路でいつでも STALE として足し直せる（安全側の追加は封じていない）。
"""


def stages_for_task_type(task_type: TaskType) -> tuple[DocumentStage, ...]:
    return FAST_PATH_STAGES[task_type]


def skipped_stages_for_task_type(task_type: TaskType) -> tuple[DocumentStage, ...]:
    """Fast Path が省略する DocumentStage。フル経路との差分。"""
    included = set(FAST_PATH_STAGES[task_type])
    return tuple(
        stage for stage in FAST_PATH_STAGES[TaskType.NEW_PRODUCT] if stage not in included
    ) + ((DocumentStage.UI,) if DocumentStage.UI not in included else ())


_NEW_PRODUCT_HINTS = re.compile(
    r"\bnew (product|system|application|service)\b|新規(製品|システム|プロダクト)|ゼロから",
    re.IGNORECASE,
)
_BUG_FIX_HINTS = re.compile(
    r"\bfix(es|ed)?\b|\bbug\b|\bregression\b|\bcrash(es)?\b|不具合|バグ|直し|修正",
    re.IGNORECASE,
)
_REFACTOR_HINTS = re.compile(
    r"\brefactor(ing)?\b|\bclean ?up\b|リファクタ|整理",
    re.IGNORECASE,
)
_DOC_ONLY_HINTS = re.compile(
    r"\breadme\b|\bdocs?\b|\bdocumentation\b|ドキュメント|README",
    re.IGNORECASE,
)
_SMALL_FEATURE_HINTS = re.compile(
    r"\bsmall\b|\bminor\b|\btweak\b|\badd (a|an|the)? ?(flag|option|field|parameter)\b|"
    r"軽微|小規模|ちょっとした",
    re.IGNORECASE,
)
_NEW_FEATURE_HINTS = re.compile(
    r"\b(add|introduce|implement) (a |an )?(new )?feature\b|新機能|機能追加",
    re.IGNORECASE,
)


def classify_task_type(request: str) -> TaskType:
    """自由文の初期要求から保守的に既定分類する（指示書 §3 / §3.3）。

    これは「一般的な保守方針」で安全に決められる場合の既定分類器であって、
    製品判断そのものを推測するものではない。判定できない、または新規製品らしい
    手がかりがあれば NEW_PRODUCT（= フルの Progressive Refinement）へ倒す。
    人間や Director が明示的に task_type を与えた場合はこの関数を経由しない。
    """
    text = request or ""
    if _NEW_PRODUCT_HINTS.search(text):
        return TaskType.NEW_PRODUCT
    if _DOC_ONLY_HINTS.search(text) and not _NEW_FEATURE_HINTS.search(text):
        return TaskType.DOC_ONLY
    if _BUG_FIX_HINTS.search(text):
        return TaskType.BUG_FIX
    if _REFACTOR_HINTS.search(text):
        return TaskType.REFACTOR
    if _SMALL_FEATURE_HINTS.search(text):
        return TaskType.SMALL_FEATURE
    if _NEW_FEATURE_HINTS.search(text):
        return TaskType.NEW_FEATURE
    return TaskType.NEW_PRODUCT
