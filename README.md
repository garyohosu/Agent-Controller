# Agent Controller

複数の AI エージェント（Claude Code / Codex CLI など）を決定論的な State Machine で制御し、
設計・実装・テスト・レビュー・文書同期の工程を継続的に進めるコントローラー。

工程遷移は Controller が決定し、AI は各 State の仕事を担当する交換可能な Worker として扱う。

```text
current_state + event -> next_state
```

## 現在の実装範囲

実装指示書 `instructions/instruction-2026-08-08-001.md` の §17 実装順のうち 1〜5 まで。

- プロジェクト骨格
- Pydantic の State / Event / ArtifactStatus モデルと遷移表
- SQLite 永続化
- State Transition Logger
- 小さな Main Graph（LangGraph、各 State は stub）

AI Worker への実接続、DocumentStage Subgraph、IMPACT_ANALYSIS、loop guard、
Git checkpoint / rollback は未実装。詳細は `instructions/result-2026-08-08-001.md` を参照。

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
- `instructions/result-2026-08-08-001.md` — 実施結果
