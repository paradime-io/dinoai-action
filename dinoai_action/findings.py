"""Parse the agent's structured findings and map them onto reviewable diff lines."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# The agent is asked to close its final message with one fenced JSON block. Accept a few
# fence labels so a model that writes ```json instead of ```dinoai-findings still parses.
_FENCE_RE = re.compile(r"```(?:dinoai-findings|json)\s*\n(.*?)\n```", re.DOTALL)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

REVIEW_MARKER = "<!-- dinoai-review head={head_sha} session={session_id} -->"
_MARKER_RE = re.compile(r"<!-- dinoai-review head=([0-9a-f]{7,40}) session=([^ >]+) -->")


@dataclass
class Finding:
    path: str
    line: int | None
    severity: str
    title: str
    body: str
    suggestion: str | None = None

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, len(SEVERITIES))


def parse_reviewed_head(text: str) -> str | None:
    """The `reviewed_head` the agent reported in its findings block, if any."""
    for match in _FENCE_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "findings" in data:
            head = str(data.get("reviewed_head") or "").strip()
            return head or None
    return None


def parse_findings(text: str) -> tuple[str, list[Finding], bool]:
    """Split the agent's final message into (summary, findings, structured).

    ``structured`` is False when no findings block was found; the whole message is then
    the summary and nothing is posted inline.
    """
    for match in _FENCE_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "findings" not in data:
            continue
        summary = str(data.get("summary") or "").strip()
        if not summary:
            summary = text[: match.start()].strip()
        findings = [f for f in (_coerce(item) for item in data.get("findings") or []) if f is not None]
        return summary, findings, True
    return text.strip(), [], False


def parse_findings_from_messages(texts: list[str]) -> tuple[str, list[Finding], bool]:
    """Like parse_findings, but looks through every agent message, newest first, for the block.

    The summary falls back to the newest message when no block exists anywhere.
    """
    for text in reversed(texts):
        summary, findings, structured = parse_findings(text)
        if structured:
            return summary, findings, True
    return (texts[-1].strip() if texts else ""), [], False


def _coerce(item: object) -> Finding | None:
    if not isinstance(item, dict):
        return None
    path = str(item.get("path") or "").strip().lstrip("./")
    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or item.get("description") or "").strip()
    if not path or not (title or body):
        return None
    line_raw = item.get("line")
    line = int(line_raw) if isinstance(line_raw, (int, float, str)) and str(line_raw).strip().isdigit() else None
    severity = str(item.get("severity") or "medium").strip().lower()
    if severity not in _SEVERITY_RANK:
        severity = "medium"
    suggestion = item.get("suggestion")
    return Finding(
        path=path,
        line=line,
        severity=severity,
        title=title or body.splitlines()[0][:120],
        body=body,
        suggestion=str(suggestion) if suggestion else None,
    )


def commentable_lines(files: list[dict]) -> dict[str, set[int]]:
    """RIGHT-side line numbers present in each file's patch.

    GitHub only accepts review comments on lines that appear in the diff (added or context),
    and rejects the whole review with a 422 otherwise. Parse each hunk header and walk it.
    """
    result: dict[str, set[int]] = {}
    for f in files:
        patch = f.get("patch")
        path = f.get("filename")
        if not patch or not path:
            continue
        lines: set[int] = set()
        current = 0
        for raw in patch.splitlines():
            m = _HUNK_RE.match(raw)
            if m:
                current = int(m.group(1))
                continue
            if raw.startswith("-"):
                continue
            if raw.startswith("\\"):  # "\ No newline at end of file"
                continue
            lines.add(current)
            current += 1
        result[path] = lines
    return result


def partition(findings: list[Finding], commentable: dict[str, set[int]], max_inline: int) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (inline, summary-only), most severe first.

    A finding goes inline only if its path and line are commentable; everything else, and
    everything past ``max_inline``, is listed in the review body instead of being dropped.
    """
    ordered = sorted(findings, key=lambda f: (f.rank, f.path, f.line or 0))
    inline: list[Finding] = []
    overflow: list[Finding] = []
    for f in ordered:
        placeable = f.line is not None and f.line in commentable.get(f.path, set())
        if placeable and len(inline) < max_inline:
            inline.append(f)
        else:
            overflow.append(f)
    return inline, overflow


def render_comment(f: Finding) -> str:
    parts = [f"**[{f.severity}] {f.title}**"]
    if f.body and f.body != f.title:
        parts.append(f.body)
    if f.suggestion:
        parts.append(f"```suggestion\n{f.suggestion.rstrip()}\n```")
    return "\n\n".join(parts)


def render_review_body(
    *,
    summary: str,
    overflow: list[Finding],
    inline_count: int,
    status: str,
    head_sha: str,
    session_id: str,
    session_url: str | None,
    structured: bool = True,
    reviewed: bool = True,
) -> str:
    total = inline_count + len(overflow)
    if not structured:
        heading = "### DinoAI review"
    else:
        heading = "### DinoAI review" + (f" — {total} finding{'s' if total != 1 else ''}" if total else " — no findings")
    parts = [heading, summary or "_The agent returned no summary._"]
    if overflow:
        parts.append("#### Further findings")
        parts.extend(
            f"- **[{f.severity}]** `{f.path}`{f':{f.line}' if f.line else ''} — {f.title}" for f in overflow
        )
    footer = [f"Reviewed `{head_sha[:12]}` · run `{status}` · session `{session_id}`"]
    if session_url:
        footer.append(f"[View in Paradime]({session_url})")
    parts.append("<sub>" + " · ".join(footer) + "</sub>")
    # The marker is what a later run reads as "this commit was reviewed". A run that failed or
    # was stopped never looked at the tree, so stamping it would make the next push diff from a
    # commit nobody reviewed and skip whatever changed in between.
    if reviewed:
        parts.append(REVIEW_MARKER.format(head_sha=head_sha, session_id=session_id))
    return "\n\n".join(parts)


def find_previous_reviews(reviews: list[dict]) -> tuple[str | None, set[int]]:
    """(head SHA of the latest review this action posted, ids of all of them)."""
    ours = [(r.get("submitted_at") or "", r) for r in reviews if _MARKER_RE.search(r.get("body") or "")]
    ours.sort(key=lambda t: t[0], reverse=True)
    if not ours:
        return None, set()
    latest = _MARKER_RE.search(ours[0][1].get("body") or "")
    return (latest.group(1) if latest else None), {int(r.get("id")) for _, r in ours if r.get("id") is not None}
