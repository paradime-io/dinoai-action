import unittest

from dinoai_action.paradime_api import ParadimeApiError, ParadimeClient, RunState


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
