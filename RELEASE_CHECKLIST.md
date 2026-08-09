# Agent Controller v1.0 Release Checklist

Status: `V1_RELEASE_READY_WITH_KNOWN_LIMITATIONS`

## Package and CLI

- [x] `pyproject.toml` declares package `agent-controller`, version `1.0.0`, and the `agent-controller` entry point.
- [x] `uv run agent-controller --help` works from the repository.
- [x] `status`, `questions`, `show`, `answer`, and `answer-batch` are implemented through the same SQLite-backed CLI.

## Controller acceptance

- [x] Main Graph path and scripted/fake E2E tests pass.
- [x] CompleteGate checks freshness, Q&A blockers, README status, and Git state.
- [x] Git checkpoint, rollback, dirty-tree protection, and Worker fallback tests pass.
- [x] Codex baseline real-AI Main Graph E2E reaches COMPLETE.
- [x] Normal pytest remains independent of external AI availability.

## Documentation and security

- [x] README documents installation, the supported CLI, workflow, and known limitation.
- [x] `hikitsugi.md` and historical result files are retained as evidence; historical notes are not treated as current implementation status.
- [x] Diagnostic JSONL is outside the repository and is audited for credentials before release.
- [x] Fresh-clone-like package import and CLI smoke checks pass.

## Known limitation and release boundary

- [x] Claude long Reviewer-payload timeout is recorded as a known limitation and is not silently relabeled as success.
- [x] No v1.0.0 Git tag or GitHub Release is created by this preparation task.
- [ ] A maintainer may create the tag/release after reviewing this checklist and `instructions/result-2026-08-09-022.md`.
