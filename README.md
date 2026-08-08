# Agent Controller

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

遷移表は 2 段に分かれている。トップレベルが `(State, Event) -> State`、
Document Stage の中が `(Phase, Event) -> Phase`。
stage の中の往復はトップレベル State を動かさない。

ループを止めるのは Controller の機械的な歯止めであって、AI の判断ではない。
LangGraph の `recursion_limit` はその後ろに残した最後の非常停止装置。

影響範囲は AI が提案し、Controller は依存グラフの制約に照らして検証・適用するだけ。
矛盾した提案（上流が STALE なのに下流が VALID など）は適用せず人間へ渡す。

Worker の出力は信用しない。壊れた JSON も知らない Event 名も、例外ではなく
WORKER_ERROR という遷移として記録し、生の出力を添えて人間へ渡す。

Codex CLI で SPEC.md → USECASE.md の 1 stage を実接続で通してある。
QandA.md の実体、Git checkpoint / rollback、COMPLETE gate は未実装。
詳細は `instructions/result-2026-08-09-006.md` を参照。

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
