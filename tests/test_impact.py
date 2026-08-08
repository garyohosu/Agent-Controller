"""影響範囲分析のテスト（指示書 §6 / §17-10）。

要点は「AI は影響範囲を提案できるが、依存グラフの制約は破れない」。
"""

from __future__ import annotations

from datetime import timezone

import pytest

from agent_controller.design import (
    artifact_kind_for,
    default_design_stages,
    design_artifact_statuses,
    run_design,
)
from agent_controller.document_stage import ScriptedPhaseHandlers, StageResult
from agent_controller.impact import (
    ImpactResult,
    default_impact_analyzer,
    merge_impacts,
    validate_impact_result,
)
from agent_controller.models import (
    ArtifactState,
    ArtifactStatus,
    DocumentStage,
    Event,
    Phase,
    RunState,
    RunStatus,
    State,
)
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

UTC = timezone.utc

FULL_PLAN = default_design_stages(include_ui=True)

# チャットで示された例をそのまま使う。
EXAMPLE = ImpactResult(
    cause_stage=DocumentStage.CLASS,
    impacts={
        DocumentStage.SPEC: ArtifactStatus.VALID,
        DocumentStage.USECASE: ArtifactStatus.VALID,
        DocumentStage.SEQUENCE: ArtifactStatus.REVIEW_REQUIRED,
        DocumentStage.CLASS: ArtifactStatus.STALE,
        DocumentStage.UI: ArtifactStatus.REVIEW_REQUIRED,
        DocumentStage.TESTCASE: ArtifactStatus.STALE,
    },
    reason="responsibility change may affect sequence and UI flow",
)


@pytest.fixture
def design_run(store: Store) -> RunState:
    run = RunState(
        project_id="agent-controller",
        run_id="run-impact",
        current_state=State.DESIGN,
        checkpoint_commit="a86c154",
    )
    store.save_run(run)
    return run


def starts(logger: TransitionLogger, run_id: str) -> list[tuple[DocumentStage, Phase]]:
    """各 stage にどの phase から入ったか。"""
    return [
        (item.to_substate, item.to_phase)
        for item in logger.history(run_id)
        if item.event == Event.START and item.to_substate is not None
    ]


class TestValidation:
    def test_the_example_is_accepted(self) -> None:
        assert validate_impact_result(EXAMPLE, FULL_PLAN, DocumentStage.CLASS) == []

    def test_stale_upstream_with_valid_downstream_is_rejected(self) -> None:
        """SPEC が STALE なのに USECASE が VALID は依存関係として成立しない。"""
        broken = ImpactResult(
            cause_stage=DocumentStage.SPEC,
            impacts={
                DocumentStage.SPEC: ArtifactStatus.STALE,
                DocumentStage.USECASE: ArtifactStatus.VALID,
                DocumentStage.SEQUENCE: ArtifactStatus.VALID,
                DocumentStage.CLASS: ArtifactStatus.VALID,
                DocumentStage.UI: ArtifactStatus.VALID,
                DocumentStage.TESTCASE: ArtifactStatus.VALID,
            },
            reason="only the spec wording changed",
        )

        violations = validate_impact_result(broken, FULL_PLAN, DocumentStage.SPEC)
        assert violations
        assert any("USECASE cannot be VALID" in item for item in violations)
        assert len(violations) == 5  # SPEC の下流すべて

    def test_review_required_downstream_of_stale_is_allowed(self) -> None:
        """REVIEW_REQUIRED なら成立する。全部作り直せとは言っていない。"""
        result = ImpactResult(
            cause_stage=DocumentStage.SEQUENCE,
            impacts={
                DocumentStage.SPEC: ArtifactStatus.VALID,
                DocumentStage.USECASE: ArtifactStatus.VALID,
                DocumentStage.SEQUENCE: ArtifactStatus.STALE,
                DocumentStage.CLASS: ArtifactStatus.REVIEW_REQUIRED,
                DocumentStage.UI: ArtifactStatus.REVIEW_REQUIRED,
                DocumentStage.TESTCASE: ArtifactStatus.REVIEW_REQUIRED,
            },
            reason="ordering only",
        )
        assert validate_impact_result(result, FULL_PLAN, DocumentStage.SEQUENCE) == []

    def test_review_required_upstream_constrains_nothing(self) -> None:
        """REVIEW_REQUIRED は「見れば分かる」であって、下流を巻き込まない。"""
        assert EXAMPLE.impacts[DocumentStage.SEQUENCE] == ArtifactStatus.REVIEW_REQUIRED
        assert EXAMPLE.impacts[DocumentStage.CLASS] == ArtifactStatus.STALE
        assert validate_impact_result(EXAMPLE, FULL_PLAN, DocumentStage.CLASS) == []

    def test_cause_stage_must_be_stale(self) -> None:
        """変更が必要だという上流の判断を、分析側が黙って取り下げられない。"""
        softened = EXAMPLE.model_copy(
            update={
                "impacts": {
                    **EXAMPLE.impacts,
                    DocumentStage.CLASS: ArtifactStatus.REVIEW_REQUIRED,
                }
            }
        )
        violations = validate_impact_result(softened, FULL_PLAN, DocumentStage.CLASS)
        assert any("must be STALE" in item for item in violations)

    def test_cause_stage_must_match_the_escalation(self) -> None:
        violations = validate_impact_result(EXAMPLE, FULL_PLAN, DocumentStage.SEQUENCE)
        assert any("does not match the escalated stage" in item for item in violations)

    def test_missing_decision_is_rejected(self) -> None:
        """判定漏れを VALID とみなして補完しない。"""
        partial = EXAMPLE.model_copy(
            update={
                "impacts": {
                    stage: status
                    for stage, status in EXAMPLE.impacts.items()
                    if stage != DocumentStage.UI
                }
            }
        )
        violations = validate_impact_result(partial, FULL_PLAN, DocumentStage.CLASS)
        assert any("UI has no impact decision" in item for item in violations)

    def test_stage_outside_the_plan_is_rejected(self) -> None:
        """UI を計画に入れていないのに UI の判定を返してきた場合。"""
        violations = validate_impact_result(EXAMPLE, default_design_stages(), DocumentStage.CLASS)
        assert any("UI is not part of the design plan" in item for item in violations)


class TestMerge:
    def test_never_upgrades_an_unfinished_artifact(self) -> None:
        """VALID は「新たな影響は無い」であって「もう出来ている」ではない。"""
        current = {
            DocumentStage.SPEC: ArtifactStatus.VALID,
            DocumentStage.USECASE: ArtifactStatus.VALID,
            DocumentStage.SEQUENCE: ArtifactStatus.VALID,
            DocumentStage.CLASS: ArtifactStatus.VALID,
            DocumentStage.UI: ArtifactStatus.VALID,
            DocumentStage.TESTCASE: ArtifactStatus.STALE,  # まだ生成していない
        }
        optimistic = EXAMPLE.model_copy(
            update={
                "impacts": {**EXAMPLE.impacts, DocumentStage.TESTCASE: ArtifactStatus.VALID}
            }
        )

        merged = merge_impacts(current, optimistic, FULL_PLAN)
        assert merged[DocumentStage.TESTCASE] == ArtifactStatus.STALE

    def test_takes_the_more_severe_side(self) -> None:
        current = {stage: ArtifactStatus.REVIEW_REQUIRED for stage in EXAMPLE.impacts}
        merged = merge_impacts(current, EXAMPLE, FULL_PLAN)

        assert merged[DocumentStage.SPEC] == ArtifactStatus.REVIEW_REQUIRED
        assert merged[DocumentStage.CLASS] == ArtifactStatus.STALE

    def test_unknown_current_state_counts_as_stale(self) -> None:
        merged = merge_impacts({}, EXAMPLE, FULL_PLAN)
        assert all(status == ArtifactStatus.STALE for status in merged.values())


class TestDefaultAnalyzer:
    def test_reproduces_the_conservative_rule(self) -> None:
        result = default_impact_analyzer(
            RunState(project_id="p", run_id="r"), DocumentStage.SEQUENCE, FULL_PLAN, {}
        )

        assert result.impacts == {
            DocumentStage.SPEC: ArtifactStatus.VALID,
            DocumentStage.USECASE: ArtifactStatus.VALID,
            DocumentStage.SEQUENCE: ArtifactStatus.STALE,
            DocumentStage.CLASS: ArtifactStatus.STALE,
            DocumentStage.UI: ArtifactStatus.STALE,
            DocumentStage.TESTCASE: ArtifactStatus.STALE,
        }

    def test_its_own_output_passes_validation(self) -> None:
        for config in FULL_PLAN:
            result = default_impact_analyzer(
                RunState(project_id="p", run_id="r"), config.name, FULL_PLAN, {}
            )
            assert validate_impact_result(result, FULL_PLAN, config.name) == []


class TestEndToEnd:
    def _handlers(self):
        """TESTCASE のレビューが CLASS の責務変更を指摘する。"""
        return ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    Event.PASS,  # SEQUENCE
                    Event.PASS,  # CLASS
                    Event.PASS,  # UI
                    StageResult(  # TESTCASE
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.CLASS,
                        reason="class responsibility changed",
                    ),
                    Event.PASS,  # SEQUENCE（レビューのみ）
                    Event.PASS,  # CLASS（再生成）
                    Event.PASS,  # UI（レビューのみ）
                    Event.PASS,  # TESTCASE（再生成）
                ],
            }
        ).as_handlers()

    def _analyzer(self):
        return lambda run, cause, stages, current: EXAMPLE

    def test_only_the_affected_stages_are_touched(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        final = run_design(
            design_run, logger, FULL_PLAN, self._handlers(), analyzer=self._analyzer()
        )

        assert final.current_state == State.IMPLEMENT
        assert starts(logger, final.run_id) == [
            (DocumentStage.SPEC, Phase.GENERATE),
            (DocumentStage.USECASE, Phase.GENERATE),
            (DocumentStage.SEQUENCE, Phase.GENERATE),
            (DocumentStage.CLASS, Phase.GENERATE),
            (DocumentStage.UI, Phase.GENERATE),
            (DocumentStage.TESTCASE, Phase.GENERATE),
            # 影響範囲分析のあと
            (DocumentStage.SEQUENCE, Phase.REVIEW_LIGHT),  # 軽量レビューのみ
            (DocumentStage.CLASS, Phase.GENERATE),  # 再生成
            (DocumentStage.UI, Phase.REVIEW_LIGHT),  # 軽量レビューのみ
            (DocumentStage.TESTCASE, Phase.GENERATE),  # 再生成
        ]

    def test_untouched_stages_are_never_re_entered(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(
            design_run, logger, FULL_PLAN, self._handlers(), analyzer=self._analyzer()
        )

        entered = [stage for stage, _ in starts(logger, design_run.run_id)]
        assert entered.count(DocumentStage.SPEC) == 1
        assert entered.count(DocumentStage.USECASE) == 1

    def test_review_only_stages_skip_generation(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(
            design_run, logger, FULL_PLAN, self._handlers(), analyzer=self._analyzer()
        )

        generate_rows = [
            item
            for item in logger.history(design_run.run_id)
            if item.phase == Phase.GENERATE and item.from_substate == DocumentStage.SEQUENCE
        ]
        # SEQUENCE は最初の 1 回しか生成していない。
        assert len(generate_rows) == 1

    def test_the_analysis_result_is_in_the_log(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§6「影響分析結果と再開理由は必ずログへ残す」。"""
        run_design(
            design_run, logger, FULL_PLAN, self._handlers(), analyzer=self._analyzer()
        )

        rendered = logger.render(design_run.run_id, UTC)
        assert "SEQUENCE=REVIEW_REQUIRED" in rendered
        assert "CLASS=STALE" in rendered
        assert "responsibility change may affect sequence and UI flow" in rendered
        assert "-> DESIGN/SEQUENCE/REVIEW_LIGHT" in rendered

    def test_all_artifacts_end_valid(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        run_design(
            design_run, logger, FULL_PLAN, self._handlers(), analyzer=self._analyzer()
        )

        statuses = design_artifact_statuses(logger, design_run, FULL_PLAN)
        assert all(status == ArtifactStatus.VALID for status in statuses.values())


class TestRejection:
    def test_an_inconsistent_analysis_stops_the_run(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """依存関係を破る提案は適用せず、人間へ渡す。"""

        def bad_analyzer(run, cause, stages, current):
            return ImpactResult(
                cause_stage=cause,
                impacts={
                    config.name: (
                        ArtifactStatus.STALE
                        if config.name == cause
                        else ArtifactStatus.VALID
                    )
                    for config in stages
                },
                reason="nothing else is affected",
            )

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [Event.PASS],
                Phase.REVIEW_LIGHT: [
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SPEC,
                        reason="spec is wrong",
                    )
                ],
            }
        ).as_handlers()

        final = run_design(
            design_run, logger, default_design_stages(), handlers, analyzer=bad_analyzer
        )

        assert final.current_state == State.HUMAN_REQUIRED
        assert final.status == RunStatus.WAITING
        assert final.return_state == State.DESIGN
        # 何を分析していたのかが人間に残る。
        assert final.pending_upstream_stage == DocumentStage.SPEC

        last = logger.history(final.run_id)[-1]
        assert last.event == Event.INVALID_IMPACT_RESULT
        assert "cannot be VALID" in last.reason

    def test_the_rejected_proposal_is_not_applied(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        def bad_analyzer(run, cause, stages, current):
            return ImpactResult(
                cause_stage=cause,
                impacts={config.name: ArtifactStatus.VALID for config in stages},
                reason="all good",
            )

        handlers = ScriptedPhaseHandlers(
            {
                Phase.GENERATE: [Event.DONE],
                Phase.REVIEW_DEEP: [
                    StageResult(
                        event=Event.UPSTREAM_CHANGE_REQUIRED,
                        upstream_target=DocumentStage.SPEC,
                        reason="spec is wrong",
                    )
                ],
            }
        ).as_handlers()

        final = run_design(
            design_run, logger, default_design_stages(), handlers, analyzer=bad_analyzer
        )

        assert final.current_state == State.HUMAN_REQUIRED
        statuses = design_artifact_statuses(logger, final)
        # 「全部 VALID」は適用されていない。
        assert statuses[DocumentStage.TESTCASE] == ArtifactStatus.STALE


class TestReviewRequiredPromotion:
    def test_a_review_only_stage_becomes_valid_on_pass(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        stages = default_design_stages()
        for config in stages:
            store.save_artifact(
                ArtifactState(
                    run_id=design_run.run_id,
                    kind=artifact_kind_for(config.name),
                    status=(
                        ArtifactStatus.REVIEW_REQUIRED
                        if config.name == DocumentStage.CLASS
                        else ArtifactStatus.VALID
                    ),
                )
            )

        final = run_design(design_run, logger, stages)

        assert final.current_state == State.IMPLEMENT
        assert starts(logger, final.run_id) == [
            (DocumentStage.CLASS, Phase.REVIEW_LIGHT)
        ]
        assert design_artifact_statuses(logger, final, stages)[
            DocumentStage.CLASS
        ] == ArtifactStatus.VALID

    def test_light_review_even_for_a_deep_stage(
        self, store: Store, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """§6「REVIEW_REQUIRED 影響の可能性あり。軽量レビューのみ」。

        SPEC は既定で DEEP だが、影響確認のための再訪は軽量で入る。
        """
        stages = default_design_stages()
        for config in stages:
            store.save_artifact(
                ArtifactState(
                    run_id=design_run.run_id,
                    kind=artifact_kind_for(config.name),
                    status=(
                        ArtifactStatus.REVIEW_REQUIRED
                        if config.name == DocumentStage.SPEC
                        else ArtifactStatus.VALID
                    ),
                )
            )

        final = run_design(design_run, logger, stages)

        assert starts(logger, final.run_id) == [(DocumentStage.SPEC, Phase.REVIEW_LIGHT)]
        assert final.review_phase == Phase.REVIEW_LIGHT
