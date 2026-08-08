"""Worker interface と CLI Adapter のテスト（§17-11B / §17-11C）。

一番大事なのは「壊れた Worker 出力で Controller が落ちない」こと。
scripted stub は書いた本人が形を保証していたので例外を投げてよかったが、
subprocess の出力にその保証は無い。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_controller.cli_worker import (
    ClaudeCodeWorker,
    CodexCliWorker,
    CommandResult,
    build_prompt,
    extract_json,
    looks_like_a_resource_limit,
    result_from_output,
)
from agent_controller.document_stage import (
    DocumentStageConfig,
    run_document_stage,
    stage_completed,
)
from agent_controller.models import (
    DocumentStage,
    Event,
    Phase,
    Role,
    RunState,
    State,
    Worker,
)
from agent_controller.transition_log import TransitionLogger
from agent_controller.worker import (
    PHASE_ROLES,
    WorkerRequest,
    WorkerResult,
    phase_handlers_from_worker,
    validate_worker_result,
)

CLASS_STAGE = DocumentStageConfig(
    name=DocumentStage.CLASS, inputs=["SEQUENCE.md"], output="CLASS.md"
)


def make_request(**overrides) -> WorkerRequest:
    defaults = dict(
        role=Role.IMPLEMENTER,
        state=State.DESIGN,
        stage=DocumentStage.CLASS,
        phase=Phase.GENERATE,
        workspace=".",
        input_artifacts=["SEQUENCE.md"],
        output_artifact="CLASS.md",
        directive="write it",
        allowed_events=[Event.DONE, Event.QUESTION, Event.UPSTREAM_CHANGE_REQUIRED],
    )
    return WorkerRequest(**{**defaults, **overrides})


@pytest.fixture
def design_run(logger: TransitionLogger) -> RunState:
    run = RunState(
        project_id="agent-controller", run_id="run-worker", current_state=State.DESIGN
    )
    logger.store.save_run(run)
    return run


def fake_runner(stdout: str = "", exit_code: int = 0, stderr: str = "", timed_out: bool = False):
    def runner(argv, cwd, timeout, stdin_text):
        runner.calls.append((list(argv), cwd, timeout, stdin_text))
        return CommandResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out
        )

    runner.calls = []
    return runner


class TestJsonExtraction:
    def test_fenced_block(self) -> None:
        text = 'Here you go.\n```json\n{"event": "DONE"}\n```\nHope that helps.'
        assert extract_json(text) == {"event": "DONE"}

    def test_bare_object_at_the_end(self) -> None:
        assert extract_json('blah blah\n{"event": "PASS", "reason": "ok"}') == {
            "event": "PASS",
            "reason": "ok",
        }

    def test_prefers_the_last_object(self) -> None:
        text = '{"event": "FAIL"}\nactually:\n{"event": "PASS"}'
        assert extract_json(text) == {"event": "PASS"}

    def test_nested_objects_survive(self) -> None:
        text = '```json\n{"event": "DONE", "meta": {"a": {"b": 1}}}\n```'
        assert extract_json(text) == {"event": "DONE", "meta": {"a": {"b": 1}}}

    def test_no_json_is_none(self) -> None:
        assert extract_json("I could not do it, sorry.") is None

    def test_broken_json_is_none(self) -> None:
        assert extract_json('{"event": "DONE",}') is None


class TestResultFromOutput:
    def test_reads_the_event(self) -> None:
        result = result_from_output('```json\n{"event": "DONE", "reason": "wrote it"}\n```', "raw")
        assert result.event == Event.DONE
        assert result.reason == "wrote it"

    def test_unknown_event_becomes_worker_error(self) -> None:
        result = result_from_output('{"event": "LOOKS_GOOD_TO_ME"}', "raw")
        assert result.event == Event.WORKER_ERROR
        assert "LOOKS_GOOD_TO_ME" in (result.reason or "")

    def test_missing_json_becomes_worker_error(self) -> None:
        result = result_from_output("I am done!", "I am done!")
        assert result.event == Event.WORKER_ERROR
        assert result.raw_output == "I am done!"

    def test_usage_limit_text_becomes_resource_limit(self) -> None:
        raw = "Error: you have hit your usage limit, try again later"
        result = result_from_output(raw, raw)
        assert result.event == Event.WORKER_RESOURCE_LIMIT

    def test_resource_limit_patterns(self) -> None:
        assert looks_like_a_resource_limit("HTTP 429 Too Many Requests")
        assert looks_like_a_resource_limit("Rate limit reached")
        assert not looks_like_a_resource_limit("syntax error on line 3")


class TestValidation:
    def test_disallowed_event_is_rejected(self) -> None:
        request = make_request(allowed_events=[Event.DONE])
        problem = validate_worker_result(WorkerResult(event=Event.PASS), request)
        assert problem is not None
        assert "not allowed here" in problem.message

    def test_upstream_change_needs_a_target(self) -> None:
        problem = validate_worker_result(
            WorkerResult(event=Event.UPSTREAM_CHANGE_REQUIRED), make_request()
        )
        assert problem is not None
        assert "upstream_target" in problem.message

    def test_upstream_target_must_be_a_stage(self) -> None:
        problem = validate_worker_result(
            WorkerResult(
                event=Event.UPSTREAM_CHANGE_REQUIRED,
                structured_result={"upstream_target": "ARCHITECTURE"},
            ),
            make_request(),
        )
        assert problem is not None
        assert "not a document stage" in problem.message

    def test_a_good_result_passes(self) -> None:
        assert (
            validate_worker_result(
                WorkerResult(
                    event=Event.UPSTREAM_CHANGE_REQUIRED,
                    structured_result={"upstream_target": "SEQUENCE"},
                ),
                make_request(),
            )
            is None
        )


class TestBridge:
    def test_roles_follow_the_phase(self) -> None:
        assert PHASE_ROLES[Phase.GENERATE] == Role.IMPLEMENTER
        assert PHASE_ROLES[Phase.REVIEW_LIGHT] == Role.REVIEWER
        assert PHASE_ROLES[Phase.QANDA] == Role.DIRECTOR

    def test_implementer_and_reviewer_can_be_different_workers(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """指示書 §13: 可能なら Implementer と Reviewer は別 Worker。"""
        implementer = CodexCliWorker(runner=fake_runner('{"event": "DONE"}'))
        reviewer = ClaudeCodeWorker(
            runner=fake_runner(json.dumps({"result": '{"event": "PASS"}'}))
        )

        handlers = phase_handlers_from_worker(
            {
                Role.IMPLEMENTER: implementer,
                Role.REVIEWER: reviewer,
                Role.DIRECTOR: reviewer,
                Role.ANSWERER: reviewer,
                Role.CONTROLLER: reviewer,
            },
            workspace=".",
            input_artifacts=["SEQUENCE.md"],
            output_artifact="CLASS.md",
        )

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert stage_completed(final, CLASS_STAGE)
        workers = [
            item.worker for item in logger.history(final.run_id) if item.worker is not None
        ]
        assert Worker.CODEX_CLI in workers
        assert Worker.CLAUDE_CODE in workers

    def test_malformed_output_becomes_worker_error_not_a_crash(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """壊れた出力で graph の node が例外を出したら、遷移も記録されず人間にも渡らない。"""
        worker = CodexCliWorker(runner=fake_runner("I wrote the file! Trust me."))
        handlers = phase_handlers_from_worker(
            worker, workspace=".", input_artifacts=[], output_artifact="CLASS.md"
        )

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        last = logger.history(final.run_id)[-1]
        assert last.event == Event.WORKER_ERROR

    def test_upstream_without_target_becomes_worker_error(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """本物の AI がやりがちな抜け。例外ではなく遷移として扱う。"""
        worker = CodexCliWorker(
            runner=fake_runner('{"event": "UPSTREAM_CHANGE_REQUIRED", "reason": "bad seq"}')
        )
        handlers = phase_handlers_from_worker(
            worker, workspace=".", input_artifacts=[], output_artifact="CLASS.md"
        )

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        last = logger.history(final.run_id)[-1]
        assert last.event == Event.WORKER_ERROR
        assert "upstream_target" in (last.reason or "")

    def test_a_good_upstream_target_is_carried_through(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        worker = CodexCliWorker(
            runner=fake_runner(
                '{"event": "DONE"}'  # GENERATE
            )
        )

        def runner(argv, cwd, timeout, stdin_text):
            if "REVIEW" in stdin_text:
                return CommandResult(
                    exit_code=0,
                    stdout='{"event": "UPSTREAM_CHANGE_REQUIRED", '
                    '"upstream_target": "SEQUENCE", "reason": "seq is wrong"}',
                )
            return CommandResult(exit_code=0, stdout='{"event": "DONE"}')

        worker.runner = runner
        handlers = phase_handlers_from_worker(
            worker, workspace=".", input_artifacts=[], output_artifact="CLASS.md"
        )

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.pending_upstream_stage == DocumentStage.SEQUENCE
        last = logger.history(final.run_id)[-1]
        assert last.event == Event.UPSTREAM_CHANGE_REQUIRED

    def test_disallowed_event_for_the_phase_is_rejected(
        self, logger: TransitionLogger, design_run: RunState
    ) -> None:
        """GENERATE は PASS を返せない。返してきたら受け付けない。"""
        worker = CodexCliWorker(runner=fake_runner('{"event": "PASS"}'))
        handlers = phase_handlers_from_worker(
            worker, workspace=".", input_artifacts=[], output_artifact="CLASS.md"
        )

        final = run_document_stage(design_run, CLASS_STAGE, logger, handlers)

        assert final.current_state == State.HUMAN_REQUIRED
        assert "not allowed here" in (logger.history(final.run_id)[-1].reason or "")


class TestProcessFailures:
    def test_non_zero_exit_is_a_worker_error(self) -> None:
        worker = CodexCliWorker(runner=fake_runner(exit_code=2, stderr="boom"))
        result = worker.run(make_request())
        assert result.event == Event.WORKER_ERROR
        assert "exited with 2" in (result.reason or "")

    def test_non_zero_exit_with_a_limit_message_waits_instead(self) -> None:
        worker = CodexCliWorker(
            runner=fake_runner(exit_code=1, stderr="429 rate limit exceeded")
        )
        result = worker.run(make_request())
        assert result.event == Event.WORKER_RESOURCE_LIMIT

    def test_timeout_is_a_worker_error(self) -> None:
        worker = CodexCliWorker(runner=fake_runner(timed_out=True, exit_code=-1))
        result = worker.run(make_request())
        assert result.event == Event.WORKER_ERROR
        assert "timed out" in (result.reason or "")


class TestCommands:
    def test_codex_runs_non_interactively_in_the_workspace(self, tmp_path: Path) -> None:
        runner = fake_runner('{"event": "DONE"}')
        worker = CodexCliWorker(runner=runner)
        worker.run(make_request(workspace=str(tmp_path)))

        argv, cwd, _timeout, stdin_text = runner.calls[0]
        assert argv[:2] == ["codex", "exec"]
        assert "--sandbox" in argv and "workspace-write" in argv
        assert "--skip-git-repo-check" in argv
        assert cwd == str(tmp_path)
        assert "CLASS.md" in stdin_text

    def test_claude_runs_non_interactively(self, tmp_path: Path) -> None:
        runner = fake_runner(json.dumps({"result": '{"event": "DONE"}'}))
        worker = ClaudeCodeWorker(runner=runner)
        result = worker.run(make_request(workspace=str(tmp_path)))

        argv, _cwd, _timeout, _stdin = runner.calls[0]
        assert argv[0] == "claude"
        assert "-p" in argv
        assert "--output-format" in argv and "json" in argv
        assert result.event == Event.DONE

    def test_claude_envelope_without_result_falls_back_to_stdout(self) -> None:
        worker = ClaudeCodeWorker(runner=fake_runner('{"event": "DONE"}'))
        assert worker.run(make_request()).event == Event.DONE


class TestPrompt:
    def test_names_the_documents_and_the_allowed_events(self) -> None:
        prompt = build_prompt(make_request())
        assert "SEQUENCE.md" in prompt
        assert "CLASS.md" in prompt
        assert "DONE" in prompt
        assert "PASS" not in prompt.split("must be exactly one of:")[1]

    def test_states_the_role_and_position(self) -> None:
        prompt = build_prompt(make_request(role=Role.REVIEWER, phase=Phase.REVIEW_LIGHT))
        assert "REVIEWER" in prompt
        assert "REVIEW_LIGHT" in prompt
        assert "CLASS" in prompt
