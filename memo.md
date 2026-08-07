# Agent Controller 設計メモ

## 1. 目的

Agent Controller は、複数の AI エージェントを状態遷移で制御し、ソフトウェア開発工程を継続的・安全に進めるためのコントローラーとする。

AI エージェント自身に工程管理を任せるのではなく、Controller が現在状態・イベント・次状態を明示的に管理する。AI は各状態の処理を担当する交換可能な Worker として扱う。

主な狙いは以下。

- 長時間の AI コーディングでコンテキストが増大し続ける問題を抑える
- 設計・実装・レビューを複数 AI に分担する
- Claude Code / Codex CLI の session limit・token limit で処理が停止しても復旧できるようにする
- PC 変更、CLI 異常終了、AI 利用制限などが発生しても Git の checkpoint から再開できるようにする
- 上流成果物の変更による手戻りを State Machine で明示的に管理する
- QandA.md を介して AI 間の質問・回答・人間への問い合わせを管理する

## 2. 使用技術

- Python
- LangGraph
- Pydantic
- SQLite

LangGraph は状態遷移・Graph 実行の基盤として利用する。
Pydantic は状態・イベント・Worker 応答・設定値などの型と検証に使用する。
SQLite は実行状態、イベント履歴、Worker 状態、retry 情報、checkpoint commit などの永続化に使用する。

## 3. 使用する AI エージェント

初期対応 Worker は以下の 4 種類とする。

1. Claude Code
2. Codex CLI
3. AntiGravity
4. Grok

通常は Claude Code と Codex CLI を優先して使用する。

想定する通常運転例：

- Claude Code が実装、Codex CLI がレビュー
- Codex CLI が実装、Claude Code がレビュー

片方が session limit / token limit / rate limit 等で利用不能になった場合は、利用可能な Claude Code または Codex CLI 単独で実装・レビューを継続できるようにする。

両方とも利用不能の場合は AntiGravity、Grok を fallback Worker として選択可能にする。

例：

```text
Claude + Codex
      ↓ unavailable
Claude only / Codex only
      ↓ unavailable
AntiGravity
      ↓ unavailable
Grok
      ↓ unavailable
WAIT_RESOURCE / HUMAN_REQUIRED
```

Worker の優先順位・対応 Role は設定ファイルから変更可能にする。

## 4. 基本思想

### 4.1 AI は Worker、Controller は決定論的に動作する

AI に「次に何をするか」を自由判断させる範囲を最小化する。

```text
current_state + event -> next_state
```

という状態遷移は Controller が決定する。

AI は指定された State の仕事を実施し、成果物と Event を返す。

例：

```text
IMPLEMENT
  ↓
Codex / Claude
  ↓
DONE / QUESTION / ERROR
  ↓
Controller
```

### 4.2 ドキュメントを AI 間のインターフェースにする

基本的な設計・実装工程は以下を想定する。

```text
SPEC.md
  ↓
USECASE.md
  ↓
SEQUENCE.md
  ↓
CLASS.md
  ↓
TESTCASE.md
  ↓
IMPLEMENT
  ↓
TEST
  ↓
REVIEW
```

各 AI にプロジェクト開始時からの全会話を渡すのではなく、現在の State に必要な成果物だけを Context として渡す。

これにより、コンテキスト増大を抑制する。

## 5. 基本 State

初期案：

```text
IDLE
SPEC_CREATE
SPEC_REVIEW
USECASE_CREATE
USECASE_REVIEW
SEQUENCE_CREATE
SEQUENCE_REVIEW
CLASS_CREATE
CLASS_REVIEW
TESTCASE_CREATE
TESTCASE_REVIEW
IMPLEMENT
TEST
CODE_REVIEW
ANSWER_QUESTION
HUMAN_REQUIRED
WAIT_RESOURCE
COMPLETE
ABORT
```

将来必要に応じて State を追加する。

## 6. 基本状態遷移

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> SPEC_CREATE : NEW_TASK

    SPEC_CREATE --> SPEC_REVIEW : DONE
    SPEC_REVIEW --> USECASE_CREATE : PASS
    SPEC_REVIEW --> SPEC_CREATE : FAIL

    USECASE_CREATE --> USECASE_REVIEW : DONE
    USECASE_REVIEW --> SEQUENCE_CREATE : PASS
    USECASE_REVIEW --> USECASE_CREATE : FAIL

    SEQUENCE_CREATE --> SEQUENCE_REVIEW : DONE
    SEQUENCE_REVIEW --> CLASS_CREATE : PASS
    SEQUENCE_REVIEW --> SEQUENCE_CREATE : FAIL

    CLASS_CREATE --> CLASS_REVIEW : DONE
    CLASS_REVIEW --> TESTCASE_CREATE : PASS
    CLASS_REVIEW --> CLASS_CREATE : FAIL

    TESTCASE_CREATE --> TESTCASE_REVIEW : DONE
    TESTCASE_REVIEW --> IMPLEMENT : PASS
    TESTCASE_REVIEW --> TESTCASE_CREATE : FAIL

    IMPLEMENT --> TEST : DONE
    TEST --> CODE_REVIEW : PASS
    TEST --> IMPLEMENT : FAIL

    CODE_REVIEW --> COMPLETE : PASS
    CODE_REVIEW --> IMPLEMENT : CODE_FIX
    CODE_REVIEW --> CLASS_CREATE : CLASS_FIX
    CODE_REVIEW --> SEQUENCE_CREATE : SEQUENCE_FIX
    CODE_REVIEW --> USECASE_CREATE : USECASE_FIX
    CODE_REVIEW --> SPEC_CREATE : SPEC_FIX

    COMPLETE --> [*]
```

## 7. QandA.md

QandA.md はレビュー専用ではなく、全 Worker が利用できる AI 間の質問・回答チャネルとする。

質問を出す可能性がある Worker：

- 設計 AI
- 実装 AI
- テスト AI
- レビュー AI

質問が発生した場合：

```text
WORKING_STATE
    ↓ QUESTION
ANSWER_QUESTION
    ↓ ANSWERED
元の State に戻る
```

回答 AI は既存の設計文書から回答可能か確認する。

回答不能の場合は推測せず HUMAN_REQUIRED に遷移する。

人間回答によって仕様変更が必要になった場合、QandA.md のみに情報を残さず、SPEC.md 等の正式成果物へ反映する。

## 8. 手戻り管理

後工程で上流設計の問題が見つかった場合も、例外扱いせず正式な状態遷移として管理する。

例：

```text
CODE_REVIEW
  ↓ SPEC_FIX
SPEC_CREATE / SPEC_FIX
  ↓
SPEC_REVIEW
  ↓
必要な下流成果物を再生成・再レビュー
```

変更の影響度を分類できるようにする。

```text
CODE_ONLY
CLASS
SEQUENCE
USECASE
SPEC
```

上流成果物変更時には、依存する下流成果物を STALE として扱うことを検討する。

依存関係：

```text
SPEC
 ↓
USECASE
 ↓
SEQUENCE
 ↓
CLASS
 ↓
TESTCASE
 ↓
CODE
```

## 9. Git を checkpoint として利用する

Git commit を State Machine の安全な checkpoint として利用する。

基本ルール：

```text
State 開始
  ↓
開始時 commit SHA を記録
  ↓
AI Worker 実行
  ↓
検証
  ↓
State 成功
  ↓
commit
  ↓
push
  ↓
次 State
```

State 完了時の commit を「その State が正常完了した証拠」とする。

## 10. Token / Session Limit 対策

Claude Code や Codex CLI が以下のような利用制限で突然停止することを前提に設計する。

```text
You've hit your session limit
```

Controller は CLI 出力・終了コード等から resource limit を検出する。

異常終了した場合：

```text
Worker 実行
  ↓
SESSION_LIMIT / RATE_LIMIT
  ↓
現在 State を失敗扱い
  ↓
State 開始時 checkpoint commit まで rollback
  ↓
別 Worker 選択
  ↓
同じ State を最初から再実行
```

重要な設計方針：

**AI に token limit 直前の引き継ぎ文書作成を依存しない。**

突然停止しても Git checkpoint と State DB から安全に再実行できる設計を優先する。

## 11. State の再実行性

各 State は可能な限り以下を満たすようにする。

- State 開始時点の clean な checkpoint から再実行可能
- 同じ入力に対して再実行しても破壊的な副作用を起こさない
- State 途中の成果物は完了扱いにしない
- 成功時のみ commit / push する

長時間 State は将来サブ State 化することも検討する。

## 12. SQLite に保持する情報（案）

例：

```text
project_id
current_state
previous_state
return_state
last_event
active_worker
active_role
checkpoint_commit
retry_count
status
started_at
updated_at
```

その他：

- Event history
- Worker availability
- Worker failure reason
- State execution history
- Q&A status
- Artifact status (VALID / STALE)

## 13. Worker 抽象化

Controller が Claude / Codex / AntiGravity / Grok 固有処理に依存しすぎないよう、共通 Worker インターフェースを定義する。

概念例：

```python
class Worker:
    def available(self) -> bool:
        ...

    def run(self, role, context, instruction):
        ...

    def classify_error(self, result):
        ...
```

Role 例：

```text
SPEC_CREATOR
DESIGNER
IMPLEMENTER
REVIEWER
ANSWERER
```

## 14. Worker 選択

設定例：

```yaml
workers:
  claude:
    roles:
      - design
      - implement
      - review
    priority: 1

  codex:
    roles:
      - implement
      - review
    priority: 1

  antigravity:
    roles:
      - implement
      - review
    priority: 2

  grok:
    roles:
      - review
      - research
    priority: 3
```

初期実装では Claude Code + Codex CLI を優先し、AntiGravity / Grok は fallback とする。

## 15. Review 方針

通常時は可能な限り実装者と Reviewer を別 Worker にする。

```text
Claude IMPLEMENT -> Codex REVIEW
Codex IMPLEMENT  -> Claude REVIEW
```

片方が利用不能の場合は single-agent mode を許可する。

ただし結果には通常レビューと区別できる情報を残す。

例：

```text
PASS
PASS_WITH_SINGLE_AGENT_REVIEW
PASS_WITH_FALLBACK_REVIEW
```

## 16. Controller が AI である必要はない

工程制御、retry、rollback、Worker 切替は Python / LangGraph の決定論的処理として実装する。

AI を使用するのは、設計判断、実装、レビュー、Q&A 回答など意味理解が必要な部分に限定する。

```text
工程判断 -> Controller
内容判断 -> AI Worker
```

この責務分離を基本原則とする。

## 17. 最初の実装範囲

最初からすべてを作らず、以下の MVP を目標とする。

1. Python プロジェクト作成
2. Pydantic による State / Event モデル
3. SQLite 永続化
4. LangGraph による基本状態遷移
5. Claude Code Worker
6. Codex CLI Worker
7. Worker availability / error classification
8. Git checkpoint commit 管理
9. session limit 検出
10. rollback + Worker fallback
11. SPEC -> IMPLEMENT -> REVIEW 程度の小さな実証フロー

その後、USECASE / SEQUENCE / CLASS / TESTCASE / QandA / AntiGravity / Grok を段階的に追加する。

## 18. 今後検討すること

- Claude Code / Codex CLI の resource limit 検出方法
- 各 CLI の subprocess 実行方式
- CLI ごとの終了コード・エラーメッセージ分類
- Windows / Linux / WSL での差異
- worktree を State ごとに分離するか
- rollback 時の untracked file の扱い
- AI が勝手に commit した場合の扱い
- State ごとの timeout
- retry 上限
- Human in the Loop の UI / CLI
- 複数プロジェクト同時実行
- Worker の並列実行
- ログと実行トレースの可視化
- LangGraph の checkpoint 機能と独自 SQLite 状態管理の責務分担

## 19. 最終的なイメージ

```text
                  +------------------+
                  | Agent Controller |
                  |   State Machine  |
                  +---------+--------+
                            |
                  State / Role / Input
                            |
          +-----------------+-----------------+
          |                 |                 |
          v                 v                 v
      Claude Code       Codex CLI       Fallback Worker
                                           |
                                  AntiGravity / Grok
          |                 |                 |
          +-----------------+-----------------+
                            |
                 Artifact / QandA / Event
                            |
                            v
                  +------------------+
                  |   Controller     |
                  +--------+---------+
                           |
                    Git checkpoint
                    SQLite state
                           |
                           v
                       Next State
```

Agent Controller が継続性を持ち、AI Worker は交換可能とする。

AI が停止しても開発工程そのものは停止しない構造を目指す。

## 20. README.md の自動同期

README.md は手作業に任せると更新漏れが発生しやすいため、正式な成果物として State Machine に組み込む。

特に SPEC / USECASE レベルの変更では README 更新確認を必須とする。

変更影響度の例：

```text
CODE_ONLY      -> DOC_IMPACT = CHECK
CLASS_CHANGE   -> DOC_IMPACT = CHECK
USECASE_CHANGE -> DOC_IMPACT = REQUIRED
SPEC_CHANGE    -> DOC_IMPACT = REQUIRED
```

README 更新用 State の例：

```text
DOC_SYNC
README_REVIEW
```

完了条件では、SPEC / USECASE / SEQUENCE / CLASS / TESTCASE / CODE だけでなく README.md も最新であることを確認する。

README 更新 Worker は既存 README.md だけを根拠にせず、最新の SPEC.md、USECASE.md、実装、TESTCASE.md、変更要求等を参照して同期する。

## 21. 状態遷移ログ

状態遷移ログは Agent Controller で最優先に確認する運用ログとする。

**「どの State で、どの Event が発生し、どの State に遷移したか」**を必ず1遷移1レコードで記録する。

最低限、以下を保持する。

```text
timestamp
project_id
run_id
transition_id
from_state
event
to_state
worker
role
reason
retry_count
checkpoint_before
checkpoint_after
result
```

人間が真っ先に読むログは、詳細な AI 会話ログではなく状態遷移ログとする。

表示例：

```text
2026-08-07 16:20:12 | IMPLEMENT   | DONE          | TEST         | claude | retry=0
2026-08-07 16:21:03 | TEST        | TEST_FAIL     | IMPLEMENT    | pytest | retry=1 | reason=test_login_failed
2026-08-07 16:27:41 | IMPLEMENT   | SESSION_LIMIT | IMPLEMENT    | claude | retry=2 | rollback=abc1234
2026-08-07 16:27:43 | IMPLEMENT   | WORKER_SWITCH | IMPLEMENT    | codex  | retry=2 | reason=claude_session_limit
2026-08-07 16:31:10 | CODE_REVIEW | SPEC_FIX      | SPEC_CREATE  | codex  | retry=0 | reason=requirement_ambiguity
```

ログだけを見れば、正常に前進しているのか、どこで後戻りしたのか、何が原因だったのかを把握できるようにする。

SQLite の Event history / State execution history を正本とし、必要に応じて人間向けのテキストログにも同時出力する。

ログレベルは最低限以下を想定する。

```text
INFO    正常遷移
WARN    retry / rollback / fallback
ERROR   Worker異常 / State失敗
FATAL   継続不能 / HUMAN_REQUIRED / ABORT
```

## 22. 無限ループ防止

State Machine に循環経路を許可する一方、無制限の再試行は絶対に許可しない。

単純な retry_count だけでなく、以下の複数のガードを持つ。

### 22.1 State 単位の retry 上限

同一 State の連続失敗回数に上限を設ける。

```text
IMPLEMENT retry >= 3 -> ESCALATE / HUMAN_REQUIRED
```

上限値は State ごとに設定可能とする。

### 22.2 同一遷移の連続回数制限

例えば以下が何度も続く場合を検出する。

```text
IMPLEMENT -> TEST -> IMPLEMENT -> TEST -> ...
```

同一の State/Event/NextState パターンが一定回数を超えたら LOOP_DETECTED を発生させる。

### 22.3 一定区間内の最大遷移数

1 run あたり、または一定時間内の State transition 数に上限を設ける。

```text
MAX_TRANSITIONS_PER_RUN
MAX_TRANSITIONS_PER_HOUR
```

上限超過時は無条件に自動実行を停止し、原因解析 State または HUMAN_REQUIRED へ遷移する。

### 22.4 進捗のないループ検出

単に State が循環しているだけでなく、成果物や Git commit が実質的に進んでいない状態を検出する。

例：

```text
同じエラー
同じレビュー指摘
同じ Q&A
同じ diff
同じ test failure
```

が繰り返される場合は NO_PROGRESS と判定する。

可能であれば成果物 hash、Git diff hash、error signature、review issue ID 等を比較する。

### 22.5 Worker を替えてから諦める

同じ Worker で単純再試行を繰り返さない。

```text
Claude failure
  -> retry
  -> Claude failure
  -> Codexへ切替
  -> failure
  -> fallback Worker
  -> failure
  -> HUMAN_REQUIRED
```

Worker fallback も回数制限を持つ。

### 22.6 後戻り深度の監視

CODE_REVIEW から SPEC_CREATE まで戻ること自体は正常な設計とするが、同じ変更要求によって何度も SPEC まで戻る場合は LOOP_DETECTED とする。

```text
SPEC_FIX_COUNT_PER_CHANGE_REQUEST
```

などを記録し、一定回数を超えた場合は自動継続しない。

### 22.7 Loop検出時の遷移

```text
LOOP_DETECTED
    ↓
停止前checkpointを保存
    ↓
状態遷移ログへ原因を記録
    ↓
必要なら別Workerによる原因分析
    ↓
HUMAN_REQUIRED または WAIT_RESOURCE
```

無限ループ防止では「止めないこと」よりも「安全に止まり、なぜ止まったかがログですぐ分かること」を優先する。

## 23. 完了判定

COMPLETE へ遷移する前に最低限以下を確認する。

```text
必要な成果物が VALID
README.md が最新
TEST PASS
REVIEW PASS
未解決 Q&A なし
未処理 HUMAN_REQUIRED なし
Git working tree clean
必要な commit / push 完了
loop guard 異常なし
```

これらを満たさない場合は COMPLETE にしない。
