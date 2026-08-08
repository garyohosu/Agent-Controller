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
    """Document Stage Subgraph 内の phase（指示書 §3 / §4）。

    §3 の図では REVIEW は 1 つだが、§4 の FAST PATH / DEEP PATH を phase として
    素直に表すため REVIEW_LIGHT と REVIEW_DEEP に分けている。
    通常は REVIEW_LIGHT だけを通り、SERIOUS_ISSUE が出た時だけ REVIEW_DEEP へ上がる。
    """

    GENERATE = "GENERATE"
    REVIEW_LIGHT = "REVIEW_LIGHT"
    REVIEW_DEEP = "REVIEW_DEEP"
    FIX = "FIX"
    QANDA = "QANDA"
    COMPLETE = "COMPLETE"


REVIEW_PHASES: frozenset[Phase] = frozenset({Phase.REVIEW_LIGHT, Phase.REVIEW_DEEP})


class ReviewLevel(StrEnum):
    """レビュー強度（指示書 §4 / §5）。通常は LIGHT、疑いがある時だけ DEEP。

    Document Stage の設定値であり、その stage で最初に入る review phase を決める。
    """

    LIGHT = "LIGHT"
    DEEP = "DEEP"

    @property
    def phase(self) -> Phase:
        return Phase.REVIEW_DEEP if self is ReviewLevel.DEEP else Phase.REVIEW_LIGHT


DEFAULT_REVIEW_LEVELS: dict[DocumentStage, ReviewLevel] = {
    DocumentStage.SPEC: ReviewLevel.DEEP,
    DocumentStage.USECASE: ReviewLevel.DEEP,
    DocumentStage.SEQUENCE: ReviewLevel.LIGHT,
    DocumentStage.CLASS: ReviewLevel.LIGHT,
    DocumentStage.UI: ReviewLevel.LIGHT,
    DocumentStage.TESTCASE: ReviewLevel.LIGHT,
}
"""指示書 §5 の初期値。State Machine 側で LIGHT → DEEP へ昇格できる。"""


class Event(StrEnum):
    """Worker が返す Event。Controller はこれを見て次 State を決める。

    指示書に名前が出ている Event をそのまま使う。
    それ以外は追加した理由をブロックごとに記す（result に記録済み）。
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

    # 影響範囲分析の結果が依存関係として成立していない（§6 / §17-10）。
    # AI は影響範囲を提案できるが、依存グラフの制約は破れない。
    INVALID_IMPACT_RESULT = "INVALID_IMPACT_RESULT"

    # 指示書 §3 の max_review_retry / §11 の retry 上限を Event 化したもの。
    # NO_PROGRESS とは分ける。RETRY_LIMIT は回数の超過、NO_PROGRESS は同じ失敗の
    # 繰り返し（fingerprint）で、判定材料が違う。
    RETRY_LIMIT = "RETRY_LIMIT"

    # 遷移上必要なため補った Event。IDLE を出る / HUMAN_REQUIRED・WAIT_RESOURCE から
    # 戻る / 終了要求を出す、をいずれも暗黙処理にせず正式な Event にする。
    START = "START"
    HUMAN_ANSWER = "HUMAN_ANSWER"
    RESOURCE_AVAILABLE = "RESOURCE_AVAILABLE"
    ABORT_REQUESTED = "ABORT_REQUESTED"


class ArtifactStatus(StrEnum):
    """影響範囲分析の結果（指示書 §6）。

    VALID           影響なし。処理不要
    REVIEW_REQUIRED 影響の可能性あり。軽量レビューのみ
    STALE           影響あり。再生成または修正が必要
    """

    VALID = "VALID"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    STALE = "STALE"

    @property
    def severity(self) -> int:
        """深刻さの順序。影響を統合するとき、軽い方へは倒さない。"""
        return {
            ArtifactStatus.VALID: 0,
            ArtifactStatus.REVIEW_REQUIRED: 1,
            ArtifactStatus.STALE: 2,
        }[self]


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

    return_phase: Phase | None = None
    """中断した Document Stage を再開する phase。

    return_state の phase 版。レビュー途中で resource limit に当たった run を、
    GENERATE からやり直させずに止まった位置へ戻すために持つ。
    上位工程へ戻るための離脱（UPSTREAM_CHANGE_REQUIRED / SERIOUS_ISSUE）では
    その stage をやり直すので設定しない。
    """

    pending_upstream_stage: DocumentStage | None = None
    """UPSTREAM_CHANGE_REQUIRED で Worker が指した、戻るべき上位工程。

    Controller が推測で決めない。Worker が指定しなければ受け付けない。
    """

    question_source_state: State | None = None
    question_source_phase: Phase | None = None
    """QANDA へ入る前の phase。回答後はここへ戻る（指示書 §3 / §9）。"""

    resume_role: Role | None = None

    review_phase: Phase | None = None
    """現在の Document Stage で有効なレビュー強度。SERIOUS_ISSUE で LIGHT→DEEP に上がる。"""

    review_retry: int = 0
    """現在の Document Stage で FIX からレビューへ戻った回数（§3 の max_review_retry 用）。"""

    last_event: Event | None = None

    active_role: Role | None = None
    active_worker: Worker | None = None

    checkpoint_commit: str | None = None
    """State 開始時に記録する commit SHA（指示書 §12）。rollback はこの SHA へ戻す。"""

    state_retry: int = 0
    """同じトップレベル State を続けて実行した回数。State が変わると 0 に戻る。"""

    repeat: int = 0
    """直前とまったく同じ遷移が続いた回数。初回は 0、2 回目で 1。"""

    last_transition_key: str | None = None
    """repeat を数えるための直前の遷移の指紋。stage が変わるとクリアする。"""

    upstream_rework: int = 0
    """この run で上位工程へ戻した回数（§11 の「同一理由による上位手戻り」用）。"""

    transition_count: int = 0
    """この run の総遷移数。§11 の「1 run の最大 transition 数」に使う。"""

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

    state_retry: int = 0
    review_retry: int = 0
    repeat: int = 0
    """§10 の retry_count を用途別に 3 つへ分けたもの。

    1 つの数では「State を繰り返したのか」「レビューをやり直したのか」
    「まったく同じ遷移が続いたのか」が区別できず、ログを読むときに意味が定まらない。
    """

    checkpoint_commit: str | None = None

    @property
    def from_phase(self) -> Phase | None:
        """遷移元の phase。

        §10 の項目一覧は state / substate / phase をひとまとまりで挙げており、
        これは遷移を評価した位置＝遷移元を指す。読むときに
        from_state / from_substate / from_phase と to_state / to_substate / to_phase
        が対称に見えるよう、別カラムを増やさず別名だけ用意する。
        """
        return self.phase


SUSPENSION_EVENTS: frozenset[Event] = frozenset(
    {Event.WORKER_RESOURCE_LIMIT, Event.RESOURCE_AVAILABLE, Event.HUMAN_ANSWER}
)
"""外部要因による中断と復帰。ループの証拠にはしない。

rate limit を 3 回待っただけの run は、同じところを回っているわけではない。
これらを数えると、辛抱強く待った run が LOOP_DETECTED で止まってしまう。
"""


def transition_key(
    from_state: State,
    from_substate: DocumentStage | None,
    from_phase: Phase | None,
    event: Event,
    to_state: State,
    to_substate: DocumentStage | None,
    to_phase: Phase | None,
) -> str:
    """まったく同じ遷移かどうかを判定するための指紋。"""

    def part(value: StrEnum | None) -> str:
        return value.value if value is not None else "-"

    origin = "/".join((part(from_state), part(from_substate), part(from_phase)))
    target = "/".join((part(to_state), part(to_substate), part(to_phase)))
    return f"{origin}|{event.value}|{target}"


class QuestionStatus(StrEnum):
    """QandA.md の 1 件の状態（指示書 §9）。"""

    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class Question(BaseModel):
    """Agent 間の問い合わせ 1 件（指示書 §9）。

    制御に必要な情報は SQLite に持ち、QandA.md はここから生成する。
    Markdown を状態にしない。§10 で遷移ログをそうしたのと同じ扱い。
    """

    question_id: str
    """Q-0001 形式。QandA.md と Worker への指示に出るので、読める ID にする。"""

    run_id: str
    status: QuestionStatus = QuestionStatus.OPEN

    question: str
    context: str | None = None
    """なぜ答えられないのか（§9 の Reason / Context）。"""

    answer: str | None = None
    answered_by: Worker | None = None
    related_artifacts: list[str] = Field(default_factory=list)

    asked_role: Role | None = None
    asked_worker: Worker | None = None

    source_state: State
    source_stage: DocumentStage | None = None
    source_phase: Phase | None = None

    return_state: State | None = None
    return_phase: Phase | None = None
    """回答後に戻る場所。質問した時点の位置を控えておく。"""

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def position(self) -> str:
        return "/".join(
            part
            for part in (
                self.source_state.value,
                self.source_stage.value if self.source_stage is not None else None,
                self.source_phase.value if self.source_phase is not None else None,
            )
            if part is not None
        )


class ArtifactState(BaseModel):
    """成果物 1 件の影響分析結果（指示書 §6）。"""

    run_id: str
    kind: ArtifactKind
    status: ArtifactStatus = ArtifactStatus.VALID
    path: str | None = None
    reason: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
