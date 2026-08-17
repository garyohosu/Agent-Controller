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
    AgyCliWorker,
    ClaudeCodeWorker,
    CodexCliWorker,
    CommandResult,
    GrokCliWorker,
    build_prompt,
    extract_json,
    looks_like_a_resource_limit,
    resolve_executable,
    result_from_output,
    signed_exit_code,
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
from agent_controller.document_stage import allowed_stage_events
from agent_controller.worker import (
    NO_GUESSING_RULE,
    PHASE_DIRECTIVES,
    PHASE_ROLES,
    REVIEWER_RULE,
    WorkerRequest,
    WorkerResult,
    WorkerRouter,
    phase_handlers_from_worker,
    phase_handlers_for_stages,
    role_router_from_adapters,
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


def test_default_role_router_declares_provider_order() -> None:
    codex = CodexCliWorker(runner=fake_runner('{"event": "DONE"}'))
    claude = ClaudeCodeWorker(runner=fake_runner('{"event": "PASS"}'))
    grok = GrokCliWorker(runner=fake_runner('{"event": "PASS"}'))
    router = role_router_from_adapters({
        Worker.CODEX_CLI: codex,
        Worker.CLAUDE_CODE: claude,
        Worker.GROK: grok,
    })
    assert [item.name for item in router.candidates_for(Role.REVIEWER)] == [
        Worker.CLAUDE_CODE, Worker.CODEX_CLI, Worker.GROK
    ]
    assert [item.name for item in router.candidates_for(Role.IMPLEMENTER)] == [
        Worker.CODEX_CLI, Worker.CLAUDE_CODE
    ]


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
    def test_stage_dispatch_keeps_stage_specific_inputs(self) -> None:
        first = DocumentStageConfig(name=DocumentStage.SPEC, inputs=[], output="SPEC.md")
        second = DocumentStageConfig(name=DocumentStage.USECASE, inputs=["SPEC.md"], output="USECASE.md")
        runner = fake_runner('{"event": "PASS"}')
        worker = CodexCliWorker(runner=runner)
        handlers = phase_handlers_for_stages(worker, ".", [first, second])
        handlers[Phase.REVIEW_DEEP](RunState(project_id="p", run_id="r", current_state=State.DESIGN, substate=DocumentStage.SPEC))
        assert "SPEC.md" in runner.calls[0][3]
        assert "USECASE.md" not in runner.calls[0][3]

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

    def test_claude_code_rate_limit_observation_is_resource_limit(self) -> None:
        worker = ClaudeCodeWorker(
            runner=fake_runner(
                exit_code=1,
                stderr="Claude Code rate limit reached after 2m11s",
            )
        )
        result = worker.run(make_request())
        assert result.event == Event.WORKER_RESOURCE_LIMIT
        assert result.reason == "CLAUDE_CODE hit a usage limit"
        assert "rate limit" in result.diagnostic["stderr_tail"].lower()

    def test_timeout_is_a_worker_error(self) -> None:
        worker = CodexCliWorker(runner=fake_runner(timed_out=True, exit_code=-1))
        result = worker.run(make_request())
        assert result.event == Event.WORKER_ERROR
        assert "timed out" in (result.reason or "")

    def test_success_body_with_429_is_not_resource_limit(self) -> None:
        result = CodexCliWorker(runner=fake_runner("429 is just text")).run(make_request())
        assert result.event == Event.WORKER_ERROR
        assert result.diagnostic["raw_exit_code"] == 0

    def test_diagnostic_contains_invocation_and_prompt_sizes(self, tmp_path: Path) -> None:
        result = CodexCliWorker(runner=fake_runner('{"event": "PASS"}')).run(
            make_request(workspace=str(tmp_path), phase=Phase.QANDA)
        )
        assert result.diagnostic["invocation_id"]
        assert result.diagnostic["prompt_chars"] > 0
        assert result.diagnostic["prompt_utf8_bytes"] >= result.diagnostic["prompt_chars"]
        assert result.diagnostic["stdout_chars"] > 0
        assert result.diagnostic["result_event"] == "PASS"

    def test_windows_exit_code_keeps_raw_and_signed_diagnostic(self) -> None:
        result = CodexCliWorker(runner=fake_runner(exit_code=4294967295, stderr="fatal")).run(make_request())
        assert result.event == Event.WORKER_ERROR
        assert signed_exit_code(4294967295) == -1
        assert result.diagnostic["signed_exit_code"] == -1


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

    def test_read_only_command_shapes(self, tmp_path: Path) -> None:
        request = make_request(role=Role.REVIEWER, workspace=str(tmp_path), phase=Phase.REVIEW_LIGHT)
        codex_runner = fake_runner('{"event": "PASS"}')
        CodexCliWorker(runner=codex_runner).run(request)
        assert "read-only" in codex_runner.calls[0][0]
        claude_runner = fake_runner(json.dumps({"result": '{"event": "PASS"}'}))
        ClaudeCodeWorker(runner=claude_runner).run(request)
        assert "plan" in claude_runner.calls[0][0]
        assert "--no-session-persistence" in claude_runner.calls[0][0]
        assert "--tools" not in claude_runner.calls[0][0]

    def test_grok_envelope_text_and_command(self, tmp_path: Path) -> None:
        runner = fake_runner(json.dumps({"text": '{"event": "PASS"}'}))
        result = GrokCliWorker(runner=runner).run(make_request(role=Role.REVIEWER, workspace=str(tmp_path)))
        assert result.event == Event.PASS
        assert runner.calls[0][0][1:3] == ["--prompt-file", runner.calls[0][0][2]]
        assert runner.calls[0][0][-2:] == ["--output-format", "json"]

    def test_agy_direct_stdout_and_utf16_guard(self, tmp_path: Path) -> None:
        runner = fake_runner('{"event": "PASS"}')
        result = AgyCliWorker(runner=runner).run(make_request(role=Role.REVIEWER, workspace=str(tmp_path)))
        assert result.event == Event.PASS
        assert runner.calls[0][0][0:2] == ["agy", "--print"]
        huge = make_request(role=Role.REVIEWER, directive="x" * 31000)
        blocked = AgyCliWorker(runner=runner).run(huge)
        assert blocked.event == Event.WORKER_ERROR
        assert "UTF-16" in (blocked.reason or "")


class TestExecutableResolution:
    def test_resolves_through_pathext(self) -> None:
        """Windows では npm 由来の CLI が codex と codex.cmd の両方で置かれる。

        CreateProcess は PATHEXT を見ないので、素の名前のままだと起動できない。
        実際にこれで最初の smoke run が exit -1 になった。
        """
        resolved = resolve_executable("python")
        assert Path(resolved).name.lower().startswith("python")
        assert Path(resolved).is_absolute()

    def test_unknown_command_is_left_alone(self) -> None:
        assert resolve_executable("definitely-not-installed-xyz") == (
            "definitely-not-installed-xyz"
        )


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


class TestDirectiveConstraints:
    """指示書 002 §1 / §4: 推測禁止を Directive に正式に入れる。

    実 AI で回したとき、Codex は仕様の穴を QUESTION にせず自分で埋めて DONE を返した。
    Adapter の不具合ではなく、指示が縛っていなかったことが原因だった。
    """

    def test_every_judgement_phase_forbids_guessing(self) -> None:
        for phase, directive in PHASE_DIRECTIVES.items():
            assert NO_GUESSING_RULE in directive, phase

    def test_the_rule_names_question_as_the_alternative(self) -> None:
        assert "return QUESTION instead of deciding it yourself" in NO_GUESSING_RULE

    def test_a_question_must_say_what_it_checked(self) -> None:
        """§1: 何が不明か、どの成果物を確認したか、なぜ判断不能か。"""
        assert "what is undecided" in NO_GUESSING_RULE
        assert "which documents you checked" in NO_GUESSING_RULE
        assert "cannot settle it" in NO_GUESSING_RULE

    def test_it_does_not_ask_for_more_questions_in_general(self) -> None:
        """§1: 目的は質問を増やすことではない。表記揺れまで聞かせない。"""
        assert "not an instruction to ask more often" in NO_GUESSING_RULE
        assert "do not raise those as questions" in NO_GUESSING_RULE

    def test_reviewers_may_not_pass_on_their_own_interpretation(self) -> None:
        for phase in (Phase.REVIEW_LIGHT, Phase.REVIEW_DEEP):
            assert REVIEWER_RULE in PHASE_DIRECTIVES[phase]
        assert REVIEWER_RULE not in PHASE_DIRECTIVES[Phase.GENERATE]

    def test_the_rule_lives_in_one_place_not_per_adapter(self) -> None:
        """§4: Adapter ごとに固定文を書き分けない。"""
        codex = Path("src/agent_controller/cli_worker.py").read_text(encoding="utf-8")
        assert "Do not fill in a judgement" not in codex

    def test_the_rule_actually_reaches_the_worker(self) -> None:
        """§8: Directive が実際に Worker へ渡っているかを確かめる。"""
        runner = fake_runner('{"event": "DONE"}')
        worker = CodexCliWorker(runner=runner)
        handlers = phase_handlers_from_worker(
            worker, workspace=".", input_artifacts=["SPEC.md"], output_artifact="USECASE.md"
        )
        handlers[Phase.GENERATE](
            RunState(project_id="p", run_id="r", current_state=State.DESIGN)
        )

        _argv, _cwd, _timeout, prompt = runner.calls[0]
        assert NO_GUESSING_RULE in prompt

    def test_question_is_an_allowed_event_everywhere_it_is_demanded(self) -> None:
        """§8: 構造化出力スキーマが QUESTION を許しているか。"""
        for phase in (Phase.GENERATE, Phase.REVIEW_LIGHT, Phase.REVIEW_DEEP, Phase.FIX):
            assert Event.QUESTION in allowed_stage_events(phase), phase
