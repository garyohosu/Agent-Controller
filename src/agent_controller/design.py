"""DESIGN の Progressive Refinement（指示書 §17-7）。

共通 DocumentStage Subgraph を DOCUMENT_STAGE_ORDER の順に回す。

```text
SPEC → USECASE → SEQUENCE → CLASS → UI → TESTCASE
```

これは単なる文書生成の連結ではない。各工程で上位成果物をより細かい粒度へ
再設計し、実装前に上位仕様の曖昧さ・矛盾・不足を出すための段階的詳細化である（§2）。

保つべき性質は 2 つ。

- stage の中は局所処理。トップレベル State は DESIGN から動かない。
- stage を抜ける時だけ上位へ Event を返す。

ここは LangGraph ではなく素の Python ループにしている。やっていることは
「次にどの stage を回すか」の選択だけで、graph にするほどの分岐が無いため。
stage 単位の移動も必ず logger を通すので、遷移ログからは同じように追える。
"""

from __future__ import annotations

from pathlib import Path

from agent_controller.document_stage import (
    ESCALATING_EVENTS,
    DocumentStageConfig,
    PhaseHandler,
    run_document_stage,
    stage_completed,
)
from agent_controller.guards import LoopGuard, check_counters
from agent_controller.impact import (
    ImpactAnalyzer,
    default_impact_analyzer,
    merge_impacts,
    validate_impact_result,
)
from agent_controller.models import (
    DEFAULT_REVIEW_LEVELS,
    DOCUMENT_STAGE_ORDER,
    ArtifactKind,
    ArtifactState,
    ArtifactStatus,
    DocumentStage,
    Event,
    Phase,
    RunState,
    State,
)
from agent_controller.transition_log import TransitionLogger

STAGE_OUTPUTS: dict[DocumentStage, str] = {
    DocumentStage.SPEC: "SPEC.md",
    DocumentStage.USECASE: "USECASE.md",
    DocumentStage.SEQUENCE: "SEQUENCE.md",
    DocumentStage.CLASS: "CLASS.md",
    DocumentStage.UI: "UI.md",
    DocumentStage.TESTCASE: "TESTCASE.md",
}

STAGE_INPUTS: dict[DocumentStage, list[str]] = {
    DocumentStage.SPEC: [],
    DocumentStage.USECASE: ["SPEC.md"],
    DocumentStage.SEQUENCE: ["USECASE.md", "SPEC.md"],
    DocumentStage.CLASS: ["SEQUENCE.md", "SPEC.md"],
    DocumentStage.UI: ["USECASE.md", "SPEC.md"],
    DocumentStage.TESTCASE: ["USECASE.md", "CLASS.md", "SPEC.md"],
}


def artifact_kind_for(stage: DocumentStage) -> ArtifactKind:
    """Document Stage に対応する成果物。名前は 1 対 1 で対応している。"""
    return ArtifactKind(stage.value)


def default_design_stages(include_ui: bool = False) -> list[DocumentStageConfig]:
    """設計工程の既定の並び。

    review_level は §5 の初期値（SPEC / USECASE のみ DEEP）を当てる。
    UI は §2 が「必要な場合」としているため既定では入れない。
    """
    return [
        DocumentStageConfig(
            name=stage,
            inputs=STAGE_INPUTS[stage],
            output=STAGE_OUTPUTS[stage],
            review_level=DEFAULT_REVIEW_LEVELS[stage],
        )
        for stage in DOCUMENT_STAGE_ORDER
        if include_ui or stage != DocumentStage.UI
    ]


def _artifact_status(
    store_statuses: dict[ArtifactKind, ArtifactState], stage: DocumentStage
) -> ArtifactStatus:
    """未生成の成果物は STALE として扱う。"""
    artifact = store_statuses.get(artifact_kind_for(stage))
    return artifact.status if artifact is not None else ArtifactStatus.STALE


def _mark(
    logger: TransitionLogger,
    run: RunState,
    stage: DocumentStage,
    status: ArtifactStatus,
    reason: str | None = None,
) -> None:
    logger.store.save_artifact(
        ArtifactState(
            run_id=run.run_id,
            kind=artifact_kind_for(stage),
            status=status,
            path=STAGE_OUTPUTS[stage],
            reason=reason,
        )
    )


def invalidate_from(
    logger: TransitionLogger,
    run: RunState,
    stages: list[DocumentStageConfig],
    target: DocumentStage,
    reason: str | None = None,
) -> None:
    """target とその下流を STALE にする。

    影響範囲分析（impact.py）を使わずに直接無効化したいときの入口。
    通常の手戻りは run_design が analyzer を通す。
    """
    names = [config.name for config in stages]
    if target not in names:
        raise ValueError(f"{target.value} is not part of this design plan")

    for stage in names[names.index(target) :]:
        _mark(logger, run, stage, ArtifactStatus.STALE, reason)


def _entry_for(
    statuses: dict[ArtifactKind, ArtifactState],
    config: DocumentStageConfig,
    workspace: str | Path | None,
) -> Phase:
    """その工程にどこから入るかを決める。

    - REVIEW_REQUIRED  影響の可能性あり。軽量レビューのみ（指示書 §6）
    - STALE            影響あり。生成からやり直す
    - 未処理だが文書がある  生成せず、その工程の強度でレビューから入る
    - 未処理で文書も無い    生成から

    3 つ目が要点。人が書いた SPEC.md を Controller が上書きしてはいけない。
    最上流こそ無検査で通さず、まず読ませる。

    「未処理」の判定には artifact 行の有無を使う。行があるということは、
    この run で Controller が既にその工程を触ったということ。だから影響範囲分析が
    STALE を書いた工程は、文書が残っていても再生成になる。
    """
    artifact = statuses.get(artifact_kind_for(config.name))

    if artifact is not None:
        if artifact.status == ArtifactStatus.REVIEW_REQUIRED:
            return Phase.REVIEW_LIGHT
        return Phase.GENERATE

    if workspace is not None and (Path(workspace) / config.output).exists():
        return config.review_level.phase

    return Phase.GENERATE


class DesignProgressError(RuntimeError):
    """DESIGN のループが進まなくなった。"""


def run_design(
    run: RunState,
    logger: TransitionLogger,
    stages: list[DocumentStageConfig] | None = None,
    handlers: dict[Phase, PhaseHandler] | None = None,
    guard: LoopGuard | None = None,
    analyzer: ImpactAnalyzer | None = None,
    workspace: str | Path | None = None,
) -> RunState:
    """すべての設計成果物が VALID になるまで stage を回す。

    全部通れば DESIGN + PASS を出して IMPLEMENT へ渡す。
    途中で run が DESIGN の外へ出た場合（HUMAN_REQUIRED / WAIT_RESOURCE / ABORT）は
    そこで返す。呼び出し側が Worker を替えて run_design をもう一度呼べば続きから進む。

    上位手戻りの回数は guard が見る（GuardLimits.max_upstream_rework）。
    guard を渡さなければ既定値の LoopGuard を使う。歯止め無しでは回さない。

    workspace を渡すと、既に存在する文書は生成せずレビューから入る。
    渡さなければ全工程を生成から始める。
    """
    stages = stages if stages is not None else default_design_stages()
    guard = guard if guard is not None else LoopGuard(logger.store)
    analyzer = analyzer if analyzer is not None else default_impact_analyzer
    entry_reason: str | None = None

    while True:
        statuses = logger.store.artifacts(run.run_id)
        pending = next(
            (
                config
                for config in stages
                if _artifact_status(statuses, config.name) != ArtifactStatus.VALID
            ),
            None,
        )

        if pending is None:
            logger.record(run, Event.PASS, reason="all design artifacts valid")
            return run

        run = run_document_stage(
            run,
            pending,
            logger,
            handlers,
            guard,
            entry_phase=_entry_for(statuses, pending, workspace),
            entry_reason=entry_reason,
        )
        entry_reason = None

        if run.current_state != State.DESIGN:
            # HUMAN_REQUIRED / WAIT_RESOURCE / ABORT。stage の途中で止まっている。
            return run

        if stage_completed(run, pending):
            _mark(logger, run, pending.name, ArtifactStatus.VALID)
            continue

        if run.last_event in ESCALATING_EVENTS:
            target = run.pending_upstream_stage
            if target is None:
                raise DesignProgressError(
                    f"{pending.name.value} escalated without naming an upstream stage"
                )

            run.upstream_rework += 1
            verdict = check_counters(run, guard.limits)
            if verdict is not None:
                logger.record(
                    run,
                    verdict.event,
                    to_substate=run.substate,
                    reason=verdict.reason,
                )
                return run

            # AI が影響範囲を提案し、Controller は依存グラフの制約に照らして検証する。
            # 通れば適用し、破っていれば適用せず人間へ渡す。
            proposal = analyzer(run, target, stages, statuses)
            violations = validate_impact_result(proposal, stages, expected_cause=target)
            if violations:
                logger.record(
                    run,
                    Event.INVALID_IMPACT_RESULT,
                    to_substate=run.substate,
                    reason="; ".join(violations),
                )
                return run

            merged = merge_impacts(
                design_artifact_statuses(logger, run, stages), proposal, stages
            )
            for stage, status in merged.items():
                _mark(logger, run, stage, status, proposal.reason)

            # §6「影響分析結果と再開理由は必ずログへ残す」。
            # 次の工程に入る行の理由として出す。
            entry_reason = f"impact: {proposal.summary()} | {proposal.reason}"
            run.pending_upstream_stage = None
            logger.store.save_run(run)
            continue

        raise DesignProgressError(
            f"{pending.name.value} left phase {run.phase} on {run.last_event}"
        )


def design_artifact_statuses(
    logger: TransitionLogger,
    run: RunState,
    stages: list[DocumentStageConfig] | None = None,
) -> dict[DocumentStage, ArtifactStatus]:
    """各設計工程の現在の成果物状態。人間向けの確認用。"""
    stages = stages if stages is not None else default_design_stages()
    statuses = logger.store.artifacts(run.run_id)
    return {config.name: _artifact_status(statuses, config.name) for config in stages}
