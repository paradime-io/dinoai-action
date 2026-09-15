import unittest

from dinoai_action.findings import (
    Finding,
    commentable_lines,
    find_previous_reviews,
    parse_findings,
    partition,
    render_review_body,
)

PATCH = (
    "@@ -1,3 +1,4 @@\n"
    " select\n"
    "-  a\n"
    "+  a,\n"
    "+  b\n"
    " from t\n"
    "@@ -10,2 +11,2 @@\n"
    "-x\n"
    "+y\n"
    " z\n"
)


class ParseFindingsTest(unittest.TestCase):
    def test_structured_block(self):
        text = (
            "Looked at everything.\n\n```dinoai-findings\n"
            '{"summary": "Two problems.", "findings": [{"path": "models/a.sql", "line": 3, '
            '"severity": "high", "title": "Fan-out", "body": "join multiplies rows"}]}\n```'
        )
        summary, findings, structured = parse_findings(text)
        self.assertTrue(structured)
        self.assertEqual(summary, "Two problems.")
        self.assertEqual(findings[0].path, "models/a.sql")
        self.assertEqual(findings[0].line, 3)
        self.assertEqual(findings[0].severity, "high")

    def test_json_fence_label_accepted(self):
        text = '```json\n{"summary": "ok", "findings": []}\n```'
        summary, findings, structured = parse_findings(text)
        self.assertTrue(structured)
        self.assertEqual(findings, [])

    def test_unstructured_falls_back_to_summary(self):
        summary, findings, structured = parse_findings("Just prose, no block.")
        self.assertFalse(structured)
        self.assertEqual(summary, "Just prose, no block.")
        self.assertEqual(findings, [])

    def test_bad_severity_and_line_are_coerced(self):
        text = '```json\n{"summary": "s", "findings": [{"path": "./m.sql", "line": "abc", "severity": "wat", "title": "t"}]}\n```'
        _, findings, _ = parse_findings(text)
        self.assertEqual(findings[0].path, "m.sql")
        self.assertIsNone(findings[0].line)
        self.assertEqual(findings[0].severity, "medium")

    def test_empty_summary_uses_prose_before_block(self):
        text = 'Prose summary here.\n```json\n{"findings": []}\n```'
        summary, _, _ = parse_findings(text)
        self.assertEqual(summary, "Prose summary here.")


class CommentableLinesTest(unittest.TestCase):
    def test_walks_hunks_on_right_side(self):
        lines = commentable_lines([{"filename": "m.sql", "patch": PATCH}])
        self.assertEqual(lines["m.sql"], {1, 2, 3, 4, 11, 12})

    def test_file_without_patch_is_not_commentable(self):
        self.assertEqual(commentable_lines([{"filename": "big.csv"}]), {})


class PartitionTest(unittest.TestCase):
    def _f(self, path, line, sev):
        return Finding(path=path, line=line, severity=sev, title="t", body="b")

    def test_orders_by_severity_and_respects_cap(self):
        commentable = {"m.sql": {1, 2, 3}}
        findings = [self._f("m.sql", 1, "low"), self._f("m.sql", 2, "critical"), self._f("m.sql", 3, "high")]
        inline, overflow = partition(findings, commentable, max_inline=2)
        self.assertEqual([f.severity for f in inline], ["critical", "high"])
        self.assertEqual([f.severity for f in overflow], ["low"])

    def test_uncommentable_line_goes_to_overflow(self):
        inline, overflow = partition([self._f("m.sql", 99, "high")], {"m.sql": {1}}, 25)
        self.assertEqual(inline, [])
        self.assertEqual(len(overflow), 1)


class MarkerTest(unittest.TestCase):
    def test_round_trip_and_latest_wins(self):
        body_old = render_review_body(summary="s", overflow=[], inline_count=0, status="completed",
                                      head_sha="a" * 40, session_id="s1", session_url=None)
        body_new = render_review_body(summary="s", overflow=[], inline_count=0, status="completed",
                                      head_sha="b" * 40, session_id="s2", session_url=None)
        reviews = [
            {"id": 1, "submitted_at": "2026-01-01T00:00:00Z", "body": body_old},
            {"id": 2, "submitted_at": "2026-01-02T00:00:00Z", "body": body_new},
            {"id": 3, "submitted_at": "2026-01-03T00:00:00Z", "body": "a human review"},
        ]
        sha, ids = find_previous_reviews(reviews)
        self.assertEqual(sha, "b" * 40)
        self.assertEqual(ids, {1, 2})

    def test_no_previous(self):
        self.assertEqual(find_previous_reviews([{"id": 9, "body": "hi"}]), (None, set()))


if __name__ == "__main__":
    unittest.main()
