"""Claude Code / Codex CLI の Adapter（§17-11C / §17-12）。

CLI 固有の処理はここだけに閉じ込める。Controller から見えるのは
WorkerRequest / WorkerResult だけ。

Worker の出力は信用しない。JSON が壊れていても、Event 名が知らないものでも、
プロセスが落ちても、必ず WorkerResult に翻訳して返す。例外は投げない。
判断できない出力は WORKER_ERROR、利用制限らしきものは WORKER_RESOURCE_LIMIT。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from agent_controller.models import Event, Worker
from agent_controller.worker import WorkerRequest, WorkerResult

RESOURCE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
    "429",
    "overloaded",
    "session limit",
    "too many requests",
    "insufficient_quota",
)
"""利用制限らしさの手がかり。文字列一致なので当然もれる。

外したら「本当は待てばよかったのに WORKER_ERROR で人間を呼ぶ」だけなので、
安全側に倒れる。CLI ごとの終了コードが分かってきたら置き換える。
"""


class CommandResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    resolved_executable: str | None = None
    elapsed_ms: int | None = None


CommandRunner = Callable[[Sequence[str], str, int, str], CommandResult]
"""argv, cwd, timeout 秒, 標準入力 -> 結果。テストではここを差し替える。"""


def resolve_executable(name: str) -> str:
    """PATH から実行ファイルを解決する。

    Windows では npm 由来の CLI が `codex`（sh スクリプト）と `codex.cmd`
    （ランチャ）の両方で置かれる。CreateProcess は PATHEXT を見ないので、
    素の名前のまま渡すと起動できない。shutil.which は PATHEXT を見る。
    """
    found = shutil.which(name)
    return found if found is not None else name


def run_command(
    argv: Sequence[str], cwd: str, timeout: int, stdin_text: str
) -> CommandResult:
    resolved = resolve_executable(argv[0])
    argv = [resolved, *argv[1:]]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(exit_code=-1, stderr=f"timed out after {timeout}s", timed_out=True, resolved_executable=resolved, elapsed_ms=int((time.monotonic()-started)*1000))
    except OSError as error:  # CLI が無い、起動できない
        return CommandResult(exit_code=-1, stderr=str(error), resolved_executable=resolved, elapsed_ms=int((time.monotonic()-started)*1000))

    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        resolved_executable=resolved,
        elapsed_ms=int((time.monotonic()-started)*1000),
    )


_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """出力から JSON オブジェクトを取り出す。

    AI は指示しても前後に文章を付けるので、素直に json.loads できる前提を置かない。
    フェンス付き -> 最後の {...} の順に試す。
    """
    for match in reversed(_FENCED.findall(text)):
        try:
            parsed = json.loads(match)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    depth = 0
    end = -1
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if char == "}":
            if depth == 0:
                end = index
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0 and end != -1:
                try:
                    parsed = json.loads(text[index : end + 1])
                except json.JSONDecodeError:
                    end = -1
                    continue
                if isinstance(parsed, dict):
                    return parsed
                end = -1
    return None


def looks_like_a_resource_limit(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in RESOURCE_LIMIT_PATTERNS)


def failure_is_resource_limit(stdout: str, stderr: str) -> bool:
    """Classify only a failed invocation; a successful model body is not an error."""
    for candidate in (stdout, stderr):
        try:
            payload = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            status = payload.get("api_error_status") or payload.get("status")
            if status == 429 or str(payload.get("error", "")).lower() in {"quota", "rate_limit", "rate limited"}:
                return True
            if payload.get("is_error") and looks_like_a_resource_limit(str(payload.get("result", ""))):
                return True
    return looks_like_a_resource_limit(stderr)


def signed_exit_code(raw: int | None) -> int | None:
    if raw is None:
        return None
    return raw - 2**32 if raw >= 2**31 else raw


def build_prompt(request: WorkerRequest) -> str:
    """Worker に渡す指示。工程に必要なものだけを書く（memo.md §4.2）。"""
    lines = [
        f"You are acting as the {request.role.value} in an automated software "
        "development pipeline.",
        "",
        f"State: {request.state.value}",
    ]
    if request.stage is not None:
        lines.append(f"Design stage: {request.stage.value}")
    if request.phase is not None:
        lines.append(f"Phase: {request.phase.value}")

    lines += ["", "## Task", request.directive, ""]

    if request.input_artifacts:
        lines += [
            "## Input documents (read these; do not modify them)",
            *(f"- {path}" for path in request.input_artifacts),
            "",
        ]
    if request.artifact_contents:
        lines += ["## Controller-attached artifact contents"]
        for path, content in request.artifact_contents.items():
            lines += [f"### {path}", content, ""]
    if request.output_artifact:
        lines += [
            "## Output document (create or update this)",
            f"- {request.output_artifact}",
            "",
        ]

    allowed = ", ".join(event.value for event in request.allowed_events)
    lines += [
        "## Response",
        "After doing the work, reply with a single fenced JSON object and nothing after it:",
        "",
        "```json",
        '{"event": "...", "reason": "...", "finding_code": null, '
        '"finding_subject": null, "finding_category": null, "question": null, '
        '"answer": null, "upstream_target": null, "files_changed": []}',
        "```",
        "",
        f"`event` must be exactly one of: {allowed}",
        "`reason` is one short sentence a human will read in a log.",
        "`upstream_target` is required only for UPSTREAM_CHANGE_REQUIRED, and must name "
        "the design stage that needs to change (SPEC, USECASE, SEQUENCE, CLASS, UI, TESTCASE).",
        "",
        "When the event reports a problem (FAIL, LOCAL_FIX, SERIOUS_ISSUE, "
        "UPSTREAM_CHANGE_REQUIRED), `finding_code` and `finding_subject` are required:",
        "",
        "- `finding_code`: SCREAMING_SNAKE_CASE name for the KIND of problem, e.g. "
        "RESPONSIBILITY_MISMATCH, MISSING_SECTION, CONTRADICTS_SPEC, BROKEN_DIAGRAM, "
        "TEST_FAILURE. Use the SAME code every time you report the same kind of problem.",
        "- `finding_subject`: the specific thing at fault, e.g. a class name, a section "
        "heading, or a test id. Use the SAME subject string every time you report the "
        "same problem, even if you word `reason` differently.",
        "",
        "These two fields are how the controller detects that a problem is not getting "
        "fixed. Wording them consistently matters more than wording them well.",
        "",
        "`question` is required for QUESTION: state exactly what you cannot decide.",
        "`answer` is required when you answer a question: state the decision and which "
        "document settles it.",
    ]
    return "\n".join(lines)


def result_from_output(text: str, raw: str, *, classify_resource: bool = True) -> WorkerResult:
    """CLI の出力を WorkerResult にする。壊れていても例外にしない。"""
    payload = extract_json(text)
    if payload is None:
        if classify_resource and looks_like_a_resource_limit(raw):
            return WorkerResult(
                event=Event.WORKER_RESOURCE_LIMIT,
                reason="worker stopped and the output mentions a usage limit",
                raw_output=raw,
            )
        return WorkerResult(
            event=Event.WORKER_ERROR,
            reason="no JSON object in the worker output",
            raw_output=raw,
        )

    name = str(payload.get("event", "")).strip().upper()
    try:
        event = Event(name)
    except ValueError:
        return WorkerResult(
            event=Event.WORKER_ERROR,
            reason=f"unknown event {name!r}",
            structured_result=payload,
            raw_output=raw,
        )

    def field(name: str) -> str | None:
        value = payload.get(name)
        return str(value).strip() or None if value is not None else None

    return WorkerResult(
        event=event,
        structured_result=payload,
        reason=str(payload.get("reason") or "") or None,
        finding_code=field("finding_code"),
        finding_subject=field("finding_subject"),
        finding_category=field("finding_category"),
        question=field("question"),
        answer=field("answer"),
        action=field("action"),
        files_changed=[str(item) for item in payload.get("files_changed") or []],
        decision_class=field("decision_class"),
        provisional_answer=field("provisional_answer"),
        risk=field("risk"),
        reversible=payload.get("reversible"),
        affected_artifacts=[str(item) for item in payload.get("affected_artifacts") or []],
        blocking_scope=field("blocking_scope"),
        recommended_human_action=field("recommended_human_action"),
        requires_human_confirmation_before_complete=bool(
            payload.get("requires_human_confirmation_before_complete", False)
        ),
        raw_output=raw,
    )


class CliWorker:
    """CLI を subprocess で呼ぶ Worker の共通部分。"""

    name: Worker

    def __init__(
        self,
        runner: CommandRunner | None = None,
        timeout: int = 900,
        model: str | None = None,
    ) -> None:
        self.runner = runner if runner is not None else run_command
        self.timeout = timeout
        self.model = model

    def command(self, request: WorkerRequest, output_file: str) -> list[str]:
        raise NotImplementedError

    def output_text(self, result: CommandResult, output_file: str) -> str:
        """JSON を探すべきテキスト。既定は標準出力。"""
        return result.stdout

    def run(self, request: WorkerRequest) -> WorkerResult:
        invocation_id = str(uuid.uuid4())
        started_wall = datetime.now(timezone.utc)
        started_mono = time.monotonic()
        prompt = build_prompt(request)
        Path(request.workspace).mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="agent-controller-") as scratch:
            output_file = str(Path(scratch) / "last-message.txt")
            command = self.command(request, output_file)
            result = self.runner(command, request.workspace, self.timeout, prompt)
            text = self.output_text(result, output_file)

        raw = "\n".join(part for part in (text, result.stderr) if part).strip()
        diagnostic = {
            "invocation_id": invocation_id,
            "started_at": started_wall.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "timeout_seconds": self.timeout,
            "worker": self.name.value,
            "role": request.role.value,
            "state": request.state.value,
            "stage": request.stage.value if request.stage else None,
            "phase": request.phase.value if request.phase else None,
            "argv": command,
            "resolved_executable": result.resolved_executable or resolve_executable(command[0]),
            "raw_exit_code": result.exit_code,
            "signed_exit_code": signed_exit_code(result.exit_code),
            "elapsed_ms": result.elapsed_ms if result.elapsed_ms is not None else int((time.monotonic() - started_mono) * 1000),
            "timed_out": result.timed_out,
            "prompt_chars": len(prompt),
            "prompt_utf8_bytes": len(prompt.encode("utf-8")),
            "artifact_chars": sum(len(value) for value in request.artifact_contents.values()),
            "question_chars": len(request.directive) if request.phase and request.phase.value == "QANDA" else 0,
            "stdout_chars": len(text),
            "stderr_chars": len(result.stderr),
            "result_event": None,
            "stdout_tail": text[-1000:],
            "stderr_tail": result.stderr[-1000:],
        }

        if result.timed_out:
            diagnostic["result_event"] = Event.WORKER_ERROR.value
            return WorkerResult(
                event=Event.WORKER_ERROR,
                reason=f"{self.name.value} timed out after {self.timeout}s",
                raw_output=raw,
                diagnostic=diagnostic,
            )

        if result.exit_code != 0:
            if failure_is_resource_limit(text, result.stderr):
                diagnostic["result_event"] = Event.WORKER_RESOURCE_LIMIT.value
                return WorkerResult(
                    event=Event.WORKER_RESOURCE_LIMIT,
                    reason=f"{self.name.value} hit a usage limit",
                    raw_output=raw,
                    diagnostic=diagnostic,
                )
            return WorkerResult(
                event=Event.WORKER_ERROR,
                reason=f"{self.name.value} exited with {result.exit_code}",
                raw_output=raw,
                diagnostic=diagnostic,
            )

        parsed = result_from_output(text, raw, classify_resource=False)
        diagnostic["result_event"] = parsed.event.value
        parsed.diagnostic = diagnostic
        return parsed


class CodexCliWorker(CliWorker):
    """codex exec を非対話で呼ぶ。"""

    name = Worker.CODEX_CLI

    def command(self, request: WorkerRequest, output_file: str) -> list[str]:
        command = [
            "codex",
            "exec",
            "--cd",
            request.workspace,
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write" if request.role.value == "IMPLEMENTER" else "read-only",
            "--output-last-message",
            output_file,
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    def output_text(self, result: CommandResult, output_file: str) -> str:
        # 最終メッセージだけを別ファイルに書かせる。進捗ログと混ざらない。
        path = Path(output_file)
        if path.exists():
            last = path.read_text(encoding="utf-8", errors="replace").strip()
            if last:
                return last
        return result.stdout


class ClaudeCodeWorker(CliWorker):
    """claude -p を非対話で呼ぶ。"""

    name = Worker.CLAUDE_CODE

    def command(self, request: WorkerRequest, output_file: str) -> list[str]:
        command = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits" if request.role.value == "IMPLEMENTER" else "plan",
            "--add-dir",
            request.workspace,
        ]
        if request.role.value != "IMPLEMENTER":
            # Reviewers may inspect artifacts but may not edit or execute shell
            # commands.  An empty --tools list made the previous profile unable
            # to read input documents at all.
            command += ["--tools", "Read,Glob,Grep", "--no-session-persistence"]
        if self.model:
            command += ["--model", self.model]
        return command

    def output_text(self, result: CommandResult, output_file: str) -> str:
        # --output-format json は封筒を返すので、まず result フィールドを取り出す。
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            return envelope["result"]
        return result.stdout


def _diagnostic(worker: Worker, request: WorkerRequest, command: list[str], result: CommandResult, text: str, prompt: str = "", timeout_seconds: int | None = None) -> dict:
    elapsed = result.elapsed_ms or 0
    finished = datetime.now(timezone.utc)
    return {
        "invocation_id": str(uuid.uuid4()),
        "started_at": datetime.fromtimestamp(finished.timestamp() - elapsed / 1000, timezone.utc).isoformat(),
        "finished_at": finished.isoformat(),
        "timeout_seconds": timeout_seconds,
        "worker": worker.value, "role": request.role.value, "state": request.state.value,
        "stage": request.stage.value if request.stage else None,
        "phase": request.phase.value if request.phase else None,
        "argv": command, "resolved_executable": result.resolved_executable or resolve_executable(command[0]),
        "raw_exit_code": result.exit_code, "signed_exit_code": signed_exit_code(result.exit_code),
        "elapsed_ms": result.elapsed_ms, "timed_out": result.timed_out,
        "prompt_chars": len(prompt), "prompt_utf8_bytes": len(prompt.encode("utf-8")),
        "artifact_chars": sum(len(value) for value in request.artifact_contents.values()),
        "question_chars": len(request.directive) if request.phase and request.phase.value == "QANDA" else 0,
        "stdout_chars": len(text), "stderr_chars": len(result.stderr),
        "result_event": None,
        "stdout_tail": text[-1000:], "stderr_tail": result.stderr[-1000:],
    }


class GrokCliWorker(CliWorker):
    name = Worker.GROK

    def run(self, request: WorkerRequest) -> WorkerResult:
        prompt = build_prompt(request)
        with tempfile.TemporaryDirectory(prefix="agent-controller-grok-") as scratch:
            prompt_file = Path(scratch) / "prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            command = ["grok", "--prompt-file", str(prompt_file), "--output-format", "json"]
            result = self.runner(command, request.workspace, self.timeout, "")
        try:
            envelope = json.loads(result.stdout)
            text = envelope.get("text", "") if isinstance(envelope, dict) else result.stdout
        except json.JSONDecodeError:
            text = result.stdout
        raw = "\n".join(part for part in (text, result.stderr) if part).strip()
        diagnostic = _diagnostic(self.name, request, command, result, text, prompt, self.timeout)
        if result.timed_out:
            diagnostic["result_event"] = Event.WORKER_ERROR.value
            return WorkerResult(event=Event.WORKER_ERROR, reason=f"{self.name.value} timed out", raw_output=raw, diagnostic=diagnostic)
        if result.exit_code != 0:
            event = Event.WORKER_RESOURCE_LIMIT if failure_is_resource_limit(text, result.stderr) else Event.WORKER_ERROR
            diagnostic["result_event"] = event.value
            return WorkerResult(event=event, reason=f"{self.name.value} exited with {result.exit_code}", raw_output=raw, diagnostic=diagnostic)
        parsed = result_from_output(text, raw, classify_resource=False)
        diagnostic["result_event"] = parsed.event.value
        parsed.diagnostic = diagnostic
        return parsed


class AgyCliWorker(CliWorker):
    name = Worker.ANTIGRAVITY
    MAX_PROMPT_UTF16 = 30000

    @classmethod
    def command_line_units(cls, text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def run(self, request: WorkerRequest) -> WorkerResult:
        prompt = build_prompt(request)
        units = self.command_line_units(prompt)
        if units > self.MAX_PROMPT_UTF16:
            return WorkerResult(
                event=Event.WORKER_ERROR,
                reason=f"{self.name.value} prompt exceeds UTF-16 command-line guard",
                diagnostic={"worker": self.name.value, "prompt_utf16_units": units, "limit": self.MAX_PROMPT_UTF16},
            )
        command = ["agy", "--print", prompt]
        result = self.runner(command, request.workspace, self.timeout, "")
        raw = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        diagnostic = _diagnostic(self.name, request, ["agy", "--print", "<prompt>"], result, result.stdout, prompt, self.timeout)
        if result.timed_out:
            diagnostic["result_event"] = Event.WORKER_ERROR.value
            return WorkerResult(event=Event.WORKER_ERROR, reason=f"{self.name.value} timed out", raw_output=raw, diagnostic=diagnostic)
        if result.exit_code != 0:
            event = Event.WORKER_RESOURCE_LIMIT if failure_is_resource_limit(result.stdout, result.stderr) else Event.WORKER_ERROR
            diagnostic["result_event"] = event.value
            return WorkerResult(event=event, reason=f"{self.name.value} exited with {result.exit_code}", raw_output=raw, diagnostic=diagnostic)
        parsed = result_from_output(result.stdout, raw, classify_resource=False)
        diagnostic["result_event"] = parsed.event.value
        parsed.diagnostic = diagnostic
        return parsed
