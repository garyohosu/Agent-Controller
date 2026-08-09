# 引き継ぎ

このファイルだけ読めば作業を再開できることを目指す。
設計の理由は `memo.md` と `instructions/result-*.md` にある。

---

Instruction 011 traced the prior REVIEW/Q&A timeout with per-invocation diagnostics.
The root cause was the missing Main Graph REVIEW QUESTION → Director/QANDA route,
not a Codex timeout or oversized prompt. After the fix, the Codex-only live E2E
reached COMPLETE and verification is `270 passed, 4 skipped`. See
`instructions/result-2026-08-09-018.md`.

## COMPLETE gate status

§17-15 is implemented. COMPLETE requires machine-checked VALID design artifacts,
current CODE, fresh TEST and REVIEW PASS results, zero OPEN/HUMAN_REQUIRED questions,
README LATEST or NOT_REQUIRED, and a clean committed pushed Git state. The next
priority is wiring DESIGN / IMPLEMENT / TEST / REVIEW / DOC_SYNC in the Main Graph;
the wired path now connects those states; deterministic default stubs remain for isolated tests.
Live AI E2E was re-run in scratch and stopped at HUMAN_REQUIRED; COMPLETE was not reached.
Claude/Codex/Grok/agy adapters now exist. Grok and agy are read-only role candidates,
Claude/Codex use role-aware permission profiles, and reviewer fallback preserves the
same checkpoint and position. CLI diagnostics retain raw/signed exit code, executable,
elapsed time, and output tails. The latest live evidence and the DESIGN stale-State
fix are in `instructions/result-2026-08-09-014.md`.
Phase-level probes subsequently showed Claude/Codex/Grok PASS on the same SPEC deep
review; agy returned exit 0 but no structured output because headless command
permission was denied. A stage-aware handler dispatcher fixed phase-key overwriting.
The stable Codex-only E2E then stopped at HUMAN_REQUIRED on an undecided missing-name
behavior in SPEC/USECASE. COMPLETE remains unclaimed; see
`instructions/result-2026-08-09-015.md`.

## これは何か

複数の AI（Claude Code / Codex CLI）を決定論的な State Machine で制御し、
設計・実装・レビュー・文書同期を継続的に進めるコントローラー。

元々の狙いは 1 つ。**人間が ChatGPT と CLI AI の間で回答を転記する作業をなくす。**

現時点で、Codex が質問し Claude が答えて工程が続く往復は実接続で動いている。
人間が出てくるのは、どの成果物からも答えが出ない質問だけ。

---

## 絶対に崩してはいけない 5 つ

これを崩す変更は、たとえテストが通っても差し戻す。

### 1. Controller は意味を解釈しない

```text
AI Worker  →  構造化された結果  →  Controller が検証  →  遷移
```

AI は意味を判断する。Controller は Event / `upstream_target` / `finding_code` /
`impacts` を**検証して適用するだけ**。自由文を読んで判断しない。

「この曖昧さなら質問すべきだった」を Controller が自然言語解析で決めてはいけない。

### 2. 遷移表は 2 段。混ぜない

| 表 | 場所 | 引くもの |
|---|---|---|
| `TRANSITIONS` | `transitions.py` | `(State, Event) -> State` |
| `STAGE_TRANSITIONS` | `document_stage.py` | `(Phase, Event) -> Phase \| EXIT` |

stage の `PASS` は「この文書が通った」であって「DESIGN が終わった」ではない。
1 つの表にまとめると `DESIGN + PASS -> IMPLEMENT` に化け、SPEC を 1 本通しただけで
設計工程全体が終わったことになる。**stage を抜ける Event だけ**が上位へ伝播する。

### 3. トップレベル State は 10 個から増やさない

```text
IDLE  DESIGN  IMPLEMENT  TEST  REVIEW  DOC_SYNC
COMPLETE  HUMAN_REQUIRED  WAIT_RESOURCE  ABORT
```

`SPEC_CREATE` や `ANSWER_QUESTION` や `HUMAN_ANSWER_WAIT` を足したくなったら、
それは substate / phase / `return_*` で表せないか先に考える。
これまで全部表せている。

### 4. SQLite が正本、Markdown は生成物

`QandA.md` も遷移ログも SQLite から毎回まるごと生成する。**読み戻さない。**
手編集は次の更新で消える（テストで固定してある）。

### 5. ループを止めるのは Controller、AI ではない

`guards.py` の機械的な上限が止める。LangGraph の `recursion_limit` は
その後ろに置いた最後の非常停止装置であって、通常の停止装置ではない。

---

## ファイルの地図

```text
src/agent_controller/
  models.py          State / Event / Phase / Question / RunState など型だけ
  transitions.py     トップレベル遷移表 + apply_event（純 Python、中核）
  store.py           SQLite。runs / transitions / artifacts / questions / fingerprints
  migrations.py      schema マイグレーション（現在 v2）
  transition_log.py  遷移の記録と人間向けレンダリング
  graph.py           小さな Main Graph（LangGraph）。各 State は今も stub
  document_stage.py  共通 DocumentStage Subgraph（GENERATE/REVIEW/FIX/QANDA）
  design.py          DESIGN の Progressive Refinement（素の Python ループ）
  impact.py          影響範囲分析（AI の提案を依存グラフ制約で検証）
  guards.py          無限ループ防止（回数上限 + NO_PROGRESS 指紋）
  qanda.py           QandA.md の生成と質問の一生
  worker.py          Worker interface + Directive（推測禁止はここ 1 箇所）
  cli_worker.py      Claude Code / Codex CLI の Adapter
  human.py           人間の回答から run を再開する
  cli.py             argparse だけの薄い皮
```

コード 4400 行 / テスト 4200 行。テスト 241 件、外部 AI 非依存で完走する。

---

## 動かし方

```bash
uv sync
uv run pytest -q          # 241 passed。AI CLI は不要

agent-controller --run RUN status      # 位置と COMPLETE を阻むもの
agent-controller --run RUN questions   # 質問一覧
agent-controller --run RUN answer Q-0001 "禁止する" [--upstream SPEC]
```

実 AI を使う script は scratchpad に置く。**リポジトリ本体の成果物を smoke test で
書き換えない。** workspace は必ず scratchpad 配下にする。

---

## 実際に走らせて分かったこと

scripted test では出ず、実 AI を繋いで初めて出た。同じ穴を掘り直さないこと。

### Worker は放っておくと推測して進む

Codex は仕様に穴があっても `QUESTION` を返さず、自分で埋めて `DONE` を返した。
Adapter の不具合ではなく、Directive が縛っていなかった。

`worker.py` の `NO_GUESSING_RULE` / `REVIEWER_RULE` がその対策。
**Adapter 側に固定文を書き足さないこと**（テストで禁止してある）。

### 指紋は自由文で作ってはいけない

実 AI の `reason` は毎回文言が変わる。同じ指摘でも別物として数えられ、
`NO_PROGRESS` が効かなくなる。

```text
機械判定 → finding_code / finding_subject（AI が返す）
          + state / substate / phase / event（Controller が知っている）
人間向け → reason
```

`artifact` や `phase` を AI に自己申告させない。Controller が既に知っているので、
言わせると不一致を処理する仕事が増えるだけ。

質問だけは phase を指紋から外してある。同じ質問は誰がどの phase で出しても同じ質問。

### 復帰位置は「質問した工程」

`run.return_phase` は **stage を抜けた場所**（QANDA）であって、戻るべき場所ではない。
そこへ戻すと答え終わった質問をもう一度探すだけになる。
`questions` 行が持つ位置を正とする。

### 中断はループではない

`WORKER_RESOURCE_LIMIT` / `RESOURCE_AVAILABLE` / `HUMAN_ANSWER` はカウンタを増やさない。
rate limit を 3 回待っただけの run は同じところを回っていない。

### Windows で CLI が起動できない

npm 由来の CLI は `codex`（sh スクリプト）と `codex.cmd`（ランチャ）の両方で置かれ、
`CreateProcess` は PATHEXT を見ない。`shutil.which` で解決している。

### 人が書いた文書を上書きしない

`run_design(workspace=...)` を渡すと、既にある文書は生成せずレビューから入る。
渡さないと全工程を生成から始める（＝ SPEC.md を作り直す）。**実運用では必ず渡す。**

判定は artifact 行の有無で行う。行が無い = 外部から与えられた文書 = 保護。
行がある = Controller 管理下 = STALE なら再生成。

---

## 済んでいること

指示書 `instruction-2026-08-08-001.md` §17 の 1〜13、および
`instruction-2026-08-09-001.md` / `-002.md`。

- State / Event モデルと 2 段の遷移表
- SQLite 永続化とマイグレーション（v2）
- 状態遷移ログ（人間向けレンダリング付き）
- 共通 DocumentStage Subgraph
- DESIGN の段階的詳細化（SPEC → … → TESTCASE）
- LIGHT / DEEP レビュー強度
- 影響範囲分析（VALID / REVIEW_REQUIRED / STALE、依存グラフ制約で検証）
- 無限ループ防止（回数上限 + NO_PROGRESS 指紋）
- Worker interface と Claude Code / Codex CLI Adapter
- QandA.md と人間回答からの復帰
- 推測禁止 Directive

---

## 済んでいないこと

優先度順。

1. **§17-14 Git checkpoint / rollback。**
   `checkpoint_commit` は記録しているが、実際に commit も rollback もしていない。
   `files_changed` は Worker の自己申告で未検証。ここが入ると
   「session limit で落ちても安全に再開」が本当に成立する。
2. **§17-15 COMPLETE gate 本体の配線。**
   `human.complete_blockers()` は Q&A の条件だけ実装済み。
   残り（CODE latest / TEST PASS / tree clean など）は `COMPLETE_TODO` に名前だけ。
3. **Main Graph の DESIGN node が stub のまま。** `run_design` を呼んでいない。
   IMPLEMENT / TEST / REVIEW / DOC_SYNC も stub。設計工程しか実接続していない。
4. **Director がいない。** QANDA の指示は質問文を貼り付けるだけで、
   読むべき成果物を選んでいない。
5. **依存グラフが一直線。** UI の入力は実際には USECASE/SPEC なのに、
   順序上 SEQUENCE/CLASS の下流として扱われる。安全側に倒れるので急がない。
6. **実 AI で `CANNOT_ANSWER` を出させていない。** 仕込むと入力いじりになるので、
   自然に未決事項が出たときに確認する。

---

## 作業の進め方

この 3 往復で回してきた。

```text
1. instructions/instruction-YYYY-MM-DD-NNN.md を読む
2. 実装 + テスト
3. instructions/result-YYYY-MM-DD-NNN.md に記録
4. commit + push、git status --short が clean
```

result には**判断した理由**と**うまくいかなかったこと**を必ず書く。
「実 AI が質問しなかった」を成功扱いにしない。

指示書に無い設計判断をしたら、その理由を result に書く。
これまでの主な判断は各 result の「判断」節にある。

### やってはいけないこと

- 実 AI が期待する Event を返すまで入力を変えて回す
- session limit を `WORKER_ERROR` と混同する
- `QandA.md` を制御状態の正本に戻す
- 通常の `pytest` を外部 AI CLI 必須にする
- smoke test で正式な `SPEC.md` 等を書き換える

---

## 読む順番

```text
hikitsugi.md（これ）
README.md                                  現在の実装範囲
memo.md                                    最初の設計メモ
instructions/instruction-2026-08-08-001.md 本体の指示書（memo と矛盾したらこちら優先）
instructions/result-2026-08-09-006.md      実 Worker 接続で分かったこと
instructions/result-2026-08-09-009.md      推測禁止と指紋の話
```

`result-2026-08-08-001.md` から順に読むと、なぜこの形になったかが追える。
急ぐなら 006 と 009 だけでよい。
-
## 2026-08-09 最終安定化の引き継ぎ

Codex基準の実AI Main Graph E2Eは COMPLETE 到達を確認済み。Role routing は Director / Implementer / Reviewer / Answerer ごとに固定し、Reviewer は Claude → Codex → Grok の順でfallbackします。Worker timeout と外側Harness timeoutは別々に診断し、各 invocation の診断JSONLをGit workspace外へ保存します。

4AIの受入状況は、Codexが安定した実装・テスト・レビュー・Director経路、Claude/Grokがread-only候補、agyが環境権限解消待ちです。Claude timeout、Grok timeout、agyのheadless permission error、Q&A再質問による未完走は既知制限であり、COMPLETEを偽装しません。最終判定と証拠は `instructions/result-2026-08-09-019.md` に集約します。
-
## instruction-013 受入状況

前回のQ&A再質問ループは、回答付きReview directiveと構造化action routingで修正済みです。初回ReviewでQandA.mdを必須にせず、質問発生後だけ入力へ追加します。QandA.mdのcontroller commitも追加しました。Claudeは30秒・120秒ともtimeoutしましたが、Codex fallbackで新規scratchのCOMPLETE到達を確認しています。2種類のAIが有効結果を採用する受入は未達で、環境依存のPOST_MVPとして記録します。
