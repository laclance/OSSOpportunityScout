from __future__ import annotations

import unittest

from bountyscout import reporting
from tests.helpers import candidate


class MarkdownFormattingTests(unittest.TestCase):
    def test_github_report_ref_redirects_issue_and_pr_urls_only(self) -> None:
        self.assertEqual(
            reporting.github_report_ref(
                "https://github.com/a/b/issues/1 "
                "https://github.com/a/b/pull/2 https://example.test/x"
            ),
            "https://redirect.github.com/a/b/issues/1 "
            "https://redirect.github.com/a/b/pull/2 https://example.test/x",
        )
        self.assertEqual(reporting.github_report_ref(None), "")

    def test_markdown_label_escapes_link_breaking_characters(self) -> None:
        self.assertEqual(
            reporting.markdown_label(r"Fix [parser] C:\tmp"),
            r"Fix \[parser\] C:\\tmp",
        )

    def test_strategic_candidate_explains_ranking_delta(self) -> None:
        rendered = reporting.markdown_candidate(candidate(), 1)
        self.assertIn("**Career score:** 80/100", rendered)
        self.assertIn("**Priority score:** 93/100", rendered)
        self.assertIn(
            "**Execution adjustment:** +13 (career 80 → priority 93)",
            rendered,
        )
        self.assertIn(
            "**Priority basis:** 1–3h execution bonus, no visible competition bonus",
            rendered,
        )
        self.assertIn("**Effort basis:** bounded deterministic bug signal", rendered)

    def test_negative_execution_adjustment_is_rendered_without_plus_sign(self) -> None:
        rendered = reporting.markdown_candidate(
            candidate(career_score=90, priority_score=76, effort="1d+", competition="high"),
            1,
        )
        self.assertIn(
            "**Execution adjustment:** -14 (career 90 → priority 76)",
            rendered,
        )

    def test_paid_candidate_keeps_cash_focused_output(self) -> None:
        rendered = reporting.markdown_candidate(
            candidate(
                paid=True,
                reward="$100",
                payment_confidence=100,
                cash_score=85,
                career_score=70,
                priority_score=85,
                expected_hourly=50.0,
                cash_reasons=["payment confidence 100/100"],
                priority_reasons=[],
            ),
            2,
        )
        self.assertIn("**Cash score:** 85/100", rendered)
        self.assertIn("**Expected hourly value:** ~$50/h", rendered)
        self.assertNotIn("**Career score:**", rendered)
        self.assertNotIn("**Priority basis:**", rendered)

    def test_missing_guide_and_labels_have_readable_fallbacks(self) -> None:
        rendered = reporting.markdown_candidate(
            candidate(contribution_guide=None, labels=[]),
            1,
        )
        self.assertIn("not found at common paths", rendered)
        self.assertIn("**Labels:** none", rendered)


class NotificationFormattingTests(unittest.TestCase):
    def test_notification_candidate_truncates_long_title(self) -> None:
        lines = reporting.notification_candidate(candidate(title="x" * 180), 1)
        self.assertLessEqual(len(lines[0]), 160)
        self.assertIn("priority: 93/100", "\n".join(lines))

    def test_paid_notification_stays_cash_focused(self) -> None:
        lines = reporting.notification_candidate(
            candidate(paid=True, reward="$50", cash_score=88),
            1,
        )
        text = "\n".join(lines)
        self.assertIn("paid bounty", text)
        self.assertIn("cash: 88/100", text)
        self.assertNotIn("career:", text)

    def test_notification_message_is_length_safe_and_reports_omissions(self) -> None:
        queue = [
            candidate(
                issue_number=index,
                title="x" * 100,
                url=f"https://github.com/example/project/issues/{index}",
            )
            for index in range(1, 9)
        ]
        message = reporting.notification_message(
            queue,
            "2026-10-01 11:00 UTC",
            max_chars=650,
        )
        self.assertLessEqual(len(message), 650)
        self.assertIn("more ranked candidate(s) in the GitHub report", message)

    def test_notification_message_surfaces_coverage_warning(self) -> None:
        message = reporting.notification_message(
            [candidate()],
            "now",
            warning="Strategic verification coverage is incomplete.",
        )
        self.assertIn("⚠️ Strategic verification coverage is incomplete.", message)
        self.assertIn("#42", message)

    def test_notification_message_keeps_all_entries_when_they_fit(self) -> None:
        queue = [candidate(issue_number=1), candidate(issue_number=2)]
        message = reporting.notification_message(queue, "now", max_chars=1900)
        self.assertIn("#1", message)
        self.assertIn("#2", message)
        self.assertNotIn("more ranked candidate", message)


class ReportAssemblyTests(unittest.TestCase):
    def test_rejection_summary_combines_lanes(self) -> None:
        self.assertEqual(
            reporting.rejection_summary(
                {"active claim": 2, "low score": 1},
                {"active claim": 3, "not ready": 4},
            ),
            {"active claim": 5, "low score": 1, "not ready": 4},
        )

    def test_markdown_examples_redirects_links_and_escapes_titles(self) -> None:
        rendered = reporting.markdown_examples(
            "Verification rejects",
            [
                {
                    "title": "Fix [parser]",
                    "url": "https://github.com/a/b/issues/1",
                    "reason": "existing PR https://github.com/a/b/pull/2",
                }
            ],
        )
        self.assertIn("Fix \\[parser\\]", rendered)
        self.assertIn("https://redirect.github.com/a/b/issues/1", rendered)
        self.assertIn("https://redirect.github.com/a/b/pull/2", rendered)
        self.assertEqual(reporting.markdown_examples("x", []), "")

    def test_audit_summary_groups_repeated_tuning_signals(self) -> None:
        summary = reporting.audit_summary(
            [
                {"reason": "outside adaptive pool"},
                {"reason": "outside adaptive pool"},
                {"reason": "near miss"},
            ]
        )
        self.assertEqual(summary, "outside adaptive pool ×2; near miss ×1")
        self.assertEqual(reporting.audit_summary([]), "")

    def test_github_report_body_is_decision_oriented(self) -> None:
        body = reporting.github_report_body(
            [candidate()],
            "2026-10-01 11:00 UTC",
            verification_examples=[
                {
                    "title": "Claimed issue",
                    "url": "https://github.com/a/b/issues/1",
                    "reason": "active claim by @dev",
                }
            ],
            strategic_audit=[
                {
                    "title": "Potential miss",
                    "url": "https://github.com/c/d/issues/2",
                    "reason": "outside adaptive pool",
                },
                {
                    "title": "Potential miss 2",
                    "url": "https://github.com/c/d/issues/3",
                    "reason": "outside adaptive pool",
                },
            ],
            reject_counts={"active claim by @dev": 3, "not ready": 1},
        )
        self.assertTrue(
            body.startswith("<!-- bountyscout-report: automated; actionable: false -->")
        )
        self.assertIn(
            "Automated OSS Opportunity Scout scan report — not a development task.",
            body,
        )
        self.assertIn(
            "Paid candidates reuse OSS Opportunity Scout's existing payment/competition filters unchanged.",
            body,
        )
        self.assertIn("Do not claim this report or open a pull request to resolve it.", body)
        self.assertIn("career score measures long-term value", body)
        self.assertIn("### Verification summary", body)
        self.assertIn("**Filtered candidates:** 4", body)
        self.assertIn("active claim by @dev ×3", body)
        self.assertIn("### Verification rejects", body)
        self.assertIn("### Potential scanner misses / tuning candidates", body)
        self.assertIn("**Audit summary:** outside adaptive pool ×2", body)
        self.assertIn("**Execution adjustment:** +13", body)

    def test_report_surfaces_coverage_warning_before_candidates(self) -> None:
        warning = "Strategic verification coverage is incomplete."
        body = reporting.github_report_body(
            [candidate()],
            "now",
            coverage_warning=warning,
        )
        self.assertIn("> [!WARNING]", body)
        self.assertIn(warning, body)
        self.assertLess(body.index(warning), body.index("#### 1."))

    def test_report_without_rejects_or_audit_has_no_empty_sections(self) -> None:
        body = reporting.github_report_body([candidate()], "now")
        self.assertNotIn("Verification summary", body)
        self.assertNotIn("Verification rejects", body)
        self.assertNotIn("Potential scanner misses", body)

    def test_report_title_pluralizes(self) -> None:
        self.assertEqual(
            reporting.github_report_title(1),
            "📊 SCAN REPORT — OSS Opportunity Queue: 1 new verified candidate",
        )
        self.assertEqual(
            reporting.github_report_title(2),
            "📊 SCAN REPORT — OSS Opportunity Queue: 2 new verified candidates",
        )


if __name__ == "__main__":
    unittest.main()
