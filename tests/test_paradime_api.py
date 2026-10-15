import email.message
import io
import json
import unittest
import urllib.error
from unittest import mock

from dinoai_action.paradime_api import ParadimeApiError, ParadimeClient, RunState

ENDPOINT = "https://api.paradime.io/api/v1/abc/graphql"


def _http_error(code: int, body: bytes = b"", server: str | None = None, blocked_by: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if server:
        headers["Server"] = server
    if blocked_by:
        headers["Blocked-By"] = blocked_by
    return urllib.error.HTTPError(ENDPOINT, code, "refused", headers, io.BytesIO(body))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RefusedTest(unittest.TestCase):
    def _trigger_error(self, error: Exception) -> str:
        client = ParadimeClient(ENDPOINT, "prdm_cmp_abc", workspace_uid="o4nrp6fa18m2vmwm")
        with mock.patch("dinoai_action.paradime_api.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ParadimeApiError) as caught:
                client.trigger_run(message="m", agent=None, base_branch="sha", model_family=None)
        return str(caught.exception)

    def test_403_names_what_answered_not_the_key(self):
        msg = self._trigger_error(_http_error(403, b"<html>\n  Access denied by policy\n</html>", server="corp-proxy"))
        self.assertIn("The request to api.paradime.io got HTTP 403 (server: corp-proxy)", msg)
        self.assertIn("Response: <html> Access denied by policy </html>", msg)
        self.assertIn("proxy or firewall", msg)
        self.assertNotIn("dinoai:agent:trigger", msg)

    def test_firewall_block_points_at_the_pr_text(self):
        error = _http_error(403, b"<html><title>Bot or Not?</title></html>", server="awselb/2.0", blocked_by="AWS-WAF")
        msg = self._trigger_error(error)
        self.assertIn("Paradime's firewall blocked the request to api.paradime.io (HTTP 403)", msg)
        self.assertIn("`../`", msg)
        self.assertIn("re-running this job replays the original event", msg)
        self.assertNotIn("Bot or Not", msg)
        self.assertNotIn("dinoai:agent:trigger", msg)

    def test_401_without_headers_or_body(self):
        msg = self._trigger_error(urllib.error.HTTPError(ENDPOINT, 401, "refused", None, None))
        self.assertIn("The request to api.paradime.io got HTTP 401.", msg)
        self.assertNotIn("server:", msg)
        self.assertNotIn("Response:", msg)

    def test_bad_key_is_a_graphql_error(self):
        # What Paradime actually sends for a bad key: HTTP 200 with a GraphQL error.
        payload = {"errors": [{"message": "The provided API key is either invalid, has expired, or does not have the required permissions."}]}
        client = ParadimeClient(ENDPOINT, "prdm_cmp_abc", workspace_uid="o4nrp6fa18m2vmwm")
        with mock.patch("dinoai_action.paradime_api.urllib.request.urlopen", return_value=_Response(json.dumps(payload).encode())):
            with self.assertRaises(ParadimeApiError) as caught:
                client.trigger_run(message="m", agent=None, base_branch="sha", model_family=None)
        self.assertEqual(str(caught.exception), f"Paradime API error: {payload['errors'][0]['message']}")

    def test_sends_user_agent(self):
        client = ParadimeClient(ENDPOINT, "prdm_cmp_abc", workspace_uid="o4nrp6fa18m2vmwm")
        payload = {"data": {"triggerDinoaiAgentRun": {"ok": True, "agentSessionId": "s1", "status": "queued", "warning": None}}}
        with mock.patch("dinoai_action.paradime_api.urllib.request.urlopen", return_value=_Response(json.dumps(payload).encode())) as urlopen:
            self.assertEqual(client.trigger_run(message="m", agent=None, base_branch="sha", model_family=None), ("s1", None))
        self.assertEqual(urlopen.call_args.args[0].get_header("User-agent"), "paradime-dinoai-action")


class ClientTest(unittest.TestCase):
    def test_bearer_headers(self):
        c = ParadimeClient("https://api.example/graphql", "prdm_wsp_abc")
        self.assertEqual(c._headers, {"Authorization": "Bearer prdm_wsp_abc"})

    def test_legacy_headers(self):
        c = ParadimeClient("https://api.example/graphql", "k", "s")
        self.assertEqual(c._headers, {"X-API-KEY": "k", "X-API-SECRET": "s"})

    def test_company_key_needs_workspace(self):
        with self.assertRaises(ParadimeApiError):
            ParadimeClient("https://api.example/graphql", "prdm_cmp_abc")
        c = ParadimeClient("https://api.example/graphql", "prdm_cmp_abc", workspace_uid="o4nrp6fa18m2vmwm")
        self.assertEqual(c._headers["X-Paradime-Workspace"], "o4nrp6fa18m2vmwm")

    def test_workspace_key_ignores_missing_uid(self):
        c = ParadimeClient("https://api.example/graphql", "prdm_wsp_abc")
        self.assertNotIn("X-Paradime-Workspace", c._headers)

    def test_requires_https(self):
        with self.assertRaises(ParadimeApiError):
            ParadimeClient("http://api.example/graphql", "k")

    def test_run_state(self):
        s = RunState(status="running", messages=[{"role": "agent", "content": "a"}, {"role": "user", "content": "u"}])
        self.assertFalse(s.is_terminal)
        self.assertEqual(s.agent_messages(), ["a"])
        self.assertTrue(RunState(status="completed").is_terminal)


if __name__ == "__main__":
    unittest.main()
