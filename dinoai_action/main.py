"""Entrypoint: event → context → trigger → poll → review."""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import time

from . import context as ctx_mod
from .findings import (
    commentable_lines,
    find_previous_reviews,
    parse_findings,
    partition,
    render_comment,
    render_review_body,
)
from .github_api import GitHubApiError, GitHubClient
from .paradime_api import ParadimeApiError, ParadimeClient


# --- workflow plumbing -------------------------------------------------------------------

def _input(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper()}", default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = _input(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = _input(name, str(default))
    try:
        return int(raw)
    except ValueError:
        _warn(f"input {name}={raw!r} is not an integer; using {default}")
        return default


def _log(msg: str) -> None:
    print(msg, flush=True)


def _notice(msg: str) -> None:
    _log(f"::notice::{msg}")


def _warn(msg: str) -> None:
    _log(f"::warning::{msg}")


def _error(msg: str) -> None:
    _log(f"::error::{msg}")


def _set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    delim = f"ghadelim_{secrets.token_hex(8)}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def _step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def _load_event() -> tuple[str, dict]:
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.exists(path):
        raise SystemExit("GITHUB_EVENT_PATH is missing; this must run inside GitHub Actions")
    with open(path, encoding="utf-8") as fh:
        return name, json.load(fh)


# --- the run ------------------------------------------------------------------------------

class _Cancelled(Exception):
    pass


def run() -> int:
    event_name, payload = _load_event()
    mode = _input("mode", "review").lower()
    fail_on_findings = _bool("fail_on_findings")

    gh = GitHubClient(_input("github_token") or os.environ.get("GITHUB_TOKEN", ""))
    try:
        ctx = ctx_mod.from_event(event_name, payload, gh, mode=mode, trigger_phrase=_input("trigger_phrase", "@dinoai"))
    except ctx_mod.SkipRun as e:
        _notice(f"Skipping: {e}")
        return 0

    if event_name == "pull_request_target":
        _warn("Running on pull_request_target. This action never checks out PR code in the runner, but review the "
              "security implications of that trigger before using it.")
    if ctx.is_fork:
        msg = "This pull request comes from a fork. The agent reviews the base repository's clone, where the fork's " \
              "commits do not exist, so fork PRs are not reviewed."
        if fail_on_findings:
            _error(msg + " fail_on_findings is set, so this cannot be treated as passing.")
            return 1
        _warn(msg)
        return 0
    if ctx.draft and not _bool("review_drafts"):
        _notice("Draft pull request; set review_drafts: true to review drafts.")
        return 0

    _log(f"Reviewing {ctx.repo}#{ctx.number} at {ctx.head_sha[:12]} (base {ctx.base_sha[:12]})")

    files = gh.list_pull_files(ctx.repo, ctx.number)
    reviews = gh.list_reviews(ctx.repo, ctx.number)
    review_comments = gh.list_review_comments(ctx.repo, ctx.number)
    issue_comments = gh.list_issue_comments(ctx.repo, ctx.number)

    last_sha, our_review_ids = find_previous_reviews(reviews)
    if not _bool("incremental", True):
        last_sha = None
    if last_sha == ctx.head_sha and ctx.task is None:
        _notice(f"Head {ctx.head_sha[:12]} was already reviewed; nothing new to review.")
        return 0

    message = ctx_mod.build_message(
        ctx,
        diffstat_text=ctx_mod.diffstat(files),
        comments_text=ctx_mod.comments_block(issue_comments, review_comments, skip_marker="<!-- dinoai-review "),
        already_reported_text=ctx_mod.already_reported(review_comments, review_ids=our_review_ids),
        last_reviewed_sha=last_sha,
        instructions=_input("instructions"),
    )

    agent = ctx_mod.resolve_agent(gh, ctx, _input("agent", "pr-reviewer"))
    if agent is None and _input("agent", "pr-reviewer"):
        _notice(f"No .dinoai/agents/{_input('agent', 'pr-reviewer')}.yml on {ctx.default_branch or 'the default branch'}; "
                "running the built-in reviewer. Add that file to customise it.")

    client = ParadimeClient(_input("api_endpoint"), _input("api_key"), _input("api_secret"))
    session_id, warning = client.trigger_run(
        message=message,
        agent=agent,
        base_branch=ctx.head_sha,
        model_family=_input("model_family") or None,
    )
    _set_output("agent_session_id", session_id)
    _log(f"Started agent session {session_id}")
    if warning:
        _warn(warning)

    cancelled = {"flag": False}

    def _on_signal(signum, _frame):  # noqa: ANN001
        cancelled["flag"] = True
        raise _Cancelled(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    deadline = time.monotonic() + _int("timeout_minutes", 30) * 60
    interval = max(3, _int("poll_interval_seconds", 10))
    seen = 0
    state = None
    try:
        _log("::group::Agent progress")
        while True:
            state = client.read_run(session_id)
            for m in state.messages[seen:]:
                role = m.get("role", "?")
                _log(f"[{role}] {str(m.get('content', '')).strip()[:2000]}")
            seen = len(state.messages)
            if state.is_terminal:
                break
            if time.monotonic() > deadline:
                _warn(f"Timed out after {_int('timeout_minutes', 30)} minutes; stopping the run.")
                client.stop_run(session_id)
                state.status = "stopped"
                break
            time.sleep(interval)
    except _Cancelled as e:
        _warn(f"Received {e}; stopping agent session {session_id}.")
        try:
            client.stop_run(session_id)
        except ParadimeApiError as stop_err:
            _warn(f"Could not stop the session: {stop_err}")
        return 130
    finally:
        _log("::endgroup::")

    assert state is not None
    _set_output("status", state.status)

    agent_texts = state.agent_messages()
    final_text = agent_texts[-1] if agent_texts else ""
    summary, findings, structured = parse_findings(final_text)
    if state.status != "completed":
        summary = f"The review run ended with status `{state.status}`." + (f"\n\n{summary}" if summary else "")
        findings = []
    elif not structured:
        _warn("The agent did not return a structured findings block; posting its message as the summary only.")

    inline, overflow = partition(findings, commentable_lines(files), _int("max_findings", 25))
    session_url = _input("session_url_template").replace("{agent_session_id}", session_id) or None
    body = render_review_body(
        summary=summary,
        overflow=overflow,
        inline_count=len(inline),
        status=state.status,
        head_sha=ctx.head_sha,
        session_id=session_id,
        session_url=session_url,
    )
    _set_output("findings_count", str(len(findings)))
    _set_output("review_body", body)
    _step_summary(body)

    if _bool("post_review", True):
        event = "REQUEST_CHANGES" if (fail_on_findings and findings) else "COMMENT"
        comments = [{"path": f.path, "line": f.line, "side": "RIGHT", "body": render_comment(f)} for f in inline]
        try:
            _post_review(gh, ctx, body=body, event=event, comments=comments)
        except GitHubApiError as e:
            if e.status == 403:
                _error("GitHub refused to post the review (403). Grant the job `permissions: pull-requests: write`.")
                return 1
            raise

    if state.status != "completed":
        _error(f"Agent run ended with status {state.status}")
        return 1
    if fail_on_findings and findings:
        _error(f"{len(findings)} finding(s) reported and fail_on_findings is set")
        return 1
    return 0


def _post_review(gh: GitHubClient, ctx: ctx_mod.PullRequestContext, *, body: str, event: str, comments: list[dict]) -> None:
    try:
        gh.create_review(ctx.repo, ctx.number, commit_id=ctx.head_sha, body=body, event=event, comments=comments)
        _log(f"Posted review with {len(comments)} inline comment(s)")
    except GitHubApiError as e:
        # 422 means a comment position GitHub would not accept (a file past the 300-file listing, a
        # line the diff API elided). Better one review without inline placement than none at all.
        if e.status != 422 or not comments:
            raise
        _warn(f"GitHub rejected the inline positions ({e}); posting the review without inline comments.")
        gh.create_review(ctx.repo, ctx.number, commit_id=ctx.head_sha, body=body, event=event, comments=[])


def main() -> None:
    try:
        sys.exit(run())
    except ParadimeApiError as e:
        _error(str(e))
        sys.exit(1)
    except GitHubApiError as e:
        _error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
