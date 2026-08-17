"""Default Decision Policy と Decision Log のテスト（指示書 2026-08-17-018 §6 / §7 / §8）。

AC-06: 既存 API 互換のような軽微・可逆な判断は、Default Decision Policy に従って
       Controller が人間へ問い合わせず自動で決定し、run が続行する。
AC-07: スコープ拡大を避けるという既定方針が、実際に Worker への指示に含まれている。
AC-08: 自動決定した理由（policy_rule / classification）が SQLite から追跡できる。
SS7:   同じ run 内で同じ policy_scope の決定は、2 回目以降 Worker を呼ばずに再利用する。
"""

from __future__ import annotations

from pathlib import Path

from agent_controller.document_stage import (
    DocumentStageConfig,
    run_document_stage,
    stage_completed,
)
from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    QuestionStatus,
    RecoveryAttempt,
    Role,
    RunState,
    State,
    Worker,
)
from agent_controller.qanda import QandaFile
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger
from agent_controller.worker import (
    DEFAULT_DECISION_POLICY,
    PHASE_DIRECTIVES,
    WorkerRequest,
    WorkerResult,
    phase_handlers_from_worker,
)

SPEC_STAGE = DocumentStageConfig(name=DocumentStage.SPEC, inputs=[], output="SPEC.md")


class ScriptedDecisionWorker:
    """GENERATE で 1 度だけ質問し、Director が Default Decision Policy で即決する。"""

    name = Worker.CODEX_CLI

    def __init__(self, policy_rule: str, policy_scope: str, answer: str) -> None:
        self.generate_calls = 0
        self.policy_rule = policy_rule
        self.policy_scope = policy_scope
        self.answer = answer

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.phase == Phase.GENERATE:
            self.generate_calls += 1
            if self.generate_calls == 1:
                return WorkerResult(
                    event=Event.QUESTION,
                    question="Keep the existing flat API shape or move to nested?",
                )
            return WorkerResult(event=Event.DONE)
        if request.phase == Phase.QANDA:
            return WorkerResult(
                event=Event.DONE,
                decision_class="LOW_RISK_REVERSIBLE",
                provisional_answer=self.answer,
                risk="LOW",
                reversible=True,
                action="ANSWER_ONLY",
                policy_rule=self.policy_rule,
                policy_scope=self.policy_scope,
            )
        if request.phase == Phase.REVIEW_LIGHT:
            return WorkerResult(event=Event.PASS)
        return WorkerResult(event=Event.DONE)


def run_at(run_id: str = "policy-run") -> RunState:
    return RunState(project_id="p", run_id=run_id, current_state=State.DESIGN)


def test_low_risk_reversible_decision_never_reaches_human_required(tmp_path: Path) -> None:
    with Store(":memory:") as store:
        logger = TransitionLogger(store)
        run = run_at()
        store.save_run(run)
        qanda = QandaFile(store, tmp_path)
        worker = ScriptedDecisionWorker(
            policy_rule="EXISTING_API_COMPATIBILITY",
            policy_scope="API_SHAPE",
            answer="Keep the existing flat API shape.",
        )
        handlers = phase_handlers_from_worker(worker, str(tmp_path), [], "SPEC.md", qanda=qanda)

        final = run_document_stage(run, SPEC_STAGE, logger, handlers, qanda=qanda)

        assert final.current_state == State.DESIGN  # never left for HUMAN_REQUIRED
        assert stage_completed(final, SPEC_STAGE)

        [question] = store.questions(final.run_id)
        assert question.status is QuestionStatus.PROVISIONAL
        assert question.classification == "LOW_RISK_REVERSIBLE"
        assert question.policy_rule == "EXISTING_API_COMPATIBILITY"
        assert question.policy_scope == "API_SHAPE"
        assert question.provisional_answer == "Keep the existing flat API shape."


def test_default_decision_policy_text_forbids_scope_expansion() -> None:
    assert "do not expand the requested scope" in DEFAULT_DECISION_POLICY.lower()
    assert "existing api" in DEFAULT_DECISION_POLICY.lower()
    for phase in (Phase.GENERATE, Phase.FIX, Phase.QANDA, Phase.REVIEW_LIGHT):
        assert DEFAULT_DECISION_POLICY in PHASE_DIRECTIVES[phase]
    # The genuinely escalation-worthy cases must still be named, not silently dropped.
    assert "irreversible" in DEFAULT_DECISION_POLICY.lower()
    assert "security" in DEFAULT_DECISION_POLICY.lower()


def test_decision_is_queryable_from_sqlite_not_only_markdown(tmp_path: Path) -> None:
    """AC-08: Decision Log は SQLite が正本。Markdown (QandA.md) だけに頼らない。"""
    with Store(tmp_path / "controller.db") as store:
        logger = TransitionLogger(store)
        run = run_at("decision-log-run")
        store.save_run(run)
        qanda = QandaFile(store, tmp_path)
        worker = ScriptedDecisionWorker(
            policy_rule="MINIMAL_CHANGE", policy_scope="SCOPE_X", answer="Do the smallest thing.",
        )
        handlers = phase_handlers_from_worker(worker, str(tmp_path), [], "SPEC.md", qanda=qanda)
        run_document_stage(run, SPEC_STAGE, logger, handlers, qanda=qanda)

    # Reopen the DB file fresh, proving the decision survives independent of
    # the in-process QandA.md render.
    with Store(tmp_path / "controller.db") as reopened:
        [question] = reopened.questions("decision-log-run")
        assert question.policy_rule == "MINIMAL_CHANGE"
        assert question.classification == "LOW_RISK_REVERSIBLE"


def test_recovery_attempt_round_trips_through_sqlite(tmp_path: Path) -> None:
    with Store(tmp_path / "controller.db") as store:
        run = run_at("recovery-log-run")
        store.save_run(run)
        store.save_recovery_attempt(RecoveryAttempt(
            run_id=run.run_id, error_code="WRITE_PERMISSION_DENIED",
            failed_worker=Worker.CODEX_CLI, failed_role=Role.IMPLEMENTER,
            fallback_worker=Worker.CLAUDE_CODE, attempt_number=1,
            final_outcome="FALLBACK_SUCCEEDED", reason="filesystem writes were denied",
        ))
    with Store(tmp_path / "controller.db") as reopened:
        [attempt] = reopened.recovery_attempts("recovery-log-run")
        assert attempt.error_code == "WRITE_PERMISSION_DENIED"
        assert attempt.failed_worker == Worker.CODEX_CLI
        assert attempt.fallback_worker == Worker.CLAUDE_CODE
        assert attempt.final_outcome == "FALLBACK_SUCCEEDED"


# --- SS7: same-run answer reuse --------------------------------------------


class TwoQuestionsSameScopeWorker:
    """SPEC で 1 問目、TESTCASE 相当の 2 個目の stage で同じ policy_scope の質問を出す。

    2 回目の QANDA が絶対に呼ばれないことを確認するため、QANDA が呼ばれたら
    例外にする。
    """

    name = Worker.CODEX_CLI

    def __init__(self) -> None:
        self.generate_calls = 0
        self.qanda_calls = 0

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.phase == Phase.GENERATE:
            self.generate_calls += 1
            if self.generate_calls == 1:
                return WorkerResult(
                    event=Event.QUESTION,
                    question="Keep the existing flat API shape or move to nested?",
                )
            if self.generate_calls == 3:
                # This asker recognises it as the same category of decision
                # (指示書 018 §7 rule 8: the ASKER tags policy_scope, not just
                # the answerer) and tags it so the controller can reuse the
                # earlier answer without calling a Worker again.
                return WorkerResult(
                    event=Event.QUESTION,
                    question="Same API-shape decision again for TESTCASE?",
                    policy_scope="API_SHAPE",
                )
            return WorkerResult(event=Event.DONE)
        if request.phase == Phase.QANDA:
            self.qanda_calls += 1
            if self.qanda_calls > 1:
                raise AssertionError("QANDA must not be invoked twice for the same policy_scope")
            return WorkerResult(
                event=Event.DONE, decision_class="LOW_RISK_REVERSIBLE",
                provisional_answer="Keep the existing flat API shape.",
                risk="LOW", reversible=True, action="ANSWER_ONLY",
                policy_rule="EXISTING_API_COMPATIBILITY", policy_scope="API_SHAPE",
            )
        if request.phase == Phase.REVIEW_LIGHT:
            return WorkerResult(event=Event.PASS)
        return WorkerResult(event=Event.DONE)


def test_second_question_with_same_policy_scope_reuses_the_first_answer(tmp_path: Path) -> None:
    with Store(":memory:") as store:
        logger = TransitionLogger(store)
        run = run_at("reuse-run")
        store.save_run(run)
        qanda = QandaFile(store, tmp_path)
        worker = TwoQuestionsSameScopeWorker()
        handlers = phase_handlers_from_worker(worker, str(tmp_path), [], "SPEC.md", qanda=qanda)

        run_document_stage(run, SPEC_STAGE, logger, handlers, qanda=qanda)
        assert worker.qanda_calls == 1  # answered once via the real Director

        testcase_stage = DocumentStageConfig(
            name=DocumentStage.TESTCASE, inputs=["SPEC.md"], output="TESTCASE.md"
        )
        final = run_document_stage(run, testcase_stage, logger, handlers, qanda=qanda)

        assert stage_completed(final, testcase_stage)
        assert worker.qanda_calls == 1, "QANDA must not be re-invoked for the reused decision"

        questions = store.questions(final.run_id)
        assert len(questions) == 2
        reused = questions[1]
        assert reused.status is QuestionStatus.PROVISIONAL
        assert reused.policy_scope == "API_SHAPE"
        assert "Keep the existing flat API shape." in (reused.provisional_answer or "")
