## 2026-08-17 13:52 Dreamingタイム

### 今回やったこと
- Claude Code の「2分11秒で rate limit」を `WORKER_RESOURCE_LIMIT` の回帰テストに追加。
- Claude Code 失敗後に Codex CLI へ fallback し、同じ checkpoint から成功することを確認。
- 全体テストを実行し、316 passed、4 skipped を確認。

### 気づいたこと
- adapter は rate limit の理由を正規化し、元の stderr は診断ログに保持している。
- Recovery log の正本イベント名は `WORKER_RESOURCE_LIMIT` だった。

### 改善点
- 実 CLI の利用制限を終了コードや構造化エラーでも分類できるようにすると、文字列依存を減らせる。

### 次に試すとよさそうなこと
- 旧 Run 完了後、needs-detector の複数 stage field regression を専用 DB で再実行し、HUMAN_REQUIRED 数を比較する。
