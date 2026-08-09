# Agent Controller

Instruction 011 traced the prior REVIEW/Q&A timeout with per-invocation diagnostics.
The root cause was the missing Main Graph REVIEW QUESTION → Director/QANDA route,
not a Codex timeout or oversized prompt. After the fix, the Codex-only live E2E
reached COMPLETE and verification is `270 passed, 4 skipped`.
Evidence: `instructions/result-2026-08-09-018.md`.

## COMPLETE gate (§17-15)

The Controller now exposes a machine-only `CompleteGate`. It checks SQLite artifact
and question state plus Git clean/commit/push state; Markdown text and Worker prose
are not used. `agent-controller --run RUN status` prints structured blocker codes.
The wired Main Graph now connects IDLE → DESIGN → IMPLEMENT → TEST → REVIEW → DOC_SYNC → COMPLETE;
the default handlers remain available as deterministic stubs for unit tests.
Claude Code, Codex CLI, Grok, and Antigravity (agy) adapters are available. Grok
and agy are read-only candidates for reviewer/director/answerer roles; only the
implementer profile is write-enabled. CLI failures retain raw/signed exit codes,
resolved executable, elapsed time, and output tails, and reviewer fallback keeps
the same State/Stage/Phase checkpoint. Live AI E2E was re-run in isolated scratch
repositories but did not reach COMPLETE: Claude's live session reported disabled
filesystem tools and the graph stopped at HUMAN_REQUIRED; see
`instructions/result-2026-08-09-014.md`. Phase-level diagnostics later showed that
the cause was the old empty Claude tool list; `Read,Glob,Grep` now permits read-only
review. A Codex-only E2E then stopped at an undecided missing-argument behavior in
the SPEC, so COMPLETE is still not claimed; see `instructions/result-2026-08-09-015.md`.

複数の AI エージェント（Claude Code / Codex CLI など）を決定論的な State Machine で制御し、
設計・実装・テスト・レビュー・文書同期の工程を継続的に進めるコントローラー。

工程遷移は Controller が決定し、AI は各 State の仕事を担当する交換可能な Worker として扱う。

```text
current_state + event -> next_state
```

## 現在の実装範囲

実装指示書 `instructions/instruction-2026-08-08-001.md` の §17 実装順のうち 1〜12 の一部まで。

- プロジェクト骨格
- Pydantic の State / Event / ArtifactStatus モデルと遷移表
- SQLite 永続化
- State Transition Logger
- 小さな Main Graph（LangGraph、各 State は stub）
- 共通 DocumentStage Subgraph（GENERATE / REVIEW_LIGHT / REVIEW_DEEP / FIX / QANDA）
- DESIGN の Progressive Refinement（SPEC → USECASE → SEQUENCE → CLASS → UI → TESTCASE）
- LIGHT / DEEP レビュー強度の初期配線
- 無限ループ防止（回数の上限と NO_PROGRESS 指紋）
- 影響範囲分析（AI の提案を依存グラフの制約で検証してから適用）
- DB スキーマのマイグレーション
- 薄い Worker interface と Claude Code / Codex CLI の Adapter
- QandA.md（Agent 間の問い合わせチャネル）
- 人間の回答から run を再開する CLI

遷移表は 2 段に分かれている。トップレベルが `(State, Event) -> State`、
Document Stage の中が `(Phase, Event) -> Phase`。
stage の中の往復はトップレベル State を動かさない。

ループを止めるのは Controller の機械的な歯止めであって、AI の判断ではない。
LangGraph の `recursion_limit` はその後ろに残した最後の非常停止装置。

影響範囲は AI が提案し、Controller は依存グラフの制約に照らして検証・適用するだけ。
矛盾した提案（上流が STALE なのに下流が VALID など）は適用せず人間へ渡す。

Worker の出力は信用しない。壊れた JSON も知らない Event 名も、例外ではなく
WORKER_ERROR という遷移として記録し、生の出力を添えて人間へ渡す。

既存の文書は上書きせず、その工程の強度でレビューから入る。
人が書いた SPEC.md を Controller が作り直すことはない。

Markdown は人間と AI が読む成果物、SQLite は制御状態。QandA.md も遷移ログも
SQLite から生成し、読み戻さない。

Codex CLI で SPEC.md → USECASE.md の 1 stage を実接続で通してある。
Worker は正式な成果物に根拠が無い判断を推測で埋めない。決まっていないことは
QUESTION として State Machine 上に現れ、AI 同士で解決できなければ人間へ上がる。

```bash
agent-controller --run RUN answer Q-0001 "禁止する" [--upstream SPEC]
```

Codex が質問し Claude が回答して工程が続く往復を実接続で確認済み。
Git checkpoint / rollback と COMPLETE gate 本体は未実装。
詳細は `instructions/result-2026-08-09-009.md` を参照。

## CLI

```bash
agent-controller --run RUN status      # run の位置と COMPLETE を阻むもの
agent-controller --run RUN questions   # 質問一覧
agent-controller --run RUN show        # QandA.md
agent-controller --run RUN answer ...  # 人間が答える
```

## セットアップ

```bash
uv sync
```

## テスト

```bash
uv run pytest
```

## v1.0 release preparation

Agent Controller is a state-machine controller for a gated workflow:

```text
IDLE -> DESIGN -> IMPLEMENT -> TEST -> REVIEW -> DOC_SYNC -> COMPLETE
```

Install the package with `uv sync`. The supported command-line entry point is
`agent-controller`; use `uv run agent-controller --help` for the complete
interface. Every command operates on an explicit run and SQLite database:

```bash
uv run agent-controller --db controller.db --run RUN status
uv run agent-controller --db controller.db --run RUN questions
uv run agent-controller --db controller.db --run RUN show
uv run agent-controller --db controller.db --run RUN answer QUESTION_ID "answer"
uv run agent-controller --db controller.db --run RUN answer-batch answers.json
```

`status` reports the current state and machine-readable COMPLETE blockers;
`questions` lists pending human questions; `answer` and `answer-batch` resume
the recorded run. The controller treats SQLite and Git state as authoritative,
including freshness, checkpoint, clean-tree, commit, and push checks.

The package metadata currently targets v1.0.0. This repository is release-ready
after the checks in `RELEASE_CHECKLIST.md`; no Git tag or hosted release is
created by this preparation step.

Known limitation: the long Claude Reviewer payload can still hit the configured
worker timeout in some local environments. Claude's short read-only Reviewer
contract and Codex fallback are covered; this known runtime limitation does not
invalidate the Codex baseline path or COMPLETE gate.

## 設計資料

- `memo.md` — 初期設計メモ
- `instructions/instruction-2026-08-08-001.md` — 実装指示書（memo.md と矛盾する場合はこちらが優先）
- `instructions/result-2026-08-08-001.md` — 実施結果（§17-1〜5）
- `instructions/result-2026-08-08-002.md` — 実施結果（§17-6）
- `instructions/result-2026-08-09-003.md` — 実施結果（§17-7 / §17-8）
- `instructions/result-2026-08-09-004.md` — 実施結果（§17-9）
- `instructions/result-2026-08-09-005.md` — 実施結果（§17-10）
- `instructions/result-2026-08-09-006.md` — 実施結果（§17-11 / §17-12 の一部）
- `instructions/result-2026-08-09-007.md` — 実施結果（入口の検査 / 指紋の構造化 / §17-13）
- `instructions/instruction-2026-08-09-001.md` / `-002.md` — 人間回答と実 Q&A の指示書
- `instructions/result-2026-08-09-008.md` — 実施結果（人間回答経路と実 AI Q&A）
- `instructions/result-2026-08-09-009.md` — 実施結果（推測禁止 Directive と指紋修正）
-
## 2026-08-09 最終安定化（instruction-012）

Main Graph の scripted E2E と Codex 基準の実AI E2E は `IDLE → DESIGN → IMPLEMENT → TEST → REVIEW → DOC_SYNC → COMPLETE` まで確認済みです。Controller は Role ごとの既定 routing（Director / Implementer / Reviewer / Answerer）と、Worker invocation の診断 JSONL（timeout 層、exit code、実行時間、prompt サイズ、stdout/stderr末尾）を提供します。

Claude / Grok / agy の実行可否は環境依存です。現在の受入構成では Codex を実装・Director・fallback先、Claude を read-only Reviewer の第一候補、Grok を read-only Reviewer/Director/Answerer候補、agy は headless CLI の権限契約が整うまで未接続としています。Claude の timeout や Q&A の再質問は隠さず `RESULT_PRODUCED_WITH_LIMITATIONS` として記録します。詳細は `instructions/result-2026-08-09-019.md` を参照してください。
-
## 2026-08-09 instruction-013 結果

Q&A回答後の再Reviewに回答を構造化して渡し、`ANSWER_ONLY` / `IMPLEMENT_CHANGE_REQUIRED` / `ARTIFACT_CHANGE_REQUIRED` / `UPSTREAM_CHANGE_REQUIRED` / `HUMAN_REQUIRED` を扱います。QandA.mdはController-owned metadataとしてDOC_SYNCでcommitします。新規scratchでClaude timeout→Codex fallback→COMPLETEを確認済みです。Claudeの有効semantic resultは未確認のため、4AI完全受入は既知制限として扱います。詳細は `instructions/result-2026-08-09-020.md`。
-
## 2026-08-09 instruction-014 Claude診断結果

Claude CLI 2.1.226の単体起動・stdin PIPE・JSON出力は成功しますが、実Reviewer payloadではtimeoutが残りました。`--tools Read,Glob,Grep`を除去し、plan modeと短縮Reviewer directiveを導入した結果、短縮payloadは3回連続PASS、Main GraphではClaude timeout後のCodex fallbackでCOMPLETEを確認しています。Claude runtime側の長いpayload不安定性は既知制限として `instructions/result-2026-08-09-021.md` に記録しています。
