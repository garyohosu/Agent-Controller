"""影響範囲分析（指示書 §6 / §17-10）。

上位成果物が変わったとき、下流を機械的に全部作り直さない。
どこにどれだけ影響するかは AI（Director / Reviewer / Analyzer）が判断し、
Controller はその結果を**検証して適用するだけ**にする。

```text
Reviewer / Director / Analyzer
        ↓
   ImpactResult      ← 自由文ではなく構造化された提案
        ↓
Controller が妥当性検証   ← 依存グラフの制約を破らせない
        ↓
Artifact Status 更新
        ↓
必要な DocumentStage だけ再実行
```

Controller が扱うのは 3 値だけで、意味の解釈はしない。

```text
STALE            再生成必須
REVIEW_REQUIRED  軽量レビューして問題なければ VALID
VALID            何もしない
```
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field

from agent_controller.document_stage import DocumentStageConfig
from agent_controller.models import ArtifactStatus, DocumentStage, RunState


class ImpactResult(BaseModel):
    """AI が返す影響範囲の提案（指示書 §6）。

    自由文ではなくこの形で受け取る。Controller は文章を解釈しない。
    """

    cause_stage: DocumentStage
    """変更が要求された工程。この工程自身は必ず STALE になる。"""

    impacts: dict[DocumentStage, ArtifactStatus] = Field(default_factory=dict)
    """設計計画に含まれる全工程の判定。過不足があれば受け付けない。"""

    reason: str
    """なぜその影響範囲になるのか。ログに残す。"""

    def summary(self) -> str:
        """ログ 1 行に収まる形。指示書 §6 が求める「影響分析結果を必ずログへ残す」用。"""
        return " ".join(
            f"{stage.value}={status.value}" for stage, status in self.impacts.items()
        )


ImpactAnalyzer = Callable[
    [RunState, DocumentStage, list[DocumentStageConfig], dict[DocumentStage, ArtifactStatus]],
    ImpactResult,
]
"""影響範囲を提案する関数。§17-11 でここに実 AI Worker が入る。

現在の成果物状態も渡す。AI が「もう STALE なもの」を踏まえて判断できるようにするため。
"""


def validate_impact_result(
    result: ImpactResult,
    stages: list[DocumentStageConfig],
    expected_cause: DocumentStage | None = None,
) -> list[str]:
    """依存グラフの制約に照らして提案を検証する。違反の一覧を返す。

    AI は影響範囲を提案できるが、依存関係として矛盾する結果は通さない。
    たとえば SPEC が STALE なのに、その下流の USECASE が VALID というのは成立しない。

    純関数。store も run も要らない。
    """
    order = [config.name for config in stages]
    planned = set(order)
    violations: list[str] = []

    if result.cause_stage not in planned:
        violations.append(
            f"cause_stage {result.cause_stage.value} is not part of the design plan"
        )

    if expected_cause is not None and result.cause_stage != expected_cause:
        violations.append(
            f"cause_stage {result.cause_stage.value} does not match the "
            f"escalated stage {expected_cause.value}"
        )

    proposed = set(result.impacts)
    for stage in sorted(proposed - planned, key=lambda item: item.value):
        violations.append(f"{stage.value} is not part of the design plan")
    for stage in sorted(planned - proposed, key=lambda item: item.value):
        violations.append(f"{stage.value} has no impact decision")

    if result.impacts.get(result.cause_stage) not in (None, ArtifactStatus.STALE):
        # 変更が必要だという上流の判断を、分析側が黙って取り下げることはできない。
        violations.append(
            f"cause_stage {result.cause_stage.value} must be STALE, got "
            f"{result.impacts[result.cause_stage].value}"
        )

    for index, stage in enumerate(order):
        if result.impacts.get(stage) != ArtifactStatus.STALE:
            continue
        for downstream in order[index + 1 :]:
            if result.impacts.get(downstream) == ArtifactStatus.VALID:
                violations.append(
                    f"{downstream.value} cannot be VALID while its upstream "
                    f"{stage.value} is STALE"
                )

    return violations


def merge_impacts(
    current: dict[DocumentStage, ArtifactStatus],
    result: ImpactResult,
    stages: list[DocumentStageConfig],
) -> dict[DocumentStage, ArtifactStatus]:
    """提案を現在の状態へ統合する。**深刻な方を採る。**

    分析結果が成果物の状態を良くすることはない。VALID は「新たな影響は無い」の意味で
    あって「もう出来ている」ではない。まだ生成もしていない TESTCASE に AI が VALID を
    返したからといって、それを通過扱いにはしない。
    VALID にできるのは、その工程が実際に通ったときだけ。
    """
    merged: dict[DocumentStage, ArtifactStatus] = {}
    for config in stages:
        now = current.get(config.name, ArtifactStatus.STALE)
        proposed = result.impacts.get(config.name, ArtifactStatus.VALID)
        merged[config.name] = now if now.severity >= proposed.severity else proposed
    return merged


def default_impact_analyzer(
    run: RunState,
    cause_stage: DocumentStage,
    stages: list[DocumentStageConfig],
    current: dict[DocumentStage, ArtifactStatus],
) -> ImpactResult:
    """AI を使わない既定の分析。

    「上流が変われば下流は全部やり直し」という最も粗い規則。
    §17-10 以前の振る舞いをそのまま再現する。実 AI Analyzer を差し込むときは
    この関数を置き換える。
    """
    order = [config.name for config in stages]
    index = order.index(cause_stage)
    return ImpactResult(
        cause_stage=cause_stage,
        impacts={
            stage: (ArtifactStatus.STALE if position >= index else ArtifactStatus.VALID)
            for position, stage in enumerate(order)
        },
        reason=f"conservative default: {cause_stage.value} and everything downstream",
    )
