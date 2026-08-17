"""人間が Agent Controller に触るための最小の入口。

いまのところ用があるのは 1 つだけ。**AI が答えられなかった質問に人間が答える。**
通常運転で人間が Agent 間のメッセージを仲介することはない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_controller.human import AnswerRejected, answer_batch, answer_question, complete_blockers
from agent_controller.complete import CompleteGate
from agent_controller.lifecycle import RunStartError, start_run, validate_workspace
from agent_controller.git_checkpoint import GitCheckpointError, GitCheckpointManager
from agent_controller.models import DocumentStage, QuestionStatus, TaskType
from agent_controller.qanda import render_qanda
from agent_controller.router import classify_task_type
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

DEFAULT_DB = "controller.db"


def _configure_output() -> None:
    """Keep Windows cp932 consoles from crashing on UTF-8 Q&A content."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _require_run(args: argparse.Namespace) -> str:
    if not args.run:
        raise SystemExit("--run is required for this command")
    return args.run


def cmd_init(args: argparse.Namespace) -> int:
    if not args.workspace:
        print("rejected: --workspace is required", file=sys.stderr)
        return 2
    if not args.run:
        print("rejected: --run is required", file=sys.stderr)
        return 2
    try:
        # Validate before opening Store so invalid input does not even create
        # a new SQLite file.
        validate_workspace(args.workspace)
        if not args.request.strip():
            raise RunStartError("request must not be empty")
        # 明示 --task-type が無ければ、公開 CLI の入口としてここで自動分類する
        # （指示書 018 §3）。ライブラリの start_run() 自体は None のまま
        # （既存呼び出し側の挙動を変えない）。
        task_type = TaskType(args.task_type) if getattr(args, "task_type", None) else classify_task_type(args.request)
        with _store(args) as store:
            run, run_input = start_run(
                store,
                TransitionLogger(store),
                run_id=args.run,
                workspace=args.workspace,
                request=args.request,
                task_type=task_type,
            )
        print(f"created {run.run_id}")
        print(f"  workspace: {run_input.workspace}")
        print(f"  position: {run.current_state.value}")
        print(f"  task_type: {run.task_type.value if run.task_type else '-'}")
        return 0
    except (OSError, RunStartError) as error:
        print(f"rejected: {error}", file=sys.stderr)
        return 2


def cmd_questions(args: argparse.Namespace) -> int:
    args.run = _require_run(args)
    with _store(args) as store:
        questions = store.questions(args.run)
        if not questions:
            print(f"no questions in run {args.run}")
            return 0
        for question in questions:
            mark = "*" if question.status != QuestionStatus.ANSWERED else " "
            print(f"{mark} {question.question_id}  {question.status.value:<15} "
                  f"{question.position()}")
            print(f"    {question.question}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    args.run = _require_run(args)
    with _store(args) as store:
        print(render_qanda(store.questions(args.run)))
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    args.run = _require_run(args)
    upstream = DocumentStage(args.upstream) if args.upstream else None
    with _store(args) as store:
        logger = TransitionLogger(store)
        try:
            run, question, transition = answer_question(
                store,
                logger,
                args.run,
                args.question_id,
                args.answer,
                upstream_target=upstream,
                workspace=args.workspace,
            )
        except AnswerRejected as error:
            print(f"rejected: {error}", file=sys.stderr)
            return 2

        print(f"{question.question_id} answered")
        print(f"  -> {transition.to_state.value}"
              + (f"/{transition.to_substate.value}" if transition.to_substate else ""))
        if args.workspace:
            try:
                GitCheckpointManager(args.workspace).commit_paths(["QandA.md"])
            except GitCheckpointError:
                # Legacy answer usage also supports a non-Git fixture workspace.
                pass
        remaining = complete_blockers(store, run)
        if remaining:
            print("  still blocking COMPLETE: " + "; ".join(remaining))
    return 0


def cmd_answer_batch(args: argparse.Namespace) -> int:
    args.run = _require_run(args)
    try:
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        if not isinstance(answers, list):
            raise ValueError("answers JSON must be an array")
        with _store(args) as store:
            logger = TransitionLogger(store)
            results = answer_batch(store, logger, args.run, answers, workspace=args.workspace)
            print(f"{len(results)} questions answered")
        return 0
    except (OSError, ValueError, AnswerRejected) as error:
        print(f"rejected: {error}", file=sys.stderr)
        return 2


def cmd_status(args: argparse.Namespace) -> int:
    args.run = _require_run(args)
    with _store(args) as store:
        run = store.load_run(args.run)
        if run is None:
            print(f"run {args.run} does not exist", file=sys.stderr)
            return 2
        position = run.current_state.value
        if run.substate:
            position += f"/{run.substate.value}"
        if run.phase:
            position += f"/{run.phase.value}"
        print(f"{run.run_id}  {position}  ({run.status.value})")
        result = CompleteGate(store, getattr(args, "workspace", None) or ".").check(run)
        print("Complete: " + ("YES" if result.ready else "NO"))
        if result.blockers:
            print("Blockers:")
            for blocker in result.blockers:
                print(f"- {blocker.code.value}: {blocker.detail}")
        else:
            print("Blockers: none")
    return 0


def cmd_recovery(args: argparse.Namespace) -> int:
    """Auto-Recovery の試行履歴（指示書 018 §4.3）を表示する。"""
    args.run = _require_run(args)
    with _store(args) as store:
        attempts = store.recovery_attempts(args.run)
        if not attempts:
            print(f"no recovery attempts in run {args.run}")
            return 0
        for item in attempts:
            mismatch = " capability_mismatch" if item.capability_mismatch else ""
            fallback = f" -> {item.fallback_worker.value}" if item.fallback_worker else " -> (none)"
            print(
                f"#{item.attempt_number} {item.error_code:<24} "
                f"{item.failed_worker.value if item.failed_worker else '-':<12}"
                f"{fallback}{mismatch}  [{item.final_outcome}]"
            )
            if item.reason:
                print(f"    {item.reason}")
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    """Default Decision Policy による自動決定の Decision Log（指示書 018 §6.3）。"""
    args.run = _require_run(args)
    with _store(args) as store:
        decided = [
            item for item in store.questions(args.run)
            if item.classification or item.policy_rule
        ]
        if not decided:
            print(f"no policy decisions recorded in run {args.run}")
            return 0
        for item in decided:
            print(f"{item.question_id}  {item.status.value:<12} classification={item.classification or '-'}")
            print(f"    policy_rule={item.policy_rule or '-'}  policy_scope={item.policy_scope or '-'}")
            print(f"    risk={item.risk or '-'}  reversible={item.reversible}")
            print(f"    question: {item.question}")
            if item.provisional_answer:
                print(f"    provisional_answer: {item.provisional_answer}")
    return 0


def cmd_contracts(args: argparse.Namespace) -> int:
    args.run = _require_run(args)
    with _store(args) as store:
        contracts = store.contracts(args.run)
        if not contracts:
            print(f"no acceptance contracts in run {args.run}")
            return 0
        for contract in contracts:
            evidence = contract.actual or contract.evidence or "-"
            print(f"{contract.contract_id}  {contract.status.value:<11} "
                  f"{contract.verifier_kind:<12} {contract.target_artifact}  {evidence}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-controller")
    parser.add_argument("--db", default=DEFAULT_DB, help="controller database")
    parser.add_argument("--run", help="run id")
    parser.add_argument("--workspace", help="Git workspace for status checks")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a run and enter DESIGN")
    # Accept options after ``init`` as well as the historical global-option
    # placement, so both CLI spellings are usable.
    init.add_argument("--db", dest="db", default=argparse.SUPPRESS, help="controller database")
    init.add_argument("--run", dest="run", default=argparse.SUPPRESS, help="new run id")
    init.add_argument("--workspace", dest="workspace", default=argparse.SUPPRESS, help="Git workspace")
    init.add_argument("--request", required=True, help="formal initial request")
    init.add_argument(
        "--task-type",
        choices=[item.value for item in TaskType],
        default=argparse.SUPPRESS,
        help="Task Complexity Router classification; auto-classified from --request if omitted",
    )
    init.set_defaults(func=cmd_init)

    answer = sub.add_parser("answer", help="answer a question that is waiting on a human")
    answer.add_argument("question_id")
    answer.add_argument("answer")
    answer.add_argument(
        "--upstream",
        choices=[stage.value for stage in DocumentStage],
        help="the answer changes this upstream document; do not resume the old phase",
    )
    answer.add_argument("--workspace", default=argparse.SUPPRESS, help="where QandA.md lives")
    answer.set_defaults(func=cmd_answer)

    batch = sub.add_parser("answer-batch", help="answer several human-required questions")
    batch.add_argument("answers", help="JSON array with question_id and answer")
    batch.set_defaults(func=cmd_answer_batch)

    listing = sub.add_parser("questions", help="list questions")
    listing.set_defaults(func=cmd_questions)

    show = sub.add_parser("show", help="print QandA.md as it would be generated")
    show.set_defaults(func=cmd_show)

    status = sub.add_parser("status", help="where the run is")
    status.set_defaults(func=cmd_status)

    contracts = sub.add_parser("contracts", help="list acceptance contracts")
    contracts.set_defaults(func=cmd_contracts)

    recovery = sub.add_parser("recovery", help="list Auto-Recovery attempts")
    recovery.set_defaults(func=cmd_recovery)

    decisions = sub.add_parser("decisions", help="list Default Decision Policy decisions")
    decisions.set_defaults(func=cmd_decisions)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    if getattr(args, "workspace", None):
        args.workspace = str(Path(args.workspace))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
