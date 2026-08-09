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
from agent_controller.models import DocumentStage, QuestionStatus
from agent_controller.qanda import render_qanda
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

DEFAULT_DB = "controller.db"


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
        with _store(args) as store:
            run, run_input = start_run(
                store,
                TransitionLogger(store),
                run_id=args.run,
                workspace=args.workspace,
                request=args.request,
            )
        print(f"created {run.run_id}")
        print(f"  workspace: {run_input.workspace}")
        print(f"  position: {run.current_state.value}")
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
    init.set_defaults(func=cmd_init)

    answer = sub.add_parser("answer", help="answer a question that is waiting on a human")
    answer.add_argument("question_id")
    answer.add_argument("answer")
    answer.add_argument(
        "--upstream",
        choices=[stage.value for stage in DocumentStage],
        help="the answer changes this upstream document; do not resume the old phase",
    )
    answer.add_argument("--workspace", help="where QandA.md lives")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "workspace", None):
        args.workspace = str(Path(args.workspace))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
