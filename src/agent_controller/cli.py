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
from agent_controller.models import DocumentStage, QuestionStatus
from agent_controller.qanda import render_qanda
from agent_controller.store import Store
from agent_controller.transition_log import TransitionLogger

DEFAULT_DB = "controller.db"


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def cmd_questions(args: argparse.Namespace) -> int:
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
    with _store(args) as store:
        print(render_qanda(store.questions(args.run)))
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
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
        result = CompleteGate(store, getattr(args, "workspace", ".")).check(run)
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
    parser.add_argument("--run", required=True, help="run id")
    parser.add_argument("--workspace", default=".", help="Git workspace for status checks")
    sub = parser.add_subparsers(dest="command", required=True)

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
