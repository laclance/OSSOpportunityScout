from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from opportunity_scout import github, paid_verification
from opportunity_scout.types import GitHubIssue, SourceFailureReason
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
                        "repository_url": "https://api.github.com/repos/acme/widget",
                        "title": "Fix widget race",
                        "body": "Fixes #42",
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

    def test_issue_reference_scope_distinguishes_same_and_cross_repository_prs(self) -> None:
        same_repo_timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/acme/widget/pull/9",
                        "repository_url": "https://api.github.com/repos/ACME/WIDGET/",
                        "title": "Fix widget race",
                        "body": "Fixes #42",
                    }
                },
            }
        ]
        self.assertEqual(
            paid_verification.existing_implementation_pr_reason(
                same_repo_timeline,
                "acme/widget",
                42,
            ),
            "existing open implementation PR: https://github.com/acme/widget/pull/9",
        )

        cross_repo_timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/other/app/pull/9",
                        "repository_url": "https://api.github.com/repos/other/app",
                        "title": "Fix widget race",
                        "body": "Fixes #42",
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.existing_implementation_pr_reason(
                cross_repo_timeline,
                "acme/widget",
                42,
            )
        )

    def test_cross_repository_repo_qualified_reference_is_detected(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/other/app/pull/9",
                        "repository_url": "https://api.github.com/repos/other/app",
                        "title": "Fix widget race",
                        "body": "Resolves acme/widget#42",
                    }
                },
            }
        ]
        self.assertEqual(
            paid_verification.existing_implementation_pr_reason(
                timeline,
                "acme/widget",
                42,
            ),
            "existing open implementation PR: https://github.com/other/app/pull/9",
        )

    def test_relationship_keyword_cannot_cross_sentence_boundary(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/other/app/pull/9",
                        "repository_url": "https://api.github.com/repos/other/app",
                        "title": "Widget cache work",
                        "body": "Implement cache support. acme/widget#42",
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.existing_implementation_pr_reason(
                timeline,
                "acme/widget",
                42,
            )
        )

    def test_relationship_keyword_outside_bounded_context_is_ignored(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/other/app/pull/9",
                        "repository_url": "https://api.github.com/repos/other/app",
                        "title": "Widget cache work",
                        "body": "Fix" + ("x" * 121) + " acme/widget#42",
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.existing_implementation_pr_reason(
                timeline,
                "acme/widget",
                42,
            )
        )

    def test_non_candidate_prs_ignore_malformed_relationship_metadata(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "closed",
                        "html_url": "https://github.com/acme/widget/pull/7",
                        "title": {"bad": "shape"},
                        "body": {"bad": "shape"},
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "repository_url": {"bad": "shape"},
                        "title": {"bad": "shape"},
                        "body": {"bad": "shape"},
                    }
                },
            },
        ]
        self.assertIsNone(
            paid_verification.existing_implementation_pr_reason(
                timeline,
                "acme/widget",
                42,
            )
        )

    def test_unrelated_external_cross_reference_is_not_implementation(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/other/app/pull/7",
                        "repository_url": "https://api.github.com/repos/other/app",
                        "title": "Document widget dependency",
                        "body": "Related to https://github.com/acme/widget/issues/42 for context.",
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.has_existing_implementation_pr(
                "acme/widget",
                42,
                "tok",
                fetch_json=lambda *_: timeline,
            )
        )

        self.assertIsNone(
            paid_verification.has_existing_implementation_pr(
                "acme/widget",
                42,
                "tok",
                fetch_json=lambda *_: [
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "pull_request": {},
                                "state": "open",
                                "html_url": "https://github.com/other/app/pull/8",
                                "repository_url": "https://api.github.com/repos/other/app",
                                "title": "Document widget dependency",
                                "body": None,
                            }
                        },
                    }
                ],
            )
        )

    def test_cloudflared_dependency_update_is_not_implementation(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/anthony-spruyt/spruyt-labs/pull/3267",
                        "repository_url": (
                            "https://api.github.com/repos/anthony-spruyt/spruyt-labs"
                        ),
                        "title": (
                            "chore(deps): update container image "
                            "docker.io/cloudflare/cloudflared to v2026.9.3"
                        ),
                        "body": (
                            "Release notes mention "
                            "https://github.com/cloudflare/cloudflared/issues/1751."
                        ),
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.has_existing_implementation_pr(
                "cloudflare/cloudflared",
                1751,
                "tok",
                fetch_json=lambda *_: timeline,
            )
        )

    def test_unrelated_undici_application_pr_is_not_implementation(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/Tungdota53/QLTT-RAG/pull/1",
                        "repository_url": "https://api.github.com/repos/Tungdota53/QLTT-RAG",
                        "title": "Feat/core platform",
                        "body": (
                            "Tracking upstream behavior: "
                            "https://github.com/nodejs/undici/issues/3492."
                        ),
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.has_existing_implementation_pr(
                "nodejs/undici",
                3492,
                "tok",
                fetch_json=lambda *_: timeline,
            )
        )

    def test_cross_repository_explicit_implementation_reference_is_detected(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/prometheus/common/pull/1008",
                        "repository_url": "https://api.github.com/repos/prometheus/common",
                        "title": "expfmt: default to allow-utf-8 escaping for OpenMetrics 2.0",
                        "body": (
                            "Part of https://github.com/prometheus/client_golang/issues/2149."
                        ),
                    }
                },
            }
        ]
        self.assertEqual(
            paid_verification.has_existing_implementation_pr(
                "prometheus/client_golang",
                2149,
                "tok",
                fetch_json=lambda *_: timeline,
            ),
            "existing open implementation PR: https://github.com/prometheus/common/pull/1008",
        )

    def test_downstream_pr_reference_to_upstream_proposal_is_not_implementation(
        self,
    ) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": ("https://github.com/netobserv/flowlogs-pipeline/pull/1247"),
                        "repository_url": (
                            "https://api.github.com/repos/netobserv/flowlogs-pipeline"
                        ),
                        "title": (
                            "[DRAFT] NETOBSERV-2284 FLP metrics cache optimization (TTL registry)"
                        ),
                        "body": (
                            "Implement TTL support for metrics. See upstream proposal: "
                            "https://github.com/prometheus/client_golang/issues/1983\n\n"
                            "Alternative of "
                            "https://github.com/netobserv/flowlogs-pipeline/pull/1243"
                        ),
                    }
                },
            }
        ]
        self.assertIsNone(
            paid_verification.has_existing_implementation_pr(
                "prometheus/client_golang",
                1983,
                "tok",
                fetch_json=lambda *_: timeline,
            )
        )

    def test_incomplete_open_pr_source_fails_closed(self) -> None:
        malformed_sources = (
            {
                "pull_request": {},
                "state": "open",
                "html_url": "https://github.com/acme/widget/pull/9",
                "repository_url": "https://api.github.com/repos/acme/widget",
                "body": "Fixes #42",
            },
            {
                "pull_request": {},
                "state": "open",
                "html_url": "https://github.com/acme/widget/pull/9",
                "repository_url": "https://api.github.com/repos/acme/widget",
                "title": "Fix widget race",
                "body": {"unexpected": "shape"},
            },
            {
                "pull_request": {},
                "state": "open",
                "html_url": "https://github.com/acme/widget/pull/9",
                "title": "Fix widget race",
                "body": "Fixes #42",
            },
        )
        for source_issue in malformed_sources:
            with self.subTest(source_issue=source_issue):
                reason = paid_verification.has_existing_implementation_pr(
                    "acme/widget",
                    42,
                    "tok",
                    fetch_json=lambda *_args, source_issue=source_issue: [
                        {
                            "event": "cross-referenced",
                            "source": {"issue": source_issue},
                        }
                    ],
                )
                self.assertEqual(reason, "could not verify open implementation PR timeline")
                self.assertIsInstance(reason, SourceFailureReason)

    def test_failed_or_non_list_timeline_fails_closed(self) -> None:
        values: tuple[object, ...] = (None, {})
        for value in values:
            with self.subTest(value=value):
                reason = paid_verification.has_existing_implementation_pr(
                    "acme/widget",
                    42,
                    "tok",
                    fetch_json=lambda *_args, value=value: value,
                )
                self.assertEqual(reason, "could not verify open implementation PR timeline")
                self.assertIsInstance(reason, SourceFailureReason)

    def test_default_transport_is_used_once(self) -> None:
        with patch.object(github, "github_collection", return_value=[]) as getter:
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

    def test_comment_request_is_capped_at_exactly_thirty(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def fetch_json(url: str, token: str | None) -> Any:
            calls.append((url, token))
            return []

        self.assertIsNone(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                31,
                "tok",
                fetch_json=fetch_json,
            )
        )
        self.assertEqual(
            calls,
            [
                (
                    "https://api.github.com/repos/acme/widget/issues/42/comments"
                    "?per_page=30&sort=created&direction=desc",
                    "tok",
                )
            ],
        )

    def test_failed_or_non_list_comments_fails_closed(self) -> None:
        values: tuple[object, ...] = (None, {})
        for value in values:
            with self.subTest(value=value):
                reason = paid_verification.active_claim_reason(
                    "acme/widget",
                    42,
                    2,
                    "tok",
                    fetch_json=lambda *_, value=value: value,
                )
                self.assertEqual(reason, "could not verify active claim comments")
                self.assertIsInstance(reason, SourceFailureReason)

    def test_valid_empty_comments_has_no_claim(self) -> None:
        self.assertIsNone(
            paid_verification.active_claim_reason(
                "acme/widget",
                42,
                2,
                "tok",
                fetch_json=lambda *_: [],
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

    def test_source_failure_reason_is_preserved_and_short_circuits_claim_check(self) -> None:
        failure = SourceFailureReason("could not verify open implementation PR timeline")
        claim_calls: list[int] = []

        def claim_checker(
            _repo: str,
            _number: int,
            _comments: int,
            _token: str | None,
        ) -> str | None:
            claim_calls.append(1)
            return None

        reason, signal = paid_verification.candidate_rejection_reason(
            issue(body="bounty $100", comments=1),
            "tok",
            existing_pr_checker=lambda *_: failure,
            active_claim_checker=claim_checker,
        )
        self.assertIs(reason, failure)
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
