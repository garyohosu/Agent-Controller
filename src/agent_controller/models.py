"""State / Event / ArtifactStatus models.

指示書 §1（トップレベル State）、§2（DESIGN の段階的詳細化）、§3（Document Stage）、
§6（影響範囲分析）、§21（SQLite 保持情報）に対応する。

ここには型と値だけを置く。遷移規則は transitions.py にある。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp. Single source of "now" for the controller."""
    return datetime.now(timezone.utc)


class State(StrEnum):
    """トップレベル State（指示書 §1）。

    SPEC_CREATE / USECASE_CREATE のような工程別 State はここに並べない。
    それらは DESIGN の Substate（DocumentStage）として扱う。
    """

    IDLE = "IDLE"
    DESIGN = "DESIGN"
    IMPLEMENT = "IMPLEMENT"
    TEST = "TEST"
    REVIEW = "REVIEW"
    DOC_SYNC = "DOC_SYNC"
    COMPLETE = "COMPLETE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    WAIT_RESOURCE = "WAIT_RESOURCE"
    ABORT = "ABORT"


TERMINAL_STATES: frozenset[State] = frozenset(
    {State.COMPLETE, State.ABORT}
)
"""run がそこで終わる State。"""

PAUSE_STATES: frozenset[State] = frozenset({State.HUMAN_REQUIRED, State.WAIT_RESOURCE})
"""外部入力を待って停止する State。再開時は return_state へ戻る。"""


class DocumentStage(StrEnum):
    """DESIGN の Substate（指示書 §2 の段階的詳細化）。

    上から順に詳細化される。SPEC が最上流。
    """

    SPEC = "SPEC"
    USECASE = "USECASE"
    SEQUENCE = "SEQUENCE"
    CLASS = "CLASS"
    UI = "UI"
    TESTCASE = "TESTCASE"


DOCUMENT_STAGE_ORDER: tuple[DocumentStage, ...] = (
    DocumentStage.SPEC,
    DocumentStage.USECASE,
    DocumentStage.SEQUENCE,
    DocumentStage.CLASS,
    DocumentStage.UI,
    DocumentStage.TESTCASE,
)
"""上流→下流の順序。IMPACT_ANALYSIS（§6）が最上流の STALE を求めるのに使う。"""


class Phase(StrEnum):
    """Document Stage Subgraph 内の phase（指示書 §3）。

    値のみ先に定義する。Subgraph 本体は §17-6 で実装する。
    """

    GENERATE = "GENERATE"
    REVIEW = "REVIEW"
    FIX = "FIX"
    COMPLETE = "COMPLETE"


class ReviewLevel(StrEnum):
    """レビュー強度（指示書 §4 / §5）。通常は LIGHT、疑いがある時だけ DEEP。"""

    LIGHT = "LIGHT"
    DEEP = "DEEP"


class Event(StrEnum):
    """Worker が返す Event。Controller はこれを見て次 State を決める。

    指示書に名前が出ている Event をそのまま使う。
    末尾の 4 件は指示書に明記が無いが、IDLE / WAIT_RESOURCE / HUMAN_REQUIRED /
    ABORT へ出入りするために必要なため補った（result に記録済み）。
    """

    # 指示書に明記されている Event
    DONE = "DONE"
    PASS = "PASS"
    FAIL = "FAIL"
    LOCAL_FIX = "LOCAL_FIX"
    QUESTION = "QUESTION"
    SERIOUS_ISSUE = "SERIOUS_ISSUE"
    UPSTREAM_CHANGE_REQUIRED = "UPSTREAM_CHANGE_REQUIRED"
    WORKER_ERROR = "WORKER_ERROR"
    WORKER_RESOURCE_LIMIT = "WORKER_RESOURCE_LIMIT"
    NO_PROGRESS = "NO_PROGRESS"
    LOOP_DETECTED = "LOOP_DETECTED"
    CANNOT_ANSWER = "CANNOT_ANSWER"

    # 補った Event（指示書に名前は無いが遷移上必要）
    START = "START"
    HUMAN_ANSWER = "HUMAN_ANSWER"
    RESOURCE_AVAILABLE = "RESOURCE_AVAILABLE"
    ABORT_REQUESTED = "ABORT_REQUESTED"


class ArtifactStatus(StrEnum):
    """影響範囲分析の結果（指示書 §6）。"""

    VALID = "VALID"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    STALE = "STALE"


class ArtifactKind(StrEnum):
    """影響範囲分析の対象成果物（指示書 §6 の例）。"""

    SPEC = "SPEC"
    USECASE = "USECASE"
    SEQUENCE = "SEQUENCE"
    CLASS = "CLASS"
    UI = "UI"
    TESTCASE = "TESTCASE"
    CODE = "CODE"
    README = "README"


ARTIFACT_ORDER: tuple[ArtifactKind, ...] = (
    ArtifactKind.SPEC,
    ArtifactKind.USECASE,
    ArtifactKind.SEQUENCE,
    ArtifactKind.CLASS,
    ArtifactKind.UI,
    ArtifactKind.TESTCASE,
    ArtifactKind.CODE,
    ArtifactKind.README,
)
"""依存関係の上流→下流順（memo.md §13）。"""


class Role(StrEnum):
    """論理 Role（memo.md §5）。CONTROLLER だけは AI ではない。"""

    CONTROLLER = "CONTROLLER"
    DIRECTOR = "DIRECTOR"
    IMPLEMENTER = "IMPLEMENTER"
    REVIEWER = "REVIEWER"
    ANSWERER = "ANSWERER"


class Worker(StrEnum):
    """初期対応 Worker（指示書 §13）。通常は CLAUDE_CODE と CODEX_CLI を優先。"""

    CLAUDE_CODE = "CLAUDE_CODE"
    CODEX_CLI = "CODEX_CLI"
    ANTIGRAVITY = "ANTIGRAVITY"
    GROK = "GROK"


class RunStatus(StrEnum):
    """run 全体の状態（指示書 §21 の status）。"""

    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class RunState(BaseModel):
    """run 1 件の現在位置。LangGraph の graph state であり、SQLite の runs 行でもある。

    フィールドは指示書 §21 に対応する。substate / phase は §17-6 の
    DocumentStage Subgraph 実装まで None のままになる。
    """

    project_id: str
    run_id: str

    current_state: State = State.IDLE
    substate: DocumentStage | None = None
    phase: Phase | None = None

    previous_state: State | None = None
    previous_substate: DocumentStage | None = None

    return_state: State | None = None
    """HUMAN_REQUIRED / WAIT_RESOURCE から復帰する先。"""

    question_source_state: State | None = None
    resume_role: Role | None = None

    last_event: Event | None = None

    active_role: Role | None = None
    active_worker: Worker | None = None

    checkpoint_commit: str | None = None
    """State 開始時に記録する commit SHA（指示書 §12）。rollback はこの SHA へ戻す。"""

    retry_count: int = 0
    transition_count: int = 0

    status: RunStatus = RunStatus.RUNNING

    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Transition(BaseModel):
    """状態遷移ログ 1 行（指示書 §10 の 15 項目 + to_phase）。

    SQLite の transitions 行を正本とし、人間向けテキストはここから生成する。

    to_phase だけは §10 の項目一覧に無い。ただし §10 の表示例
    ``-> DESIGN/CLASS/FIX`` は遷移先の phase を含むため、例をそのまま再現するには
    必要になる。§17-6 での schema 変更を避けるためここで足してある。
    """

    timestamp: datetime = Field(default_factory=utcnow)
    run_id: str

    state: State
    """遷移を評価した時点の位置。通常は from_state と同じ。"""

    substate: DocumentStage | None = None
    phase: Phase | None = None

    from_state: State
    from_substate: DocumentStage | None = None

    event: Event

    to_state: State
    to_substate: DocumentStage | None = None
    to_phase: Phase | None = None

    role: Role | None = None
    worker: Worker | None = None
    reason: str | None = None

    retry_count: int = 0
    checkpoint_commit: str | None = None


class ArtifactState(BaseModel):
    """成果物 1 件の影響分析結果（指示書 §6）。"""

    run_id: str
    kind: ArtifactKind
    status: ArtifactStatus = ArtifactStatus.VALID
    path: str | None = None
    reason: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
