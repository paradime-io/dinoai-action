"""End-to-end run() with the two HTTP clients replaced. Exercises the plumbing tests above don't."""

import json
import os
import tempfile
import unittest
from unittest import mock

from dinoai_action import main
from dinoai_action.paradime_api import RunState

PR = {
    "number": 7, "title": "Add mart", "body": "", "draft": False, "html_url": "https://gh/acme/a/pull/7",
    "user": {"login": "dev"},
    "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "acme/a", "default_branch": "main"}},
    "head": {"sha": "c" * 40, "ref": "feat", "repo": {"full_name": "acme/a"}},
}
PATCH = "@@ -1,2 +1,3 @@\n select\n+  b,\n a\n"
FINAL = ('Done.\n```dinoai-findings\n{"summary": "One issue.", "findings": [{"path": "m.sql", "line": 2, '
         '"severity": "high", "title": "Fan-out", "body": "join multiplies", "suggestion": "  b -- fixed,"}, '
         '{"path": "m.sql", "line": 999, "severity": "low", "title": "Off-diff", "body": "x"}]}\n```')


class FakeGh:
    def __init__(self, *_a, **_k):
        self.reviews_posted = []
        self.reviews = []

    agent_files = set()

    def get_pull(self, repo, n): return PR
    def file_exists(self, repo, path, ref): return (path, ref) in self.agent_files
    def list_pull_files(self, repo, n): return [{"filename": "m.sql", "additions": 1, "deletions": 0, "status": "modified", "patch": PATCH}]
    def list_reviews(self, repo, n): return self.reviews
    def list_review_comments(self, repo, n): return []
    def list_issue_comments(self, repo, n): return []
    def create_review(self, repo, n, **kw): self.reviews_posted.append(kw); return {"id": 1}


class FakeParadime:
    instances = []

    def __init__(self, *_a, **_k):
        self.stopped = False
        self.polls = 0
        FakeParadime.instances.append(self)

    def trigger_run(self, **kw):
        self.trigger_kwargs = kw
        return "sess-1", None

    def read_run(self, sid):
        self.polls += 1
        if self.polls == 1:
            return RunState(status="running", messages=[{"role": "agent", "content": "looking…"}])
        return RunState(status="completed", messages=[{"role": "agent", "content": "looking…"}, {"role": "agent", "content": FINAL}])

    def stop_run(self, sid): self.stopped = True
    def send_message(self, sid, msg): self.sent = getattr(self, "sent", []) + [msg]


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.event = os.path.join(self.tmp.name, "event.json")
        self.out = os.path.join(self.tmp.name, "out.txt")
        self.summary = os.path.join(self.tmp.name, "summary.md")
        open(self.out, "w").close(); open(self.summary, "w").close()
        self.env = {
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": self.event,
            "GITHUB_OUTPUT": self.out, "GITHUB_STEP_SUMMARY": self.summary,
            "INPUT_API_ENDPOINT": "https://api.example/graphql", "INPUT_API_KEY": "prdm_wsp_x",
            "INPUT_GITHUB_TOKEN": "t", "INPUT_POLL_INTERVAL_SECONDS": "3",
        }
        FakeParadime.instances.clear()
        self._write_event({"repository": {"full_name": "acme/a"}, "pull_request": PR})

    def tearDown(self):
        self.tmp.cleanup()

    def _write_event(self, payload):
        with open(self.event, "w") as fh:
            json.dump(payload, fh)

    def _run(self, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        gh = FakeGh()
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(main, "GitHubClient", lambda *a, **k: gh), \
             mock.patch.object(main, "ParadimeClient", FakeParadime), \
             mock.patch.object(main.time, "sleep", lambda *_: None):
            code = main.run()
        return code, gh, FakeParadime.instances[-1] if FakeParadime.instances else None

    def _outputs(self):
        raw = open(self.out).read()
        out = {}
        for line in raw.splitlines():
            if "<<" in line and not line.startswith("ghadelim_"):
                key = line.split("<<", 1)[0]
                out[key] = None
        return raw, out

    def test_happy_path_posts_inline_review(self):
        code, gh, pd = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(pd.trigger_kwargs["base_branch"], "c" * 40)
        self.assertIsNone(pd.trigger_kwargs["agent"])  # no definition file → unnamed run
        self.assertIn("git diff " + "b" * 40 + "..." + "c" * 40, pd.trigger_kwargs["message"])
        self.assertEqual(len(gh.reviews_posted), 1)
        review = gh.reviews_posted[0]
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(review["commit_id"], "c" * 40)
        self.assertEqual(len(review["comments"]), 1)  # line 999 is off-diff → summary
        self.assertEqual(review["comments"][0]["line"], 2)
        self.assertIn("```suggestion", review["comments"][0]["body"])
        self.assertIn("Off-diff", review["body"])
        self.assertIn("<!-- dinoai-review head=" + "c" * 40, review["body"])
        raw, out = self._outputs()
        self.assertIn("agent_session_id", out)
        self.assertIn("findings_count", out)
        self.assertIn("\n2\n", raw)
        self.assertFalse(pd.stopped)

    def test_named_agent_when_definition_exists_on_default_branch(self):
        with mock.patch.object(FakeGh, "agent_files", {(".dinoai/agents/pr-reviewer.yml", "main")}):
            _, _, pd = self._run()
        self.assertEqual(pd.trigger_kwargs["agent"], "pr-reviewer")

    def test_fail_on_findings_requests_changes_and_fails(self):
        code, gh, _ = self._run({"INPUT_FAIL_ON_FINDINGS": "true"})
        self.assertEqual(code, 1)
        self.assertEqual(gh.reviews_posted[0]["event"], "REQUEST_CHANGES")

    def test_fork_is_skipped(self):
        fork = {**PR, "head": {**PR["head"], "repo": {"full_name": "other/a"}}}
        self._write_event({"repository": {"full_name": "acme/a"}, "pull_request": fork})
        code, gh, pd = self._run()
        self.assertEqual(code, 0)
        self.assertIsNone(pd)
        self.assertEqual(gh.reviews_posted, [])

    def test_draft_skipped_unless_enabled(self):
        self._write_event({"repository": {"full_name": "acme/a"}, "pull_request": {**PR, "draft": True}})
        code, _, pd = self._run()
        self.assertEqual(code, 0); self.assertIsNone(pd)
        code, _, pd = self._run({"INPUT_REVIEW_DRAFTS": "true"})
        self.assertEqual(code, 0); self.assertIsNotNone(pd)

    def test_already_reviewed_head_is_noop(self):
        gh_reviews = [{"id": 3, "submitted_at": "2026-01-01T00:00:00Z",
                       "body": "<!-- dinoai-review head=" + "c" * 40 + " session=s0 -->"}]
        with mock.patch.object(FakeGh, "list_reviews", lambda self, r, n: gh_reviews):
            code, gh, pd = self._run()
        self.assertEqual(code, 0)
        self.assertIsNone(pd)

    def test_post_review_false_only_outputs(self):
        code, gh, _ = self._run({"INPUT_POST_REVIEW": "false"})
        self.assertEqual(code, 0)
        self.assertEqual(gh.reviews_posted, [])
        self.assertIn("One issue.", open(self.summary).read())


if __name__ == "__main__":
    unittest.main()


class FollowUpTest(RunTest):
    """When the final message has no block, one follow-up on the same session should fetch it."""

    def test_follow_up_recovers_block(self):
        class Prose(FakeParadime):
            def read_run(self, sid):
                self.polls += 1
                if self.polls == 1:
                    return RunState(status="completed", messages=[{"role": "agent", "content": "Prose only, findings below."}])
                return RunState(status="completed", messages=[{"role": "agent", "content": "Prose only, findings below."},
                                                              {"role": "agent", "content": FINAL}])
        with mock.patch.object(main, "ParadimeClient", Prose), mock.patch.object(main.time, "sleep", lambda *_: None), \
             mock.patch.dict(os.environ, self.env, clear=False), mock.patch.object(main, "GitHubClient", lambda *a, **k: FakeGh()):
            code = main.run()
        self.assertEqual(code, 0)
        pd = FakeParadime.instances[-1]
        self.assertEqual(len(pd.sent), 1)
        raw = open(self.out).read()
        self.assertIn("structured", raw)
        self.assertIn("\n2\n", raw)  # both findings parsed from the follow-up
