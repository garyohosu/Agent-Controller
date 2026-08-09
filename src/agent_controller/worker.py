"""Worker interface（§17-11B）。

Controller をここまで純 Python に保てているので、Worker 側も厚くしない。
やることは 1 つだけ。

```text
Controller
   ↓  WorkerRequest
Worker Interface
   ↓  WorkerResult
Controller
```

Claude / Codex 固有の処理は Adapter（cli_worker.py）に閉じ込める。
この層は「何を頼み、何が返るか」の形だけを決める。

重要なのは、Worker の出力を**そのまま信用しない**こと。
scripted stub は書いた本人が形を保証していたので例外を投げてよかったが、
subprocess から返ってくる文字列にはその保証が無い。
壊れた出力は WORKER_ERROR に翻訳し、生の出力を残して人間へ渡す。
"""

from __future__ import annotations

from typing import Any, Protocol
from pathlib import Path

from pydantic import BaseModel, Field

from agent_controller.document_stage import PhaseHandler, StageResult, allowed_stage_events
from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    Question,
    Role,
    RunState,
    State,
    Worker,
)
from agent_controller.qanda import QANDA_FILENAME, QandaFile
from agent_controller.git_checkpoint import GitCheckpointError, GitCheckpointManager

PHASE_ROLES: dict[Phase, Role] = {
    Phase.GENERATE: Role.IMPLEMENTER,
    Phase.FIX: Role.IMPLEMENTER,
    Phase.REVIEW_LIGHT: Role.REVIEWER,
    Phase.REVIEW_DEEP: Role.REVIEWER,
    Phase.QANDA: Role.DIRECTOR,
}
"""phase ごとの担当 Role（memo.md §5）。

Implementer と Reviewer は可能なら別 Worker にする（指示書 §13）。
その振り分けはここではなく worker_for（Role -> Adapter）で行う。
"""


class WorkerOutput(BaseModel):
    """Worker に返させる JSON の形。プロンプトへはこの JSON Schema を渡す。"""

    event: str
    reason: str = ""
    """人間がログで読む 1 文。機械判定には使わない。"""

    finding_code: str | None = None
    """指摘の種別。RESPONSIBILITY_MISMATCH / TEST_FAILURE / MISSING_SECTION など。

    同じ問題を同じ code で呼ばせることで、reason の言い回しが変わっても
    「まだ直っていない」を機械的に判定できる（§17-9）。
    """

    finding_subject: str | None = None
    """指摘の対象。OrderService / tests/test_order.py::test_cancel など。"""

    finding_category: str | None = None

    question: str | None = None
    """QUESTION のとき、何が判断できないのか（§9）。"""

    answer: str | None = None
    """QANDA で回答したとき、その答え（§9）。"""

    upstream_target: str | None = None
    files_changed: list[str] = Field(default_factory=list)


WORKER_OUTPUT_SCHEMA: dict[str, Any] = WorkerOutput.model_json_schema()


class WorkerRequest(BaseModel):
    """Worker への依頼。

    プロジェクト開始からの全会話ではなく、この工程に必要なものだけを渡す
    （memo.md §4.2）。だから input_artifacts は「読んでよいファイル」の列挙になる。
    """

    role: Role
    state: State
    stage: DocumentStage | None = None
    phase: Phase | None = None

    workspace: str
    """作業ディレクトリ。Worker はこの中だけを読み書きする。"""

    input_artifacts: list[str] = Field(default_factory=list)
    """参照してよい成果物のパス。中身は Worker 自身に読ませる。"""

    artifact_contents: dict[str, str] = Field(default_factory=dict)
    """Controller-provided contents for Workers that cannot read the workspace."""

    output_artifact: str | None = None
    """書くべき成果物のパス。"""

    directive: str
    """何をすべきか。Director が作る指示に相当する。"""

    expected_output_schema: dict[str, Any] | None = None
    """返すべき構造化出力の JSON Schema。"""

    allowed_events: list[Event] = Field(default_factory=list)
    """この工程で返してよい Event。これ以外は受け付けない。"""


class WorkerResult(BaseModel):
    """Worker からの応答。"""

    event: Event
    structured_result: dict[str, Any] | None = None
    reason: str | None = None
    finding_code: str | None = None
    finding_subject: str | None = None
    finding_category: str | None = None

    question: str | None = None
    answer: str | None = None

    files_changed: list[str] = Field(default_factory=list)
    decision_class: str | None = None
    provisional_answer: str | None = None
    risk: str | None = None
    reversible: bool | None = None
    affected_artifacts: list[str] = Field(default_factory=list)
    blocking_scope: str | None = None
    recommended_human_action: str | None = None
    requires_human_confirmation_before_complete: bool = False
    """Worker の自己申告。実際に変わったかは検証していない（§17-14 の Git 連携まで）。"""

    raw_output: str = ""
    diagnostic: dict[str, Any] = Field(default_factory=dict)
    decision_class: str | None = None
    provisional_answer: str | None = None
    risk: str | None = None
    reversible: bool | None = None
    affected_artifacts: list[str] = Field(default_factory=list)
    blocking_scope: str | None = None
    recommended_human_action: str | None = None
    requires_human_confirmation_before_complete: bool = False


class WorkerAdapter(Protocol):
    """Worker の共通インターフェース。

    models.Worker が enum（DB に入る識別子）なので、基底クラス側は
    WorkerAdapter と呼び分ける。
    """

    name: Worker

    def run(self, request: WorkerRequest) -> WorkerResult: ...


class WorkerRouter:
    """Role-aware candidate selection; fallback never changes the position."""

    def __init__(self, candidates: dict[Role, list[WorkerAdapter] | WorkerAdapter]):
        self._candidates = {
            role: value if isinstance(value, list) else [value]
            for role, value in candidates.items()
        }

    def candidates_for(self, role: Role) -> list[WorkerAdapter]:
        return list(self._candidates.get(role, []))


class WorkerError(BaseModel):
    """Worker 出力を受け付けられなかった理由。"""

    message: str
    raw_output: str = ""


def validate_worker_result(
    result: WorkerResult,
    request: WorkerRequest,
) -> WorkerError | None:
    """Worker の応答が工程として成立しているかを見る。

    ここを通らないものは Controller の中へ入れない。
    """
    if request.allowed_events and result.event not in request.allowed_events:
        allowed = ", ".join(event.value for event in request.allowed_events)
        return WorkerError(
            message=f"{result.event.value} is not allowed here (expected one of {allowed})",
            raw_output=result.raw_output,
        )

    if result.event == Event.UPSTREAM_CHANGE_REQUIRED:
        target = (result.structured_result or {}).get("upstream_target")
        if target is None:
            return WorkerError(
                message="UPSTREAM_CHANGE_REQUIRED without an upstream_target",
                raw_output=result.raw_output,
            )
        try:
            DocumentStage(target)
        except ValueError:
            return WorkerError(
                message=f"{target!r} is not a document stage",
                raw_output=result.raw_output,
            )

    return None


def diagnostic_summary(result: WorkerResult) -> str:
    diagnostic = result.diagnostic
    if not diagnostic:
        return ""
    return (
        f"raw_exit={diagnostic.get('raw_exit_code')} "
        f"signed_exit={diagnostic.get('signed_exit_code')} "
        f"exe={diagnostic.get('resolved_executable')} "
        f"elapsed_ms={diagnostic.get('elapsed_ms')} "
        f"stderr_tail={str(diagnostic.get('stderr_tail', ''))[-300:]}"
    )


def stage_result_from(result: WorkerResult, worker: Worker, role: Role) -> StageResult:
    """検証済みの WorkerResult を Controller の StageResult にする。"""
    upstream = (result.structured_result or {}).get("upstream_target")
    return StageResult(
        event=result.event,
        role=role,
        worker=worker,
        reason=result.reason,
        upstream_target=DocumentStage(upstream) if upstream is not None else None,
        finding_code=result.finding_code,
        finding_subject=result.finding_subject,
        finding_category=result.finding_category,
        question=result.question,
        answer=result.answer,
        decision_class=result.decision_class,
        provisional_answer=result.provisional_answer,
        risk=result.risk,
        reversible=result.reversible,
        affected_artifacts=result.affected_artifacts,
        blocking_scope=result.blocking_scope,
        recommended_human_action=result.recommended_human_action,
        requires_human_confirmation_before_complete=result.requires_human_confirmation_before_complete,
    )


def build_request(
    run: RunState,
    phase: Phase,
    role: Role,
    workspace: str,
    input_artifacts: list[str],
    output_artifact: str,
    directive: str,
    allowed_events: list[Event],
    expected_output_schema: dict[str, Any] | None = None,
) -> WorkerRequest:
    contents: dict[str, str] = {}
    root = Path(workspace)
    for artifact in input_artifacts:
        path = root / artifact
        if path.is_file():
            contents[artifact] = path.read_text(encoding="utf-8", errors="replace")[:100_000]
    return WorkerRequest(
        role=role,
        state=run.current_state,
        stage=run.substate,
        phase=phase,
        workspace=workspace,
        input_artifacts=input_artifacts,
        artifact_contents=contents,
        output_artifact=output_artifact,
        directive=directive,
        expected_output_schema=expected_output_schema,
        allowed_events=allowed_events,
    )


NO_GUESSING_RULE = (
    "Do not fill in a judgement that the formal specification and design documents "
    "do not support. If something you need is not decided by the documents you were "
    "given, and getting it wrong would change the behaviour, the design, or the "
    "tests downstream, return QUESTION instead of deciding it yourself. "
    "A QUESTION must say what is undecided, which documents you checked, and why "
    "those documents cannot settle it. "
    "This is not an instruction to ask more often. Wording, obvious formatting "
    "choices, and restatements of something the documents already decide are yours "
    "to make - do not raise those as questions."
)
"""すべての意味判断 Worker にかかる共通制約（指示書 002 §1）。

実 AI で回したとき、Codex は仕様の穴を QUESTION にせず自分で埋めて DONE を返した。
Adapter の不具合ではなく、指示が「推測してはいけない」と縛っていなかったため。

Adapter ごとに書き分けず、ここ 1 箇所に置く。
"""

REVIEWER_RULE = (
    "Do not PASS something on your own interpretation when the documents do not "
    "support it. If the document under review states something its inputs do not "
    "decide, that gap was filled by guessing: return QUESTION rather than accepting "
    "or silently rewriting it."
)
"""Reviewer への追加制約（指示書 002 §1）。"""


_PHASE_TASKS: dict[Phase, str] = {
    Phase.GENERATE: (
        "Write the output document from the input documents. "
        "Return DONE only when everything you wrote is traceable to an input "
        "document. Return UPSTREAM_CHANGE_REQUIRED (with upstream_target) if an "
        "input document is itself wrong or contradictory."
    ),
    Phase.REVIEW_LIGHT: (
        "Review the output document against the input documents. Be brief: look for "
        "contradictions with the inputs, missing required sections, and broken "
        "diagram syntax. Return PASS if it is acceptable, LOCAL_FIX if it needs "
        "changes that stay inside this document, SERIOUS_ISSUE if a light review is "
        "not enough to judge it, or UPSTREAM_CHANGE_REQUIRED (with upstream_target) "
        "if an input document is wrong."
    ),
    Phase.REVIEW_DEEP: (
        "Review the output document thoroughly against the input documents: "
        "completeness, consistency, and whether it can actually be implemented. "
        "Return PASS, LOCAL_FIX, or UPSTREAM_CHANGE_REQUIRED (with upstream_target)."
    ),
    Phase.FIX: (
        "Apply the changes the reviewer asked for. Do not redesign anything that was "
        "not raised, and do not settle an open point while you are here. "
        "Return DONE when the document is updated."
    ),
    Phase.QANDA: (
        "Answer the open question in QandA.md using only the existing documents. "
        "First classify any unresolved question as exactly one of "
        "DECIDABLE_FROM_ARTIFACT, LOW_RISK_REVERSIBLE, HIGH_RISK_PRODUCT_DECISION, "
        "EXTERNAL_CONTRACT, SECURITY_OR_SAFETY, IRREVERSIBLE_OR_DATA_LOSS, or UNKNOWN. "
        "Return DONE with `answer` if the documents settle it. For LOW_RISK_REVERSIBLE, "
        "return DONE with `decision_class`, `provisional_answer`, `risk=LOW`, and "
        "`reversible=true`; this is a provisional decision, not a formal specification. "
        "LOCAL_FIX if answering it also requires changing the output document, or "
        "CANNOT_ANSWER if no document settles it and answering would be a guess. "
        "For CANNOT_ANSWER, include the classification, risk, reversible, and "
        "recommended_human_action fields. "
        "Cite the documents your answer rests on."
    ),
}

_REVIEW_PHASES_WITH_RULE = (Phase.REVIEW_LIGHT, Phase.REVIEW_DEEP)


def _directive_for(phase: Phase) -> str:
    parts = [_PHASE_TASKS[phase], "", NO_GUESSING_RULE]
    if phase in _REVIEW_PHASES_WITH_RULE:
        parts += ["", REVIEWER_RULE]
    return "\n".join(parts)


PHASE_DIRECTIVES: dict[Phase, str] = {
    phase: _directive_for(phase) for phase in _PHASE_TASKS
}
"""phase ごとの既定の指示。§17-13 で Director がこれを差し替える。"""


def qanda_directive(base: str, question: Question | None) -> str:
    """QANDA の指示は、その時点で開いている質問から組み立てる。

    ここが「指示が固定文でなくなる」最初の場所で、§17-13 の Director の芽になる。
    今は Controller が質問 1 件をそのまま渡すだけ。
    """
    if question is None:
        return base
    lines = [
        base,
        "",
        f"The open question is {question.question_id}, raised at {question.position()}:",
        "",
        question.question,
    ]
    if question.context:
        lines += ["", "Context given by the asker:", "", question.context]
    return "\n".join(lines)


def phase_handlers_from_worker(
    workers: WorkerAdapter | WorkerRouter | dict[Role, WorkerAdapter | list[WorkerAdapter]],
    workspace: str,
    input_artifacts: list[str],
    output_artifact: str,
    allowed_events: dict[Phase, list[Event]] | None = None,
    directives: dict[Phase, str] | None = None,
    qanda: QandaFile | None = None,
    git: GitCheckpointManager | None = None,
) -> dict[Phase, PhaseHandler]:
    """Worker を Document Stage の phase handler に変換する。

    workers に dict を渡すと Role ごとに別の Worker を使う（指示書 §13 の
    「Implementer と Reviewer は別 Worker」）。1 つだけ渡せば単独運転。
    """
    if isinstance(workers, WorkerRouter):
        router = workers
        worker_map = None
    elif not isinstance(workers, dict):
        router = None
        worker_map = {role: [workers] for role in Role}
    else:
        router = None
        worker_map = {
            role: value if isinstance(value, list) else [value]
            for role, value in workers.items()
        }

    directives = directives if directives is not None else PHASE_DIRECTIVES

    def make(phase: Phase) -> PhaseHandler:
        role = PHASE_ROLES[phase]
        candidates = router.candidates_for(role) if router else worker_map[role]
        adapter = candidates[0]
        permitted = (
            allowed_events[phase]
            if allowed_events is not None and phase in allowed_events
            else sorted(allowed_stage_events(phase), key=lambda event: event.value)
        )

        def handler(run: RunState) -> StageResult:
            if git is not None:
                try:
                    git.begin(run)
                except GitCheckpointError as error:
                    return StageResult(
                        event=Event.WORKER_ERROR,
                        role=role,
                        worker=adapter.name,
                        reason=f"git checkpoint unavailable: {error}",
                    )

            directive = directives[phase]
            documents = list(input_artifacts)
            if phase == Phase.QANDA and qanda is not None:
                directive = qanda_directive(directive, qanda.oldest_open(run.run_id))
                documents = [*documents, QANDA_FILENAME]

            request = build_request(
                run,
                phase,
                role,
                workspace,
                documents,
                output_artifact,
                directive,
                permitted,
                expected_output_schema=WORKER_OUTPUT_SCHEMA,
            )
            result = None
            selected = candidates[0]
            failures: list[str] = []
            for index, candidate in enumerate(candidates):
                selected = candidate
                result = candidate.run(request)
                if result.event in (Event.WORKER_ERROR, Event.WORKER_RESOURCE_LIMIT):
                    summary = diagnostic_summary(result)
                    if summary:
                        result.reason = f"{result.reason or result.event.value}; {summary}"
                problem = validate_worker_result(result, request)
                if problem is not None:
                    result.event = Event.WORKER_ERROR
                    result.reason = f"{problem.message} | raw: {problem.raw_output[:200]}"
                if result.event not in (Event.WORKER_ERROR, Event.WORKER_RESOURCE_LIMIT):
                    break
                failures.append(f"{candidate.name.value}: {result.reason or result.event.value}")
                if git is not None:
                    try:
                        actual = git.verified_files_changed(result.files_changed)
                        git.rollback(run.checkpoint_commit or git.head())
                        result.reason = (
                            f"{result.reason or result.event.value}; rolled back "
                            f"{actual or 'no tracked changes'} to {run.checkpoint_commit}"
                        )
                    except GitCheckpointError as error:
                        result.event = Event.WORKER_ERROR
                        result.reason = f"rollback failed: {error}"
                        break
                if index + 1 < len(candidates) and result.event in (
                    Event.WORKER_ERROR,
                    Event.WORKER_RESOURCE_LIMIT,
                ):
                    continue
                break
            assert result is not None
            if len(failures) > 1:
                result.reason = f"fallback attempts: {'; '.join(failures)}; {result.reason or result.event.value}"
            if git is not None and result.event not in (
                Event.WORKER_ERROR,
                Event.WORKER_RESOURCE_LIMIT,
            ):
                try:
                    actual = git.verified_files_changed(result.files_changed)
                    git.commit_success(result.event)
                    result.reason = (
                        f"{result.reason or result.event.value}; "
                        f"git files_changed={actual or 'none'}"
                    )
                except GitCheckpointError as error:
                    result.event = Event.WORKER_ERROR
                    result.reason = f"git success handling failed: {error}"

            problem = validate_worker_result(result, request)
            if problem is not None:
                # subprocess の出力は信用しない。壊れていても例外にせず、
                # 遷移として記録できる形（WORKER_ERROR）に翻訳する。
                return StageResult(
                    event=Event.WORKER_ERROR,
                    role=role,
                    worker=adapter.name,
                    reason=f"{problem.message} | raw: {problem.raw_output[:200]}",
                )

            return stage_result_from(result, selected.name, role)

        return handler

    return {phase: make(phase) for phase in PHASE_ROLES}


def phase_handlers_for_stages(
    workers: WorkerAdapter | WorkerRouter | dict[Role, WorkerAdapter | list[WorkerAdapter]],
    workspace: str,
    stages: list[Any],
    *,
    git: GitCheckpointManager | None = None,
) -> dict[Phase, PhaseHandler]:
    """Dispatch phase handlers by the current DocumentStage.

    A plain ``dict[Phase, handler]`` cannot represent different artifact inputs
    for SPEC and TESTCASE; the dispatcher keeps those contracts separate while
    preserving the existing phase and top-level state machines.
    """
    per_stage = {
        stage.name: phase_handlers_from_worker(
            workers, workspace, list(stage.inputs), stage.output, git=git
        )
        for stage in stages
    }

    def dispatch(phase: Phase) -> PhaseHandler:
        def handler(run: RunState) -> StageResult:
            if run.substate not in per_stage:
                return StageResult(
                    event=Event.WORKER_ERROR,
                    role=PHASE_ROLES[phase],
                    reason=f"no phase handler for document stage {run.substate}",
                )
            return per_stage[run.substate][phase](run)

        return handler

    return {phase: dispatch(phase) for phase in PHASE_ROLES}

