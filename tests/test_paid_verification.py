from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from bountyscout import github, paid_verification
from bountyscout.types import GitHubIssue
from tests.helpers import issue


class ExistingImplementationPrTests(unittest.TestCase):
    def test_detects_open_cross_referenced_pr_and_ignores_malformed_entries(self) -> None:
        timeline: list[object] = [
            "bad",
            {"event": "commented"},
            {"event": "cross-referenced", "source": "bad"},
            {"event": "cross-referenced", "source": {"issue": "bad"}},
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "closed",
                        "html_url": "https://github.com/acme/widget/pull/8",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {"issue": {"pull_request": {}, "state": "open"}},
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/acme/widget/pull/9",
                    }
                },
            },
        ]
        calls: list[tuple[str, str | None]] = []

        def fetch_json(url: str, token: str | None) -> Any:
            calls.append((url, token))
            return timeline

        self.assertEqual(
            paid_verification.has_existing_implementation_pr(
                "acme/widget",
                42,
                "tok",
                fetch_json=fetch_json,
            ),
            "existing open implementation PR: https://github.com/acme/widget/pull/9",
        )
        self.assertEqual(
            calls,
            [
                (
                    "https://api.github.com/repos/acme/widget/issues/42/timeline?per_page=100",
                    "tok",
                )
            ],
        )

    def test_failed_or_non_list_timeline_returns_none(self) -> None:
        values: tuple[object, ...] = (None, {})
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(
                    paid_verification.has_existing_implementation_pr(
                        "acme/widget",
                        42,
                        "tok",
                        fetch_json=lambda *_args, value=value: value,
                    )
                )

    def test_default_transport_is_used_once(self) -> None:
        with patch.object(github, "github_get", return_value=[]) as getter:
            self.assertIsNone(
                paid_verification.has_existing_implementation_pr("acme/widget", 42, "tok")
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/acme/widget/issues/42/timeline?per_page=100",
            "tok",
        )


class ActiveClaimTests(unittest.TestCase):
    def test_zero_comments_skips_request(self) -> None:
        calls: list[str] = []

        def fetch_json(url: str, _token: str | None) -> Any:
            calls.append(url)
            return []

        self.assertIsNone(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                0,
                "tok",
                fetch_json=fetch_json,
            )
        )
        self.assertEqual(calls, [])

    def test_failed_or_non_list_comments_returns_none(self) -> None:
        self.assertIsNone(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                2,
                "tok",
                fetch_json=lambda *_: None,
            )
        )

    def test_known_claim_uses_expected_query_and_author(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def fetch_json(url: str, token: str | None) -> Any:
            calls.append((url, token))
            return [
                "bad",
                {"body": "Thanks for the report."},
                {"body": "I'm working on this", "user": {"login": "dev"}},
            ]

        self.assertEqual(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                99,
                "tok",
                fetch_json=fetch_json,
            ),
            "active claim by @dev",
        )
        self.assertEqual(len(calls), 1)
        url, token = calls[0]
        self.assertEqual(token, "tok")
        self.assertIn("per_page=30", url)
        self.assertIn("sort=created", url)
        self.assertIn("direction=desc", url)

    def test_claim_falls_back_to_someone_and_non_claim_returns_none(self) -> None:
        self.assertEqual(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                1,
                None,
                fetch_json=lambda *_: [{"body": "/attempt"}],
            ),
            "active claim by @someone",
        )
        self.assertIsNone(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                1,
                None,
                fetch_json=lambda *_: [{"body": "Interesting issue"}],
            )
        )

    def test_default_transport_is_used(self) -> None:
        with patch.object(github, "github_get", return_value=[]) as getter:
            self.assertIsNone(paid_verification.active_claim_reason("acme/widget", 42, 2, "tok"))
        getter.assert_called_once()
        self.assertIn("per_page=2", getter.call_args.args[0])


class CandidateRejectionTests(unittest.TestCase):
    def test_early_rejection_precedence_skips_competition(self) -> None:
        def unexpected_pr(*_args: object) -> str | None:
            raise AssertionError("existing PR checker should not run")

        def unexpected_claim(*_args: object) -> str | None:
            raise AssertionError("active claim checker should not run")

        cases: list[tuple[GitHubIssue, str]] = [
            (
                issue(body="[Bounty proposal] $100 bounty-watch"),
                "unfunded bounty proposal, not an existing award",
            ),
            (
                issue(body="bounty-watch $100 reward"),
                "meta/monitoring alert, not a contributor task",
            ),
            (
                issue(title="Plain bug", body="No compensation here"),
                "no explicit payment signal",
            ),
        ]
        for item, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    paid_verification.candidate_rejection_reason(
                        item,
                        "tok",
                        existing_pr_checker=unexpected_pr,
                        active_claim_checker=unexpected_claim,
                    ),
                    (expected, None),
                )

    def test_invalid_issue_url_preserves_signal_without_competition(self) -> None:
        item = issue(html_url="bad", body="bounty $100")

        def unexpected_pr(*_args: object) -> str | None:
            raise AssertionError("existing PR checker should not run")

        reason, signal = paid_verification.candidate_rejection_reason(
            item,
            "tok",
            existing_pr_checker=unexpected_pr,
        )
        self.assertEqual(reason, "could not identify repository/issue number")
        self.assertIsNotNone(signal)

    def test_existing_pr_precedes_claim(self) -> None:
        claim_calls: list[int] = []

        def claim_checker(
            _repo: str,
            _number: int,
            _comments: int,
            _token: str | None,
        ) -> str | None:
            claim_calls.append(1)
            return "active claim"

        reason, signal = paid_verification.candidate_rejection_reason(
            issue(body="bounty $100", comments=2),
            "tok",
            existing_pr_checker=lambda *_: "existing pr",
            active_claim_checker=claim_checker,
        )
        self.assertEqual(reason, "existing pr")
        self.assertIsNotNone(signal)
        self.assertEqual(claim_calls, [])

    def test_claim_and_acceptance_paths(self) -> None:
        reason, signal = paid_verification.candidate_rejection_reason(
            issue(body="bounty $100", comments=1),
            "tok",
            existing_pr_checker=lambda *_: None,
            active_claim_checker=lambda *_: "active claim",
        )
        self.assertEqual(reason, "active claim")
        self.assertIsNotNone(signal)

        calls: list[str] = []

        def fetch_json(url: str, _token: str | None) -> Any:
            calls.append(url)
            return []

        reason, signal = paid_verification.candidate_rejection_reason(
            issue(body="bounty $100", comments=0),
            "tok",
            fetch_json=fetch_json,
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(signal)
        self.assertEqual(
            calls,
            ["https://api.github.com/repos/example/project/issues/42/timeline?per_page=100"],
        )


if __name__ == "__main__":
    unittest.main()
