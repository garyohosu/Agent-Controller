# Agent Controller v1.0.0

Agent Controller v1.0.0 is the first release of the gated, SQLite-backed
state-machine controller for Director, Implementer, Reviewer, and Answerer
workers.

## Highlights

- Main Graph workflow: `IDLE -> DESIGN -> IMPLEMENT -> TEST -> REVIEW -> DOC_SYNC -> COMPLETE`
- Machine-checked CompleteGate based on artifact freshness, tests, review, Q&A, README, and Git state
- Git checkpoint, rollback, dirty-tree protection, and Worker fallback
- Structured Q&A routing, provisional decisions, and answer batching
- Claude Code, Codex CLI, Grok, and agy adapter/capability routing
- Official `agent-controller` CLI with `status`, `questions`, `show`, `answer`, and `answer-batch`

## Validation

- `272 passed, 4 skipped` in the normal test suite
- Fresh-clone-like `uv sync`, CLI help, and test verification passed
- Codex baseline real-AI Main Graph E2E reached COMPLETE
- Git checkpoint/fallback, Q&A, CompleteGate, and scripted E2E paths are covered

## Known limitation

The long Claude Reviewer payload can reach the configured worker timeout in
some local environments. The short read-only Reviewer contract and Codex
fallback remain available, and the controller does not misclassify this as a
successful Claude result. This is a documented runtime limitation rather than
a release blocker.

## Installation

```bash
uv sync
uv run agent-controller --help
uv run pytest
```

No breaking API migration is required for this release. No data migration is
required beyond the controller's existing SQLite migration path.
