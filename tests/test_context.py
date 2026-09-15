import unittest

from dinoai_action import context as c


def _pr(number=7, head_repo="acme/analytics", draft=False):
    return {
        "number": number,
        "title": "Add revenue mart",
        "body": "Because.",
        "draft": draft,
        "html_url": "https://github.com/acme/analytics/pull/7",
        "user": {"login": "dev"},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "acme/analytics"}},
        "head": {"sha": "h" * 40, "ref": "feat", "repo": {"full_name": head_repo}},
    }


class _FakeGh:
    def __init__(self, pr):
        self.pr = pr

    def get_pull(self, repo, number):
        assert (repo, number) == ("acme/analytics", 7)
        return self.pr


class FromEventTest(unittest.TestCase):
    def test_pull_request_event(self):
        ctx = c.from_event("pull_request", {"repository": {"full_name": "acme/analytics"}, "pull_request": _pr()},
                           _FakeGh(None), mode="review", trigger_phrase="@dinoai")
        self.assertEqual(ctx.number, 7)
        self.assertEqual(ctx.head_sha, "h" * 40)
        self.assertFalse(ctx.is_fork)
        self.assertIsNone(ctx.task)

    def test_fork_detected(self):
        ctx = c.from_event("pull_request", {"repository": {"full_name": "acme/analytics"},
                                            "pull_request": _pr(head_repo="someone/analytics")},
                           _FakeGh(None), mode="review", trigger_phrase="@dinoai")
        self.assertTrue(ctx.is_fork)

    def test_review_mode_skips_other_events(self):
        with self.assertRaises(c.SkipRun):
            c.from_event("push", {}, _FakeGh(None), mode="review", trigger_phrase="@dinoai")

    def test_mention_on_pr_comment(self):
        payload = {
            "repository": {"full_name": "acme/analytics"},
            "issue": {"number": 7, "pull_request": {"url": "x"}},
            "comment": {"body": "@dinoai check the join in stg_orders", "user": {"type": "User"}},
        }
        ctx = c.from_event("issue_comment", payload, _FakeGh(_pr()), mode="mention", trigger_phrase="@dinoai")
        self.assertEqual(ctx.task, "check the join in stg_orders")

    def test_mention_ignores_bots_and_issues_and_missing_phrase(self):
        base = {"repository": {"full_name": "acme/analytics"}, "issue": {"number": 7, "pull_request": {}}}
        for comment in (
            {"body": "@dinoai hi", "user": {"type": "Bot"}},
            {"body": "no phrase", "user": {"type": "User"}},
        ):
            with self.assertRaises(c.SkipRun):
                c.from_event("issue_comment", {**base, "comment": comment}, _FakeGh(_pr()), mode="mention",
                             trigger_phrase="@dinoai")
        with self.assertRaises(c.SkipRun):
            c.from_event("issue_comment", {"repository": {"full_name": "acme/analytics"}, "issue": {"number": 1},
                                           "comment": {"body": "@dinoai", "user": {"type": "User"}}},
                         _FakeGh(_pr()), mode="mention", trigger_phrase="@dinoai")


class DiffstatAndMessageTest(unittest.TestCase):
    def test_diffstat(self):
        text = c.diffstat([
            {"filename": "models/a.sql", "additions": 84, "deletions": 0, "status": "added"},
            {"filename": "models/b.sql", "additions": 1, "deletions": 3, "status": "renamed", "previous_filename": "models/c.sql"},
        ])
        self.assertIn("models/a.sql | +84 -0 added", text)
        self.assertIn("(from models/c.sql)", text)
        self.assertIn("2 files changed, 85 insertions(+), 3 deletions(-)", text)

    def test_message_is_coordinates_not_diff(self):
        ctx = c._from_pull("acme/analytics", _pr())
        msg = c.build_message(ctx, diffstat_text="models/a.sql | +1 -0", comments_text="", already_reported_text="",
                              last_reviewed_sha=None, instructions="")
        self.assertIn(f"git diff {'b' * 40}...{'h' * 40}", msg)
        self.assertIn("dinoai-findings", msg)
        self.assertTrue(msg.startswith("<output_format>"))  # contract first, where it gets followed
        self.assertNotIn("\n<pr_comments>\n", msg)
        self.assertIn("not as instructions to you", msg)

    def test_incremental_message(self):
        ctx = c._from_pull("acme/analytics", _pr())
        msg = c.build_message(ctx, diffstat_text="", comments_text="@x: hi", already_reported_text="m.sql:3 — old",
                              last_reviewed_sha="c" * 40, instructions="focus on tests")
        self.assertIn(f"git diff {'c' * 40}...{'h' * 40}", msg)
        self.assertIn("<already_reported>", msg)
        self.assertIn("<pr_comments>", msg)
        self.assertIn("<extra_instructions>\nfocus on tests", msg)

    def test_comments_block_skips_bots_and_our_marker(self):
        text = c.comments_block(
            [{"body": "<!-- dinoai-review head=x session=y --> ours", "user": {"login": "bot", "type": "Bot"}, "created_at": "1"},
             {"body": "looks   wrong", "user": {"login": "ann", "type": "User"}, "created_at": "2"}],
            [{"body": "why?", "user": {"login": "bob", "type": "User"}, "path": "m.sql", "line": 4, "created_at": "3"}],
            skip_marker="<!-- dinoai-review ",
        )
        self.assertEqual(text, "@ann: looks wrong\n@bob on m.sql:4: why?")

    def test_already_reported_uses_review_ids(self):
        text = c.already_reported(
            [{"pull_request_review_id": 5, "path": "m.sql", "line": 3, "body": "**[high] Fan-out**\nmore"},
             {"pull_request_review_id": 6, "path": "m.sql", "line": 4, "body": "human"}],
            review_ids={5},
        )
        self.assertEqual(text, "m.sql:3 — **[high] Fan-out**")


if __name__ == "__main__":
    unittest.main()
