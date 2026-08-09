# Agent Controllerをv1.0.0まで仕上げた記録

## AIに仕事を任せるとき、最後に必要になるもの

AIに設計や実装を任せる仕組みを作ると、最初は「強いモデルを呼べば解決する」と考えがちです。

しかし、実際に複数のAI CLIを組み合わせて動かしてみると、難しいのは個々の回答品質だけではありません。

- どの状態で停止したのか
- どの成果物が最新なのか
- テストとレビューが本当に実行されたのか
- AIが失敗したとき、どこから再開するのか
- 人間の判断が必要な質問をどう扱うのか
- 最終的に、本当に完成と言ってよいのか

この問題に取り組むため、State Machineベースの `Agent Controller` を開発しました。

2026年8月、最初の正式版として v1.0.0 を公開しました。

GitHub: https://github.com/garyohosu/Agent-Controller

## Agent Controllerとは

Agent Controllerは、Director、Implementer、Reviewer、AnswererなどのWorkerを、状態機械とSQLiteで管理するControllerです。

基本のMain Graphは次の流れです。

```text
IDLE
  -> DESIGN
  -> IMPLEMENT
  -> TEST
  -> REVIEW
  -> DOC_SYNC
  -> COMPLETE
```

AIは判断や作業を担当します。一方、Controllerは状態遷移、成果物の鮮度、質問、人間への引き継ぎ、Git状態を管理します。

ここで重要なのは、AIの自由文をそのまま「成功」とみなさないことです。最終判定はSQLiteに記録された状態と、Gitやテストの実状態を使って機械的に行います。

## 最初にぶつかった壁

開発初期は、設計上の経路が用意されていても、実際にはstubが多く残っていました。

DESIGNから先の処理を実配線し、Workerを呼び、テストを実行し、レビューを行い、ドキュメントを同期してCOMPLETE gateを通す必要があります。

単純なhappy pathだけなら、Fake Workerで簡単に通せます。しかし、実AIを接続すると状況が変わります。

- CLIの終了コードが環境によって異なる
- timeoutがどの層で発生したのか分からない
- Claudeがfilesystem tools無効で停止する
- Workerが変更したファイルを自己申告しても信用できない
- Q&Aの質問が一問ずつ再発して処理が収束しない
- DOC_SYNCのpush先がbare repositoryでないと失敗する

つまり「AIを呼べた」ことと「安全に工程を完了できた」ことは別でした。

## Git checkpointを正規経路にした

実装Workerを開始する前に、ControllerがGit checkpointを保存するようにしました。

Workerが途中で落ちた場合は、次の情報を使って再開します。

1. Worker開始時のcommit SHAを保存する
2. dirty treeを検出して保護する
3. Worker終了後のGit実状態から変更ファイルを取得する
4. 必要なら開始時SHAへrollbackする
5. 同じState / Stage / Phaseから再開する

Workerの自己申告する `files_changed` だけを信頼しない点も重要です。Controllerが `git status` やcommitの実状態を確認することで、報告と実体の食い違いを検出できます。

また、`WORKER_RESOURCE_LIMIT` と `WORKER_ERROR` を分離しました。

リソース制限なら別Workerへのfallbackや再試行が考えられます。一方、実行エラーや不正な出力なら、同じ条件で繰り返すだけでは解決しません。原因に応じて、rollback、人間への引き継ぎ、別Roleへの切り替えを選びます。

## Q&Aは「一問ごとに停止」しない

AIが仕様上の不明点を見つけたとき、従来の単純な設計では次のようになっていました。

```text
QUESTION -> HUMAN_REQUIRED
```

これでは、低リスクで可逆的な判断まで人間を止めてしまいます。さらに、一つ回答すると同じReviewで別の質問が発生することもありました。

そこで、質問を分類するようにしました。

- `LOW_RISK_REVERSIBLE`
- `HIGH_RISK_PRODUCT_DECISION`
- `SECURITY_OR_SAFETY`

低リスクかつ可逆的なものは、正式仕様とは区別した「暫定判断」としてSQLiteに保存し、処理を続行できます。

高リスクの質問は可能な限りまとめ、`answer-batch` で一度に回答できます。

ただし、`CANNOT_ANSWER` を無条件にPASSへ変えることは禁止しています。暫定判断には正式仕様と異なるライフサイクルを持たせ、通常運用のCompleteGateでは未承認の暫定判断をblockerにできます。

## 4AI対応で学んだこと

OracleCouncilでの実績をもとに、Claude、Codex、Grok、agyのAdapterを扱える構成にしました。

ただし、「4AIに対応した」と「4AIすべてが同じRoleで安定稼働する」は別の意味です。そこでCapabilityとRoleを分離し、最初はread-only Reviewerなど安全なRoleから利用します。

Reviewerが失敗した場合も、すぐ `HUMAN_REQUIRED` にするのではなく、次のAIへfallbackできます。

```text
Claude Reviewer
  -> timeout
  -> Codex Reviewer
  -> PASS
```

これは、AIごとの実行環境や権限が異なる現実に合わせた設計です。Workerが使えない場合に工程全体を止めるのではなく、Controllerが理由を分類して次の経路を選択します。

## Claudeのtimeoutをどう扱ったか

実AI E2Eでは、Claudeの長いReviewer payloadが120秒のtimeoutに到達するケースがありました。

最初はtimeoutを長くすればよいのではないかと考えました。しかし、それだけでは原因が隠れるだけです。

そこで、次の診断を行いました。

- CLI単体の最小probe
- Controller経由のcommandとの差分比較
- `--tools`、permission mode、stdin、argvの切り分け
- stdout/stderrのサイズと末尾
- first outputまでの時間
- process treeと子プロセス残留
- 単純promptからReviewer payloadまでの段階的な再現

その結果、Claude CLI自体が常に使えないわけではなく、短いread-only Reviewer contractでは構造化されたPASSを返せることを確認しました。一方、長い通常payloadではローカル環境依存のtimeoutが残りました。

この問題を成功扱いに偽装せず、既知制限として公開しました。Codex fallbackで処理を継続でき、CompleteGateを通過できるため、v1.0.0の公開を止める条件にはしないと判断しています。

## COMPLETE gateが最後の関門になる

ControllerがCOMPLETEへ入る条件は、AIの「完了しました」という文章ではありません。

最低限、次を機械的に確認します。

- 設計成果物がVALID
- CODEが最新
- TESTがPASS
- REVIEWがPASS
- Q&AのOPENが0
- `HUMAN_REQUIRED` が0
- READMEがLATEST、または更新不要と判定済み
- Git working treeがclean
- commit済み
- push済み

古いTEST PASSやREVIEW PASSを使い回さないよう、現在のCODEとの鮮度も確認します。

このgateがあることで、途中まで動いたことと、公開可能な状態であることを分けられます。

## v1.0.0で確認したこと

v1.0.0公開前に、次を確認しました。

- 通常テスト: `272 passed, 4 skipped`
- fresh clone相当で `uv sync` が成功
- fresh clone相当でCLI helpが動作
- `status`、`questions`、`show`、`answer`、`answer-batch` を実動確認
- Codex基準の実AI Main Graph E2EがCOMPLETEへ到達
- Git checkpoint、rollback、fallback、Q&A、CompleteGateのテストがPASS
- 診断ログに秘密情報らしい値を残していないことを監査
- Git tag `v1.0.0` を作成してpush
- GitHub Release `v1.0.0` を公開

## 使い始める

```bash
git clone https://github.com/garyohosu/Agent-Controller.git
cd Agent-Controller
uv sync
uv run agent-controller --help
uv run pytest
```

runの状態確認や人間回答は、SQLite DBとrun IDを指定して行います。

```bash
uv run agent-controller --db controller.db --run RUN status
uv run agent-controller --db controller.db --run RUN questions
uv run agent-controller --db controller.db --run RUN answer QUESTION_ID "answer"
uv run agent-controller --db controller.db --run RUN answer-batch answers.json
```

## おわりに

このプロジェクトで一番大きかった気づきは、AI Controllerの価値は「AIをたくさん呼べること」ではなく、「AIが失敗しても状態を壊さず、証拠を残し、再開できること」にあるという点です。

モデルの性能やCLIの仕様は変わります。だからこそ、Controller側にはcheckpoint、鮮度、fallback、質問分類、機械的なCompleteGateが必要になります。

v1.0.0は完成の終点ではありません。しかし、AIに工程を任せるための最低限の安全弁を、設計だけでなくGitと実行ログを使う実装として閉じることができました。

今後は、Claudeの長いReviewer payloadの安定化、4AIのRole別受入範囲の拡大、さらに実運用に近いE2Eの蓄積を進めていきます。

## 追記: 実利用で見つかった入口の不足

v1.0.0を実際に使った結果、既存runを操作するCLIはあっても、新しいrunを公開CLIから作る入口がないことが分かりました。

この問題をv1.0.1で修正し、次のコマンドを追加しました。

```bash
uv run agent-controller \
  --workspace C:\\project\\testproject \
  --db controller.db \
  --run TODO001 \
  init --request "PythonでCLI版TODOアプリを作る"
```

`init` はworkspaceがGit repositoryであること、clean treeであること、run IDが未使用であることを確認します。初期要求はSQLiteに正式な入力として保存され、`IDLE -> DESIGN` の遷移が記録されます。これにより、内部APIへ迂回せず、公開CLIから新しい作業を始められるようになりました。
