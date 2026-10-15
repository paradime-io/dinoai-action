"""Thin GraphQL client for the Paradime public API. Stdlib only."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

TERMINAL_STATUSES = frozenset({"completed", "failed", "expired", "stopped"})
USER_AGENT = "paradime-dinoai-action"

_TRIGGER = """
mutation Trigger($agent: String, $message: String, $baseBranch: String, $modelFamily: String) {
  triggerDinoaiAgentRun(agent: $agent, message: $message, baseBranch: $baseBranch, modelFamily: $modelFamily) {
    ok agentSessionId status warning
  }
}
"""

_READ = """
query Read($id: String!) {
  dinoaiAgentRun(agentSessionId: $id) {
    ok status messages { ts role content }
  }
}
"""

_SEND = """
mutation Send($id: String!, $message: String!) {
  sendDinoaiAgentMessage(agentSessionId: $id, message: $message) { ok status }
}
"""

_STOP = """
mutation Stop($id: String!) {
  stopDinoaiAgentRun(agentSessionId: $id) { ok status }
}
"""


class ParadimeApiError(RuntimeError):
    pass


def _refused_message(endpoint: str, e: urllib.error.HTTPError) -> str:
    # The Paradime API reports a bad, expired or under-scoped key as a GraphQL error on HTTP 200,
    # never as 401/403, so a 401/403 here never means "check the key". Paradime's firewall answers
    # 403 with `Blocked-By: AWS-WAF` when the body looks like an attack, which the PR text in the
    # trigger message can (a relative path with `../`). Anything else was sent by a proxy or firewall
    # on the runner's network, or by a host that is not the Paradime API. Say what answered.
    host = urllib.parse.urlsplit(endpoint).netloc
    headers = e.headers or {}
    if "aws-waf" in (headers.get("Blocked-By") or "").lower():
        return (
            f"Paradime's firewall blocked the request to {host} (HTTP {e.code}). It most likely matched text from "
            "this pull request: a relative path with `../` in the title, description or a comment is the usual "
            "trigger. Remove it, then start a new run; re-running this job replays the original event with the old "
            "text. If there is no `../`, contact Paradime support."
        )
    server = headers.get("Server")
    body = " ".join(e.read().decode(errors="replace").split())[:300]
    return (
        f"The request to {host} got HTTP {e.code}" + (f" (server: {server})" if server else "") + ". "
        "The Paradime API reports a bad API key as an error message, not as HTTP 401 or 403, so this response "
        "most likely came from a proxy or firewall between this runner and Paradime, or api_endpoint is not "
        f"the API Endpoint shown with your key. Check api_endpoint and that this runner can reach {host}."
        + (f" Response: {body}" if body else "")
    )


@dataclass
class RunState:
    status: str
    messages: list[dict] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def agent_messages(self) -> list[str]:
        return [m.get("content", "") for m in self.messages if m.get("role") == "agent"]


class ParadimeClient:
    def __init__(self, endpoint: str, api_key: str, api_secret: str = "", *, workspace_uid: str = "", retries: int = 3):
        if not endpoint.startswith("https://"):
            raise ParadimeApiError("api_endpoint must be an https:// URL")
        self.endpoint = endpoint
        self.retries = retries
        # Account API keys (prdm_cmp_…) are the documented scheme: a bearer token plus the
        # target workspace in X-Paradime-Workspace on every request. The legacy workspace
        # key/secret pair is bound to one workspace and stays supported server-side.
        if api_secret:
            self._headers = {"X-API-KEY": api_key, "X-API-SECRET": api_secret}
        else:
            self._headers = {"Authorization": f"Bearer {api_key}"}
        if workspace_uid:
            self._headers["X-Paradime-Workspace"] = workspace_uid
        elif api_key.startswith("prdm_cmp_"):
            raise ParadimeApiError(
                "An Account API key (prdm_cmp_…) spans workspaces: set `workspace_uid` to the workspace "
                "token the reviews should run in."
            )

    def _gql(self, query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT, **self._headers}
        last_error: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise ParadimeApiError(_refused_message(self.endpoint, e)) from e
                if e.code < 500 or attempt == self.retries - 1:
                    raise ParadimeApiError(f"Paradime API HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
                last_error = e
            except urllib.error.URLError as e:
                if attempt == self.retries - 1:
                    raise ParadimeApiError(f"Could not reach Paradime API: {e.reason}") from e
                last_error = e
            time.sleep(2 ** attempt)
        else:  # pragma: no cover - loop always breaks or raises
            raise ParadimeApiError(str(last_error))

        if payload.get("errors"):
            messages = "; ".join(str(err.get("message", err)) for err in payload["errors"])
            raise ParadimeApiError(f"Paradime API error: {messages}")
        return payload.get("data") or {}

    def trigger_run(
        self, *, message: str, agent: str | None, base_branch: str, model_family: str | None
    ) -> tuple[str, str | None]:
        data = self._gql(
            _TRIGGER,
            {
                "agent": agent or None,
                "message": message,
                "baseBranch": base_branch,
                "modelFamily": model_family or None,
            },
        )
        result = data.get("triggerDinoaiAgentRun") or {}
        session_id = result.get("agentSessionId")
        if not result.get("ok") or not session_id:
            raise ParadimeApiError(f"triggerDinoaiAgentRun did not return a session: {result}")
        return session_id, result.get("warning")

    def read_run(self, session_id: str) -> RunState:
        data = self._gql(_READ, {"id": session_id})
        result = data.get("dinoaiAgentRun") or {}
        # The status is a GraphQL enum, so it comes back as the member name. Normalise once here.
        status = str(result.get("status") or "queued").lower()
        return RunState(status=status, messages=list(result.get("messages") or []))

    def send_message(self, session_id: str, message: str) -> None:
        self._gql(_SEND, {"id": session_id, "message": message})

    def stop_run(self, session_id: str) -> None:
        self._gql(_STOP, {"id": session_id})
