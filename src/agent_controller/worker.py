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

from pydantic import BaseModel, Field

from agent_controller.document_stage import PhaseHandler, StageResult, allowed_stage_events
from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    Role,
    RunState,
    State,
    Worker,
)

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
    files_changed: list[str] = Field(default_factory=list)
    """Worker の自己申告。実際に変わったかは検証していない（§17-14 の Git 連携まで）。"""

    raw_output: str = ""


class WorkerAdapter(Protocol):
    """Worker の共通インターフェース。

    models.Worker が enum（DB に入る識別子）なので、基底クラス側は
    WorkerAdapter と呼び分ける。
    """

    name: Worker

    def run(self, request: WorkerRequest) -> WorkerResult: ...


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


def stage_result_from(result: WorkerResult, worker: Worker, role: Role) -> StageResult:
    """検証済みの WorkerResult を Controller の StageResult にする。"""
    upstream = (result.structured_result or {}).get("upstream_target")
    return StageResult(
        event=result.event,
        role=role,
        worker=worker,
        reason=result.reason,
        upstream_target=DocumentStage(upstream) if upstream is not None else None,
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
    return WorkerRequest(
        role=role,
        state=run.current_state,
        stage=run.substate,
        phase=phase,
        workspace=workspace,
        input_artifacts=input_artifacts,
        output_artifact=output_artifact,
        directive=directive,
        expected_output_schema=expected_output_schema,
        allowed_events=allowed_events,
    )


PHASE_DIRECTIVES: dict[Phase, str] = {
    Phase.GENERATE: (
        "Write the output document from the input documents. "
        "Return DONE when the document is written, "
        "QUESTION if the inputs do not tell you enough to write it, "
        "or UPSTREAM_CHANGE_REQUIRED (with upstream_target) if an input document "
        "is itself wrong or contradictory."
    ),
    Phase.REVIEW_LIGHT: (
        "Review the output document against the input documents. Be brief: look only "
        "for contradictions with the inputs, missing required sections, and broken "
        "diagram syntax. Return PASS if it is acceptable, LOCAL_FIX if it needs "
        "changes that stay inside this document, QUESTION if you need a decision, "
        "SERIOUS_ISSUE if a light review is not enough to judge it, or "
        "UPSTREAM_CHANGE_REQUIRED (with upstream_target) if an input document is wrong."
    ),
    Phase.REVIEW_DEEP: (
        "Review the output document thoroughly against the input documents: "
        "completeness, consistency, and whether it can actually be implemented. "
        "Return PASS, LOCAL_FIX, QUESTION, or UPSTREAM_CHANGE_REQUIRED "
        "(with upstream_target)."
    ),
    Phase.FIX: (
        "Apply the changes the reviewer asked for. Do not redesign anything that was "
        "not raised. Return DONE when the document is updated, or QUESTION if the "
        "requested change is ambiguous."
    ),
    Phase.QANDA: (
        "Answer the open question using only the existing documents. "
        "Return DONE with the answer in reason if the documents settle it, "
        "LOCAL_FIX if answering it requires changing the output document, or "
        "CANNOT_ANSWER if no document settles it and answering would be a guess."
    ),
}
"""phase ごとの既定の指示。§17-13 で Director がこれを差し替える。"""


def phase_handlers_from_worker(
    workers: WorkerAdapter | dict[Role, WorkerAdapter],
    workspace: str,
    input_artifacts: list[str],
    output_artifact: str,
    allowed_events: dict[Phase, list[Event]] | None = None,
    directives: dict[Phase, str] | None = None,
) -> dict[Phase, PhaseHandler]:
    """Worker を Document Stage の phase handler に変換する。

    workers に dict を渡すと Role ごとに別の Worker を使う（指示書 §13 の
    「Implementer と Reviewer は別 Worker」）。1 つだけ渡せば単独運転。
    """
    if not isinstance(workers, dict):
        workers = {role: workers for role in Role}

    directives = directives if directives is not None else PHASE_DIRECTIVES

    def make(phase: Phase) -> PhaseHandler:
        role = PHASE_ROLES[phase]
        adapter = workers[role]
        permitted = (
            allowed_events[phase]
            if allowed_events is not None and phase in allowed_events
            else sorted(allowed_stage_events(phase), key=lambda event: event.value)
        )

        def handler(run: RunState) -> StageResult:
            request = build_request(
                run,
                phase,
                role,
                workspace,
                input_artifacts,
                output_artifact,
                directives[phase],
                permitted,
                expected_output_schema=WORKER_OUTPUT_SCHEMA,
            )
            result = adapter.run(request)

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

            return stage_result_from(result, adapter.name, role)

        return handler

    return {phase: make(phase) for phase in PHASE_ROLES}

