# Agent Controller

Instruction 010 completed the final implementation-gap audit. REVIEW findings now
become the next IMPLEMENT directive, README freshness is checked by CompleteGate,
and DOC_SYNC accepts a controller-owned sync callback. Offline verification is
`269 passed, 4 skipped`. The audit result is
`instructions/result-2026-08-09-017.md`; the post-audit live E2E remains limited by
a Codex REVIEW QUESTION timeout and is not claimed as complete.

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
