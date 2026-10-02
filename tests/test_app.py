from __future__ import annotations

import io
import os
import runpy
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import patch

import bountyscout.app as scout
from bountyscout import delivery, github
from bountyscout import paid as paid_policy
from bountyscout import paid_verification
from bountyscout import state
from bountyscout.strategic import competition as competition_policy
from bountyscout.types import (
    GitHubComment,
    GitHubIssue,
    RejectionRecord,
    RepositoryMetadata,
    SearchBatch,
)
from tests.helpers import FakeResponse


def issue(**overrides: Any) -> GitHubIssue:
    base: dict[str, Any] = {
        "html_url": "https://github.com/example/project/issues/42",
        "state": "open",
        "comments": 1,
        "title": "Fix deterministic network regression",
        "body": "",
        "labels": [],
        "assignees": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return cast(GitHubIssue, base)


def repo_meta(**overrides: Any) -> RepositoryMetadata:
    base: dict[str, Any] = {
        "stargazers_count": 1500,
        "pushed_at": datetime.now(timezone.utc).isoformat(),
        "language": "Go",
        "archived": False,
    }
    base.update(overrides)
    return cast(RepositoryMetadata, base)


def candidate(**overrides: Any) -> dict[str, Any]:
    base = {
        "repo": "example/project",
        "issue_number": 42,
        "title": "Fix deterministic network regression",
        "url": "https://github.com/example/project/issues/42",
        "paid": True,
        "reward": "$100",
        "payment_confidence": 100,
        "cash_score": 80,
        "career_score": 75,
        "priority_score": 85,
        "effort": "1–3h",
        "expected_hourly": 50.0,
        "competition": "low",
        "stars": 1500,
        "recent_activity": "active in last 7d",
        "language": "Go",
        "labels": ["help wanted"],
        "cash_reasons": ["payment confidence 100/100"],
        "career_reasons": ["target infrastructure/domain fit"],
        "contribution_guide": "https://github.com/example/project/CONTRIBUTING.md",
        "comments": 1,
        "updated_at": "2026-09-30T12:00:00Z",
        "rejection_reason": None,
    }
    base.update(overrides)
    return base


class BasicHeuristicTests(unittest.TestCase):
    def test_target_repo_pool_uses_core_api_and_search_budget_is_small(self) -> None:
        with patch.object(
            github,
            "github_get",
            return_value=[issue(), "not-an-issue"],
        ) as getter:
            items, error = scout.target_repo_issue_pool("grpc/grpc-go", "t")
        self.assertIsNone(error)
        self.assertEqual(len(items), 1)
        url = getter.call_args.args[0]
        self.assertIn("/repos/grpc/grpc-go/issues?", url)
        self.assertIn("state=open", url)
        self.assertIn("sort=updated", url)
        self.assertIn("per_page=50", url)
        self.assertIn("page=1", url)

        with patch.object(github, "github_get", return_value=None):
            items, error = scout.target_repo_issue_pool("grpc/grpc-go", "t")
        self.assertEqual(items, [])
        self.assertIn("scan coverage incomplete", str(error))

        pr_item = issue(
            html_url="https://github.com/grpc/grpc-go/pull/1",
            pull_request={"url": "x"},
        )
        i1 = issue(html_url="https://github.com/grpc/grpc-go/issues/1")
        i2 = issue(html_url="https://github.com/grpc/grpc-go/issues/2")
        i3 = issue(html_url="https://github.com/grpc/grpc-go/issues/3")
        with (
            patch.object(scout, "TARGET_REPO_FETCH_PER_PAGE", 4),
            patch.object(scout, "STRATEGIC_SEARCH_PER_PAGE", 3),
            patch.object(scout, "TARGET_REPO_FETCH_PAGES", 3),
            patch.object(
                github,
                "github_get",
                side_effect=[
                    [i1, pr_item, "not-an-issue", pr_item],
                    [i2, i3, pr_item],
                ],
            ) as getter,
        ):
            items, error = scout.target_repo_issue_pool("grpc/grpc-go", "t")
        self.assertIsNone(error)
        self.assertEqual(
            [item["html_url"] for item in items],
            [
                i1["html_url"],
                i2["html_url"],
                i3["html_url"],
            ],
        )
        self.assertEqual(getter.call_count, 2)
        self.assertIn("page=2", getter.call_args.args[0])

        with (
            patch.object(scout, "TARGET_REPO_FETCH_PER_PAGE", 2),
            patch.object(scout, "STRATEGIC_SEARCH_PER_PAGE", 3),
            patch.object(
                github,
                "github_get",
                side_effect=[[i1, pr_item], None],
            ),
        ):
            items, error = scout.target_repo_issue_pool("grpc/grpc-go", "t")
        self.assertEqual([item["html_url"] for item in items], [i1["html_url"]])
        self.assertIn("scan coverage incomplete", str(error))

        with (
            patch.object(scout, "TARGET_REPO_FETCH_PER_PAGE", 2),
            patch.object(scout, "STRATEGIC_SEARCH_PER_PAGE", 5),
            patch.object(scout, "TARGET_REPO_FETCH_PAGES", 2),
            patch.object(
                github,
                "github_get",
                side_effect=[[pr_item, pr_item], [pr_item, pr_item]],
            ),
        ):
            items, error = scout.target_repo_issue_pool("grpc/grpc-go", "t")
        self.assertEqual(items, [])
        self.assertIsNone(error)

        for repo in (
            "grpc/grpc-go",
            "etcd-io/etcd",
            "containerd/containerd",
            "cloudflare/cloudflared",
            "open-telemetry/opentelemetry-js",
            "nodejs/undici",
        ):
            self.assertIn(repo, scout.TARGET_REPOS)
        self.assertTrue(
            any(
                'label:"help wanted"' in query and 'label:"bug" OR regression' in query
                for query in scout.STRATEGIC_GLOBAL_QUERIES
            )
        )
        self.assertTrue(
            any(
                'label:"good first issue" label:"bug"' in query
                for query in scout.STRATEGIC_GLOBAL_QUERIES
            )
        )
        self.assertLessEqual(
            len(scout.PAID_DISCOVERY_QUERIES) + len(scout.STRATEGIC_GLOBAL_QUERIES),
            10,
        )

    def test_strategic_basic_candidate_allows_comment_volume_only(self) -> None:
        clean = issue(comments=1)
        crowded = issue(comments=paid_policy.MAX_COMMENTS + 5)
        crowded_assigned = issue(
            comments=paid_policy.MAX_COMMENTS + 5,
            assignees=[{"login": "dev"}],
        )
        assigned = issue(assignees=[{"login": "dev"}])

        self.assertTrue(scout.strategic_basic_candidate(clean))
        self.assertTrue(scout.strategic_basic_candidate(crowded))
        self.assertFalse(scout.strategic_basic_candidate(crowded_assigned))
        self.assertFalse(scout.strategic_basic_candidate(assigned))

    def test_fetch_repo_metadata_compatibility_wrapper(self) -> None:
        with patch.object(github, "repo_metadata", return_value={"stargazers_count": 7}) as fetch:
            self.assertEqual(
                scout.fetch_repo_metadata("example/project", "t"),
                {"stargazers_count": 7},
            )
        fetch.assert_called_once_with("example/project", "t")


class HttpAndPlatformTests(unittest.TestCase):
    def test_github_get_optional_success_failure_and_auth(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"html_url":"x"}'),
        ) as opened:
            self.assertEqual(scout.github_get_optional("https://x", "tok"), {"html_url": "x"})
            self.assertEqual(opened.call_args.args[0].headers["Authorization"], "Bearer tok")
        with patch.object(urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertIsNone(scout.github_get_optional("https://x", None))

    def test_fetch_text_success_and_failure(self) -> None:
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(b"hello")):
            self.assertEqual(scout.fetch_text("https://x"), "hello")
        with patch.object(urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertEqual(scout.fetch_text("https://x"), "")

    def test_issue_comments_paths(self) -> None:
        self.assertEqual(scout.issue_comments({"html_url": "bad", "comments": 2}, "t"), [])
        self.assertEqual(scout.issue_comments(issue(comments=0), "t"), [])
        with patch.object(github, "github_get", return_value={"not": "list"}):
            self.assertEqual(scout.issue_comments(issue(comments=1), "t"), [])
        with patch.object(github, "github_get", return_value=[{"body": "x"}]):
            self.assertEqual(scout.issue_comments(issue(comments=1), "t"), [{"body": "x"}])

    def test_supplemental_payment_signals(self) -> None:
        variants = [
            ("Cash prize: $50 for merge", "explicit paid-work wording: $50"),
            ("Pay $60 as a stipend", "explicit paid-work wording: $60"),
            ("Sponsored work €70", "explicit paid-work wording: €70"),
            ("80 CAD funded task", "explicit paid-work wording: 80 CAD"),
        ]
        for body, expected in variants:
            self.assertEqual(scout.supplemental_payment_signal(issue(body=body)), expected)

        self.assertEqual(
            scout.supplemental_payment_signal(issue(body="See https://bountyhub.dev/x — $90")),
            "named bounty platform + amount (BountyHub): $90",
        )
        self.assertIsNone(
            scout.supplemental_payment_signal(
                issue(
                    body=("See https://bountyhub.dev/x — Ethereum-mainnet ERC-20 USDC/USDT address")
                )
            )
        )
        self.assertIsNone(scout.supplemental_payment_signal(issue(body="maybe paid someday")))

    def test_comment_payment_confirmations_and_commands(self) -> None:
        comment_cases = [
            (
                {
                    "body": "$25 bounty created - algora.io/x",
                    "user": {"login": "bot"},
                    "author_association": "NONE",
                },
                "confirmed bounty platform comment (Algora): $25",
            ),
            (
                {
                    "body": "Opire reward 30 USDC",
                    "user": {"login": "opire-bot"},
                    "author_association": "NONE",
                },
                "confirmed bounty platform comment (Opire): 30 USDC",
            ),
            (
                {
                    "body": "A bounty of $40 has been created - bountyhub.dev/x",
                    "user": {"login": "bot"},
                    "author_association": "NONE",
                },
                "confirmed bounty platform comment (BountyHub): $40",
            ),
        ]
        for comment, expected in comment_cases:
            with patch.object(scout, "issue_comments", return_value=[comment]):
                self.assertEqual(scout.comment_payment_signal(issue(), "t"), expected)

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/reward 75", "author_association": "OWNER", "user": {"login": "m"}}
            ],
        ):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"), "explicit /reward comment: $75"
            )

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/bounty $88", "author_association": "MEMBER", "user": {"login": "m"}}
            ],
        ):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"), "explicit /bounty comment: $88"
            )

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/bounty 50 USDT", "author_association": "MEMBER", "user": {"login": "m"}}
            ],
        ):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"), "explicit /bounty comment: 50 USDT"
            )

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/reward 999", "author_association": "NONE", "user": {"login": "x"}}
            ],
        ):
            self.assertIsNone(scout.comment_payment_signal(issue(), "t"))

    def test_issue_from_github_url(self) -> None:
        self.assertIsNone(scout.issue_from_github_url("bad", "t"))
        with patch.object(github, "github_get", return_value=[]):
            self.assertIsNone(scout.issue_from_github_url("https://github.com/a/b/issues/1", "t"))
        with patch.object(github, "github_get", return_value={"state": "open"}) as get:
            self.assertEqual(
                scout.issue_from_github_url("https://github.com/a/b/issues/1", "t"),
                {"state": "open"},
            )
            self.assertIn("/repos/a/b/issues/1", get.call_args.args[0])

    def test_issuehunt_parser_empty_pagination_amount_and_no_amount(self) -> None:
        pages = {
            "https://oss.issuehunt.io/issues": (
                '<a href="/r/apache/superset/issues/3821">x</a><span>$17.00</span>'
            ),
            "https://oss.issuehunt.io/issues?page=2": (
                '<a href="/r/acme/widget/issues/9">x</a><span>funded</span>'
            ),
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.issuehunt_platform_refs()
        self.assertEqual(
            refs["https://github.com/apache/superset/issues/3821"],
            "confirmed bounty platform feed (IssueHunt): $17.00",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/9"],
            "confirmed bounty platform feed (IssueHunt)",
        )

        with patch.object(scout, "fetch_text", return_value=""):
            self.assertEqual(scout.issuehunt_platform_refs(), {})

    def test_opire_parser_direct_details_missing_and_no_amount(self) -> None:
        pages = {
            "https://app.opire.dev/home": (
                "https:\\/\\/github.com\\/direct\\/repo\\/issues\\/1 "
                '<a href="/issues/A">a</a><a href="/issues/B">b</a>'
            ),
            "https://app.opire.dev/issues/A": "no github source here",
            "https://app.opire.dev/issues/B": "https://github.com/acme/widget/issues/2 funded",
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.opire_platform_refs()
        self.assertIn("https://github.com/direct/repo/issues/1", refs)
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (Opire)",
        )
        with patch.object(scout, "fetch_text", return_value=""):
            self.assertEqual(scout.opire_platform_refs(), {})

    def test_bountyhub_parser_direct_details_missing_and_amount(self) -> None:
        pages = {
            "https://www.bountyhub.dev/en/bounties": (
                "https:\\/\\/github.com\\/direct\\/repo\\/issues\\/1 "
                '<a href="/en/bounty/view/A">a</a><a href="/en/bounty/view/B">b</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": "no github",
            "https://www.bountyhub.dev/en/bounty/view/B": "Reward $125 https://github.com/acme/widget/issues/2",
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.bountyhub_platform_refs()
        self.assertIn("https://github.com/direct/repo/issues/1", refs)
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (BountyHub): $125",
        )
        with patch.object(scout, "fetch_text", return_value=""):
            self.assertEqual(scout.bountyhub_platform_refs(), {})

    def test_platform_paid_refs_merge_precedence(self) -> None:
        with (
            patch.object(scout, "issuehunt_platform_refs", return_value={"u": "issuehunt"}),
            patch.object(scout, "opire_platform_refs", return_value={"v": "opire"}),
            patch.object(scout, "bountyhub_platform_refs", return_value={"u": "bountyhub"}),
        ):
            self.assertEqual(scout.platform_paid_refs(), {"u": "bountyhub", "v": "opire"})

    def test_contribution_guide_found_and_missing(self) -> None:
        def getter(url: str, token: str | None) -> Any:
            return {"html_url": "guide"} if "docs/CONTRIBUTING.md" in url else None

        with patch.object(scout, "github_get_optional", side_effect=getter):
            self.assertEqual(scout.contribution_guide("a/b", "t"), "guide")
        with patch.object(scout, "github_get_optional", return_value=None):
            self.assertIsNone(scout.contribution_guide("a/b", "t"))


class CalibrationTests(unittest.TestCase):
    def test_strategic_competition_reason_paths(self) -> None:
        self.assertEqual(
            scout.strategic_competition_reason({"html_url": "bad"}, "t", []),
            "could not identify repository/issue number",
        )

        with patch.object(
            scout,
            "timeline_open_pr_reason",
            return_value="timeline pr",
        ):
            self.assertEqual(
                scout.strategic_competition_reason(issue(), "t", []),
                "timeline pr",
            )

        with (
            patch.object(scout, "timeline_open_pr_reason", return_value=None),
            patch.object(scout, "linked_open_pr_reason", return_value="linked pr"),
        ):
            self.assertEqual(
                scout.strategic_competition_reason(issue(), "t", []),
                "linked pr",
            )

        with (
            patch.object(scout, "timeline_open_pr_reason", return_value=None),
            patch.object(scout, "linked_open_pr_reason", return_value=None),
        ):
            claimed = issue(
                comments=0,
                body="I have a fix with tests and can open a PR.",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.assertEqual(
                scout.strategic_competition_reason(claimed, "t", []),
                "issue author already has an implementation/fix in progress",
            )
            self.assertIsNone(
                scout.strategic_competition_reason(issue(body="", comments=0), "t", [])
            )

    def test_wrapper_and_non_actionable_diagnostic_detection(self) -> None:
        wrapper = issue(
            title="[READY FOR ENGINEERING] upstream task",
            body=(
                "## TARGET_REPOSITORY\nhttps://github.com/acme/upstream\n\n"
                "## ORIGINAL_ISSUE_URL\nhttps://github.com/acme/upstream/issues/123\n"
            ),
        )
        self.assertEqual(
            scout.upstream_wrapper_issue_url(wrapper),
            "https://github.com/acme/upstream/issues/123",
        )
        self.assertIsNone(
            scout.upstream_wrapper_issue_url(
                issue(body="See https://github.com/acme/upstream/issues/123")
            )
        )
        self.assertIsNone(
            scout.upstream_wrapper_issue_url(
                issue(body="ORIGINAL_ISSUE_URL https://github.com/acme/upstream/issues/123")
            )
        )
        self.assertIsNone(
            scout.upstream_wrapper_issue_url(
                issue(body="TARGET_REPOSITORY x\nORIGINAL_ISSUE_URL not-a-url")
            )
        )
        self.assertEqual(
            scout.upstream_wrapper_issue_url(
                issue(
                    title="[READY FOR ENGINEERING] task",
                    body="ORIGINAL_ISSUE_URL https://github.com/acme/upstream/issues/124",
                )
            ),
            "https://github.com/acme/upstream/issues/124",
        )

        diagnostic = issue(
            title="macOS M5 hard hang / black screen under tunnel throughput",
            body=(
                "macOS 27 on an M5 MacBook. The system needs a force reset. "
                "There is no Tailscale.app userspace crash; collect sysdiagnose after repro."
            ),
            labels=[],
        )
        self.assertEqual(
            scout.non_actionable_diagnostic_reason(diagnostic),
            "hardware/kernel diagnostic report without actionable contributor scope",
        )
        self.assertIsNone(
            scout.non_actionable_diagnostic_reason(
                {**diagnostic, "labels": [{"name": "help wanted"}]}
            )
        )
        self.assertIsNone(
            scout.non_actionable_diagnostic_reason(
                issue(body="Investigate pkg/network.go on macOS M5 hard hang")
            )
        )
        self.assertIsNone(scout.non_actionable_diagnostic_reason(issue(body="normal bug")))
        self.assertIsNone(
            scout.non_actionable_diagnostic_reason(
                issue(body="macOS hard hang with sysdiagnose but no hardware model")
            )
        )
        self.assertIsNone(
            scout.non_actionable_diagnostic_reason(
                issue(body="macOS M5 hard hang with a deterministic userspace repro")
            )
        )

    def test_extended_competition_reason_all_sources(self) -> None:
        self.assertEqual(
            scout.extended_competition_reason(
                {"html_url": "bad", "comments": 0},
                "t",
            ),
            "could not identify repository/issue number",
        )

        with patch.object(
            paid_verification,
            "has_existing_implementation_pr",
            return_value="search pr",
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "search pr",
            )

        with (
            patch.object(
                paid_verification,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=[{"body": "related PR #8"}],
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value="linked pr",
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "linked pr",
            )

        claim_comments: list[GitHubComment] = [
            {
                "body": "I'm working on this",
                "user": {"login": "dev"},
            }
        ]
        with (
            patch.object(
                paid_verification,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=claim_comments,
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "active claim by @dev",
            )

        with (
            patch.object(
                paid_verification,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=[{"body": "Planning a fix", "user": {"login": "dev2"}}],
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "active claim by @dev2",
            )

        with (
            patch.object(
                paid_verification,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=[],
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
            patch.object(
                scout,
                "timeline_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertIsNone(scout.extended_competition_reason(issue(), "t"))

    def test_real_queue_calibration_examples(self) -> None:
        aws = issue(
            title="ipamd can allocate an EC2-unassigned IP after restart when IMDS is stale",
            body="deterministic regression",
            labels=[{"name": "bug"}, {"name": "good first issue"}],
            comments=1,
        )
        with (
            patch.object(
                scout,
                "issue_comments",
                return_value=[
                    {
                        "body": "Planning a fix:",
                        "user": {"login": "laclance"},
                    }
                ],
            ),
            patch.object(
                paid_verification,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(aws, "t"),
                "active claim by @laclance",
            )

        connection_pool = issue(
            title="Client-go: Add support for Connection Pool",
            body="I would like to propose adding support.",
            labels=[{"name": "kind/feature"}, {"name": "needs-triage"}],
        )
        self.assertEqual(
            scout.strategic_rejection(connection_pool, "t"),
            "awaiting maintainer triage",
        )

    def test_verify_resolves_aggregator_wrapper_to_upstream(self) -> None:
        wrapper = issue(
            html_url="https://github.com/aggregator/jobs/issues/4",
            title="[READY FOR ENGINEERING] upstream task",
            body=(
                "## TARGET_REPOSITORY\nhttps://github.com/example/project\n\n"
                "## ORIGINAL_ISSUE_URL\nhttps://github.com/example/project/issues/42\n"
            ),
            comments=0,
        )
        upstream = issue(comments=1)
        with (
            patch.object(
                scout,
                "refresh_issue",
                side_effect=[(wrapper, None), (upstream, None)],
            ),
            patch.object(scout, "issue_from_github_url", return_value=upstream),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                scout,
                "comment_payment_signal",
                return_value="confirmed bounty platform comment (Algora): $50",
            ),
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=("no explicit payment signal", None),
            ),
            patch.object(scout, "extended_competition_reason", return_value=None),
            patch.object(scout, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "contribution_guide", return_value=None),
            patch.object(
                scout,
                "build_candidate",
                return_value={"url": upstream["html_url"]},
            ),
        ):
            result, reason = scout.verify(wrapper, "t", {}, {}, require_paid=True)
        self.assertIsNone(reason)
        self.assertEqual(result, {"url": upstream["html_url"]})


class CandidateTests(unittest.TestCase):
    def test_build_paid_candidate_scoring(self) -> None:
        item_ = issue(
            body="network concurrency regression tests",
            labels=[{"name": "help wanted"}],
            comments=0,
        )
        result = scout.build_candidate(
            item_,
            "paid",
            "confirmed bounty platform feed (Opire): $500",
            repo_meta(language="Go", stargazers_count=12000),
            "guide",
        )
        self.assertTrue(result["paid"])
        self.assertEqual(result["reward"], "$500")
        self.assertGreater(result["cash_score"], 0)
        self.assertGreater(result["career_score"], 0)
        self.assertEqual(result["competition"], "none")
        self.assertEqual(result["contribution_guide"], "guide")


class VerificationTests(unittest.TestCase):
    def test_refresh_issue_all_paths(self) -> None:
        self.assertEqual(
            scout.refresh_issue({"html_url": "bad"}, "t")[1],
            "could not identify repository/issue number",
        )
        for value, expected in [
            (None, "could not refresh source issue"),
            ({"state": "closed"}, "issue is no longer open"),
            ({"state": "open", "pull_request": {}}, "source is a pull request, not an issue"),
        ]:
            with patch.object(github, "github_get", return_value=value):
                self.assertEqual(scout.refresh_issue(issue(), "t")[1], expected)
        with patch.object(github, "github_get", return_value=issue()):
            fresh, reason = scout.refresh_issue(issue(), "t")
            self.assertIsNone(reason)
            self.assertIsNotNone(fresh)
            assert fresh is not None
            self.assertEqual(fresh["state"], "open")

    def test_strategic_rejection_paths(self) -> None:
        with patch.object(paid_policy, "is_clean_candidate", return_value=False):
            self.assertEqual(
                scout.strategic_rejection(issue(), "t"), "failed basic eligibility filter"
            )

        self.assertEqual(
            scout.strategic_rejection(issue(body="OSS Opportunity Queue"), "t"),
            "generated opportunity-scout report",
        )
        self.assertEqual(
            scout.strategic_rejection(issue(labels=["needs-info"]), "t"),
            "support/triage issue rather than a contributor task",
        )
        self.assertEqual(
            scout.strategic_rejection(
                {"html_url": "bad", "title": "x", "body": "", "labels": []}, "t"
            ),
            "could not identify repository/issue number",
        )
        with patch.object(scout, "strategic_competition_reason", return_value="pr"):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "pr")
        with patch.object(scout, "strategic_competition_reason", return_value="claim"):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "claim")
        with patch.object(
            scout,
            "non_actionable_diagnostic_reason",
            return_value="diagnostic",
        ):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "diagnostic")

        self.assertEqual(
            scout.strategic_rejection(issue(labels=["needs-triage"]), "t"),
            "awaiting maintainer triage",
        )
        self.assertEqual(
            scout.strategic_rejection(issue(labels=["needs/triage"]), "t"),
            "awaiting maintainer triage",
        )
        self.assertEqual(
            scout.strategic_rejection(issue(labels=["kind/bug/possible"]), "t"),
            "awaiting maintainer triage",
        )
        with patch.object(scout, "strategic_competition_reason", return_value=None):
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(labels=[{"name": "needs-triage"}, {"name": "good first issue"}]),
                    "t",
                )
            )

    def test_readiness_gate_known_false_positive_classes(self) -> None:
        loki = issue(
            title="Interpretation of date range in Grafana UI vs query_range API is wrong",
            labels=[{"name": "type/bug"}],
        )
        loki_comments: list[GitHubComment] = [
            {
                "body": (
                    "This particular issue came up as needs discussion, so I'm going to "
                    "hand it off to the Engineering team to look at."
                ),
                "author_association": "CONTRIBUTOR",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(loki, "t", loki_comments),
            "maintainer says issue still needs discussion",
        )

        argo = issue(
            title="chore: upgrade golang to 1.26.3",
            body=(
                "PR #27737 (merged 2026-05-07) already bumps Go to 1.26.3 on master "
                "but a new release tag has not yet been published. "
                "Request: please tag a new ArgoCD release from the current master."
            ),
        )
        self.assertEqual(
            scout.strategic_rejection(argo, "t", []),
            "implementation already merged; only release/tagging remains",
        )

        traefik = issue(
            title="Per-ingress request metrics",
            labels=[{"name": "kind/proposal"}],
        )
        traefik_comments: list[GitHubComment] = [
            {
                "body": (
                    "We'd like to gauge community interest before committing. "
                    "We'll reevaluate based on the feedback. "
                    "This discussion is time-boxed to 6 months."
                ),
                "author_association": "OWNER",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(traefik, "t", traefik_comments),
            "proposal is still gathering feedback",
        )

        dashboard = issue(
            title="Dependency Dashboard",
            labels=[{"name": "dependencies"}],
            user={"login": "renovate-sh-app[bot]"},
            comments=0,
        )
        self.assertEqual(
            scout.strategic_rejection(dashboard, "t", []),
            "automated dependency dashboard, not an implementation task",
        )

        moby = issue(
            title="docker cp copy-out can write outside the destination",
            labels=[{"name": "status/needs-reproduction"}],
        )
        self.assertEqual(
            scout.strategic_rejection(moby, "t", []),
            "awaiting reproduction confirmation",
        )

        undici = issue(
            title="HTTP/1.1 304 with Content-Length closes the connection",
            labels=[{"name": "bug"}],
        )
        undici_comments: list[GitHubComment] = [
            {
                "body": (
                    "Looks like this was fixed on main by #5864. "
                    "It's just not released yet. Should be good with the next release."
                ),
                "author_association": "NONE",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(undici, "t", undici_comments),
            "implementation already merged; only release/tagging remains",
        )

        flux = issue(
            title="ResourceSet and kustomize.toolkit.fluxcd.io/ssa",
            labels=[],
        )
        flux_comments: list[GitHubComment] = [
            {
                "body": (
                    "A merge option would be the wrong solution for this. "
                    "The profile should be defined in Git instead."
                ),
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(flux, "t", flux_comments),
            "maintainer indicates the proposed implementation approach is not wanted",
        )

    def test_fresh_1128_run_false_positive_regressions(self) -> None:
        prometheus = issue(
            title="storage/remote: ensure metadata instrumentation make sense for PRW2",
            body=(
                "This issue is to decide what these metrics should mean "
                "(or whether they should exist) for PRW2 before we call PRW2 stable."
            ),
            author_association="MEMBER",
            labels=[{"name": "component/remote storage"}],
            comments=3,
        )
        self.assertEqual(
            scout.strategic_rejection(prometheus, "t", []),
            "maintainer-authored issue is still deciding implementation semantics",
        )

        external_dns = issue(
            html_url="https://github.com/kubernetes-sigs/external-dns/issues/6718",
            title="TXT records are write-once",
            body=(
                "PR https://github.com/kubernetes-sigs/external-dns/pull/6293 "
                "fixes exactly this and has been open for six months."
            ),
            labels=[],
            comments=0,
        )
        with (
            patch.object(paid_verification, "has_existing_implementation_pr", return_value=None),
            patch.object(
                github,
                "github_get",
                return_value={
                    "state": "open",
                    "html_url": "https://github.com/kubernetes-sigs/external-dns/pull/6293",
                },
            ),
        ):
            self.assertEqual(
                scout.strategic_rejection(external_dns, "t", []),
                "existing open implementation PR: "
                "https://github.com/kubernetes-sigs/external-dns/pull/6293",
            )

        cmux = issue(
            title="cmux NIGHTLY build is failing on main",
            body="This issue closes itself on the next successful publish.",
            labels=[{"name": "bug"}, {"name": "nightly-failure"}, {"name": "help wanted"}],
            user={"login": "github-actions[bot]"},
            comments=178,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(
            scout.strategic_rejection(cmux, "t", []),
            "automated CI/release incident, not an implementation task",
        )
        self.assertFalse(scout.possible_miss_signal(cmux))

    def test_fresh_1140_run_claim_and_tracker_regressions(self) -> None:
        grpc_umbrella = issue(
            title=(
                "xds/clients: API refinements and cleanup before externalizing "
                "generic xDS and LRS clients"
            ),
            body=(
                "Track and resolve the following API refinements and bug fixes:\n\n"
                "- [ ] #8314\n"
                "- [ ] #9456\n"
                "- [ ] #9457\n"
                "- [ ] #9458\n"
                "- [ ] #9459\n"
            ),
            comments=0,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(
            scout.strategic_rejection(grpc_umbrella, "t", []),
            "umbrella tracking issue, not a single implementation task",
        )
        self.assertFalse(scout.possible_miss_signal(grpc_umbrella))

        client_golang = issue(
            title="api: no way to get query stats",
            comments=1,
        )
        client_golang_comments: list[GitHubComment] = [
            {
                "body": (
                    "I'd like to pick this up. On current main, WithStats sends stats=all, "
                    "but queryResult does not retain data.stats. I'll wait for direction on "
                    "the public interface before publishing an implementation."
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "fzlzjerry"},
            }
        ]
        with patch.object(paid_verification, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                scout.strategic_rejection(client_golang, "t", client_golang_comments),
                "active claim by @fzlzjerry",
            )

        aws_lbc = issue(
            title=(
                "Helm chart: support additional IngressClass / "
                "IngressClassParams pairs for a single controller"
            ),
            body=(
                "Contribution Intention (Optional)\n\n"
                "- [x] Yes, I am willing to contribute a PR to implement this feature\n"
                "- [ ] No, I cannot work on a PR at this time"
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
            comments=0,
        )
        with patch.object(paid_verification, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                scout.strategic_rejection(aws_lbc, "t", []),
                "issue author already has an implementation/fix in progress",
            )

    def test_fresh_1153_run_release_regression(self) -> None:
        missing_release = issue(
            title="Version 3.13.4 missing release",
            body=(
                "The version update was merged in #19862. The release action failed, "
                "so the release step then never triggered. We have a v3.13.4 tag, "
                "but no release with downloads."
            ),
            comments=0,
        )
        self.assertEqual(
            scout.strategic_rejection(missing_release, "t", []),
            "implementation already merged; only release/tagging remains",
        )

    def test_fresh_1204_run_false_positive_regressions(self) -> None:
        terraform = issue(
            title="Prevent metadata functions silently ignoring positional arguments",
            author_association="MEMBER",
            body=(
                "If you are an agent reading this, do not open a PR for this issue; "
                "it will be closed due to this issue representing a breaking change."
            ),
            comments=0,
        )
        self.assertEqual(
            scout.strategic_rejection(terraform, "t", []),
            "maintainer explicitly says not to open a PR for this issue",
        )

        cloudflared = issue(
            title=(
                "Warning for no ingress rule when using remote managed tunnel with credentials-file"
            ),
            comments=2,
        )
        cloudflared_comments: list[GitHubComment] = [
            {
                "body": (
                    "I have that working with a test on a branch. But I don't think "
                    "it's the right fix, so I'm not opening a PR yet."
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "davidscottpope-gif"},
            }
        ]
        with patch.object(paid_verification, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                scout.strategic_rejection(cloudflared, "t", cloudflared_comments),
                "active claim by @davidscottpope-gif",
            )

        cmux = issue(
            title="Persistent scrollbars cover terminal content",
            user={"login": "mgol"},
            comments=2,
        )
        cmux_comments: list[GitHubComment] = [
            {
                "body": (
                    "It looks like the issue got fixed after I reported it as I don't "
                    "see it in the latest version."
                ),
                "user": {"login": "mgol"},
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(cmux, "t", cmux_comments),
            "issue reporter says the problem is already resolved",
        )

        client_golang = issue(
            title="Support constant histograms without a sum",
            comments=2,
        )
        client_golang_comments: list[GitHubComment] = [
            {
                "body": (
                    "Nobody bothered to implement this and OpenMetrics 2.0 will not "
                    "allow absent sum. I'd suggest to reject such histograms and not "
                    "emit them. This is being discussed on spec level here."
                ),
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(client_golang, "t", client_golang_comments),
            "maintainer says issue still needs discussion",
        )

    def test_fresh_1218_run_false_positive_regressions(self) -> None:
        security = issue(
            html_url="https://github.com/kubernetes-sigs/external-dns/issues/6780",
            title="[Security Disclosure] Annotation-driven DNS record injection in external-dns",
            body=(
                "Severity: HIGH. CWE: CWE-285. CVSS 3.1: 8.1. "
                "Disclosure timeline: vulnerability discovered today."
            ),
            comments=0,
        )
        self.assertEqual(
            scout.strategic_rejection(security, "t", []),
            "security disclosure, not a normal contributor task",
        )
        self.assertFalse(scout.possible_miss_signal(security))

        terraform = issue(
            title="Generic deepmerge() function",
            comments=1,
        )
        terraform_comments: list[GitHubComment] = [
            {
                "body": (
                    "The prevailing wisdom on the maintainer team is that there is no "
                    "correct answer for one perfect implementation of a deep merge. "
                    "This is a use case for provider functions."
                ),
                "author_association": "CONTRIBUTOR",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(terraform, "t", terraform_comments),
            "maintainer indicates the proposed implementation approach is not wanted",
        )

        release_announcement = issue(
            title="Planned SDK 3.0 Release (Important Dates and Information)",
            labels=[{"name": "announcement 📢"}],
            comments=1,
        )
        self.assertEqual(
            scout.strategic_rejection(release_announcement, "t", []),
            "release planning/tracking issue, not implementation work",
        )

    def test_fresh_1326_run_false_positive_regressions(self) -> None:
        otel_js = issue(
            html_url="https://github.com/open-telemetry/opentelemetry-js/issues/6957",
            title="support ConsoleMetricExporter options from declarative config",
            comments=2,
        )
        otel_comments: list[GitHubComment] = [
            {
                "body": (
                    "I poked at this locally. No breaking change needed. "
                    "I made the explicit selector win and moved the preference logic "
                    "into sdk-metrics."
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "neoLsH"},
            }
        ]

        cloudflared = issue(
            html_url="https://github.com/cloudflare/cloudflared/issues/1728",
            title="QUIC connections intermittently terminate in Azure Container Apps",
            body=(
                "We would like to determine whether this is expected behavior, an Azure "
                "networking interaction, a cloudflared issue, or configuration. "
                "We would particularly appreciate guidance on:\n"
                "1. Is this expected for established QUIC connections?\n"
                "2. Are there known UDP idle-timeout issues?\n"
                "3. Could Azure networking cause this?\n"
                "4. Would switching to HTTP/2 be recommended?\n"
            ),
            comments=0,
        )

        aws_lbc = issue(
            html_url=(
                "https://github.com/kubernetes-sigs/aws-load-balancer-controller/issues/4870"
            ),
            title="manage controller CRD upgrades",
            comments=1,
        )
        aws_comments: list[GitHubComment] = [
            {
                "body": (
                    "Thanks for filing this. I agree the current CRD lifecycle experience "
                    "isn't great. It is worth discussion. Like to hear from the community "
                    "which approach is preferable."
                ),
                "author_association": "COLLABORATOR",
            }
        ]

        controller_runtime = issue(
            html_url="https://github.com/kubernetes-sigs/controller-runtime/issues/3220",
            title="Feature: Warmup for controllers",
            body=(
                "### Tasks\n"
                "- [x] Design\n"
                "- [x] Initial implementation\n"
                "- [ ] Further improvements\n"
            ),
            comments=1,
        )
        controller_comments: list[GitHubComment] = [
            {
                "body": "Let me know if I should add additional tasks to this umbrella issue.",
                "author_association": "MEMBER",
            }
        ]

        undici = issue(
            html_url="https://github.com/nodejs/undici/issues/5912",
            title="Should Fetch retry reusable request bodies after HTTP/2 GOAWAY?",
            comments=1,
        )
        undici_comments: list[GitHubComment] = [
            {
                "body": "https://github.com/KhafraDev/undici/tree/fetch/issue-5912",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "KhafraDev"},
                "author_association": "MEMBER",
            }
        ]

        with patch.object(paid_verification, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                scout.strategic_rejection(otel_js, "t", otel_comments),
                "active claim by @neoLsH",
            )
            self.assertEqual(
                scout.strategic_rejection(undici, "t", undici_comments),
                "active implementation branch linked by @KhafraDev",
            )

        self.assertEqual(
            scout.strategic_rejection(cloudflared, "t", []),
            "support/triage issue rather than a contributor task",
        )
        self.assertEqual(
            scout.strategic_rejection(aws_lbc, "t", aws_comments),
            "maintainer says issue still needs discussion",
        )
        self.assertEqual(
            scout.strategic_rejection(controller_runtime, "t", controller_comments),
            "umbrella tracking issue, not a single implementation task",
        )

    def test_fresh_1525_run_false_positive_regressions(self) -> None:
        controller_runtime = issue(
            html_url="https://github.com/kubernetes-sigs/controller-runtime/issues/3054",
            title="Metrics improvements",
            body=(
                "This is an umbrella issue to (try) to keep an overview over current "
                "feature requests and limitations around metrics."
            ),
            author_association="MEMBER",
            labels=[{"name": "lifecycle/frozen"}],
            comments=2,
        )
        self.assertEqual(
            scout.strategic_rejection(controller_runtime, "t", []),
            "umbrella tracking issue, not a single implementation task",
        )

        etcd_release = issue(
            html_url="https://github.com/etcd-io/etcd/issues/22449",
            title="Plan to release v3.5.34",
            body="The patch release criteria has been met, so we should release v3.5.34.",
            author_association="MEMBER",
            labels=[{"name": "area/security"}, {"name": "type/feature"}],
            comments=2,
        )
        self.assertEqual(
            scout.strategic_rejection(etcd_release, "t", []),
            "release planning/tracking issue, not implementation work",
        )

        claimed = issue(
            html_url="https://github.com/lacs-project/sysknife/issues/474",
            title="The plan summary an operator approves is never sanitised",
            labels=[{"name": "bug"}, {"name": "help wanted"}, {"name": "claimed"}],
            comments=3,
        )
        self.assertEqual(
            scout.strategic_rejection(claimed, "t", []),
            "issue is marked claimed by the project",
        )

    def test_fresh_1414_run_false_positive_regressions(self) -> None:
        undici_tracker = issue(
            html_url="https://github.com/nodejs/undici/issues/5177",
            title="[2026] Tracking issue for flaky tests",
            body=(
                "This is 2026 equivalent of the previous flaky-test tracker where maintainers "
                "had reduced flaky tests. We can use this issue for tracking, and create "
                "sub-issues per test failure?"
            ),
            author_association="MEMBER",
            comments=4,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(
            scout.strategic_rejection(undici_tracker, "t", []),
            "umbrella tracking issue, not a single implementation task",
        )

    def test_fresh_1350_run_false_positive_regressions(self) -> None:
        soup = issue(
            html_url="https://github.com/MakazhanAlpamys/Soup/issues/1530",
            title="from-traces file input silently writes zero pairs",
            labels=[{"name": "bug"}, {"name": "help wanted"}, {"name": "good first issue"}],
            comments=1,
        )
        soup_comments: list[GitHubComment] = [
            {
                "body": (
                    "Taking this one — I'll trace why --logs <file> yields an empty "
                    "dataset and fix the parsing."
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "vaputa"},
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(soup, "t", soup_comments),
            "active claim by @vaputa",
        )

        republish = issue(
            html_url="https://github.com/nodejs/undici/issues/5919",
            title=(
                "undici-types 6.21.x: a trusted-publisher re-release would unblock current users"
            ),
            body=(
                "Suggestion: publish undici-types@6.21.1 with the same content as 6.21.0 "
                "through the current trusted-publisher workflow."
            ),
            comments=0,
        )
        self.assertEqual(
            scout.strategic_rejection(republish, "t", []),
            "existing package content; only release/publication remains",
        )

        aws_cni = issue(
            html_url="https://github.com/aws/amazon-vpc-cni-k8s/issues/3833",
            title="ipamd attaches ENIs but never registers them",
            comments=1,
        )
        aws_comments: list[GitHubComment] = [
            {
                "body": (
                    "Could you share the node logs if you have collected them from the "
                    "affected instance?"
                ),
                "author_association": "COLLABORATOR",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(aws_cni, "t", aws_comments),
            "maintainer is waiting for requested diagnostic evidence",
        )

        undici_core = issue(
            html_url="https://github.com/nodejs/undici/issues/2558",
            title="Enhancement of IPv6 Connectivity and Address Selection",
            comments=1,
        )
        undici_core_comments: list[GitHubComment] = [
            {
                "body": (
                    "Closing it here would help it being backported to previous node. "
                    "But I think we should do it in core."
                ),
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(undici_core, "t", undici_core_comments),
            "maintainer redirected implementation/discussion to another project",
        )

    def test_readiness_gate_targeted_live_refinements(self) -> None:
        profile_request: list[GitHubComment] = [
            {
                "body": "Would it be possible to provide a profile from the affected binary?",
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(issue(title="Memory leak"), "t", profile_request),
            "maintainer is waiting for requested diagnostic evidence",
        )

        redirect: list[GitHubComment] = [
            {
                "body": (
                    "This requires a change in the specification defined in "
                    "https://github.com/distribution/reference. "
                    "Probably best to open a ticket there for discussion."
                ),
                "author_association": "COLLABORATOR",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(issue(title="IPv6 reference parsing"), "t", redirect),
            "maintainer redirected implementation/discussion to another project",
        )

        duplicate: list[GitHubComment] = [
            {
                "body": (
                    "Looks like a (possible) duplicate of #123. "
                    "The discussion is being tracked there."
                ),
                "author_association": "OWNER",
            }
        ]
        self.assertEqual(
            scout.strategic_rejection(issue(title="Prune behavior"), "t", duplicate),
            "maintainer indicates this is probably tracked by another canonical issue",
        )

        self.assertEqual(
            scout.strategic_rejection(
                issue(title="Gateway cert", labels=[{"name": "lifecycle/rotten"}]),
                "t",
                [],
            ),
            "issue is in an abandoned/rotten lifecycle state",
        )

        supplied = profile_request + [
            {
                "body": "I've attached the requested heap profile here: https://example.test/heap.",
                "author_association": "NONE",
            }
        ]
        approved = supplied + [
            {
                "body": "Thanks, this looks valid. A PR in this repository is welcome.",
                "author_association": "MEMBER",
            }
        ]
        redirect_then_ready = redirect + [
            {
                "body": "The spec concern is resolved. A PR in this repository is welcome.",
                "author_association": "MEMBER",
            }
        ]
        revival: list[GitHubComment] = [
            {
                "body": "We are reviving this issue; this issue is active again.",
                "author_association": "MEMBER",
            }
        ]

        with patch.object(scout, "strategic_competition_reason", return_value=None):
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Memory leak"),
                    "t",
                    [
                        {
                            "body": "Could you provide a heap profile?",
                            "author_association": "CONTRIBUTOR",
                        }
                    ],
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="API question"),
                    "t",
                    [
                        {
                            "body": "Could you clarify whether this also affects v2?",
                            "author_association": "MEMBER",
                        }
                    ],
                )
            )
            self.assertIsNone(scout.strategic_rejection(issue(title="Memory leak"), "t", supplied))
            self.assertIsNone(scout.strategic_rejection(issue(title="Memory leak"), "t", approved))
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Parser bug"),
                    "t",
                    [
                        {
                            "body": "Related code is in distribution/reference for context.",
                            "author_association": "MEMBER",
                        }
                    ],
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Parser bug"),
                    "t",
                    redirect_then_ready,
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Prune behavior"),
                    "t",
                    [
                        {
                            "body": "Related to #123, but this report has different symptoms.",
                            "author_association": "MEMBER",
                        }
                    ],
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Prune behavior"),
                    "t",
                    [
                        {
                            "body": "Looks like a duplicate of #123.",
                            "author_association": "NONE",
                        }
                    ],
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Prune behavior"),
                    "t",
                    [
                        {
                            "body": "This is not a duplicate of #123.",
                            "author_association": "MEMBER",
                        }
                    ],
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Old issue", labels=[{"name": "stale"}]),
                    "t",
                    [],
                )
            )
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(title="Gateway cert", labels=[{"name": "lifecycle/rotten"}]),
                    "t",
                    revival,
                )
            )

    def test_readiness_gate_allows_explicit_ready_override_and_normal_features(self) -> None:
        pending = issue(labels=[{"name": "status/needs-reproduction"}])
        ready_comments: list[GitHubComment] = [
            {
                "body": "Reproduced and confirmed. This is ready for implementation.",
                "author_association": "MEMBER",
            }
        ]
        feature = issue(
            title="Add per-request metrics",
            labels=[{"name": "enhancement"}],
        )
        proposal_without_hold = issue(
            title="Per-request metrics",
            labels=[{"name": "kind/proposal"}],
        )

        with patch.object(scout, "strategic_competition_reason", return_value=None):
            self.assertIsNone(scout.strategic_rejection(pending, "t", ready_comments))
            self.assertIsNone(scout.strategic_rejection(feature, "t", []))
            self.assertIsNone(scout.strategic_rejection(proposal_without_hold, "t", []))

        self.assertIsNone(
            scout.readiness_pending_label_reason(
                issue(labels=[{"name": "needs-investigation"}, {"name": "help wanted"}]),
                ready_override=True,
            )
        )

    def test_verify_refresh_and_clean_failures(self) -> None:
        with patch.object(scout, "refresh_issue", return_value=(None, "closed")):
            self.assertEqual(scout.verify(issue(), "t", {}, {})[1], "closed")
        with patch.object(scout, "refresh_issue", return_value=(None, None)):
            self.assertEqual(
                scout.verify(issue(), "t", {}, {})[1],
                "could not refresh source issue",
            )
        with (
            patch.object(scout, "refresh_issue", return_value=(issue(), None)),
            patch.object(paid_policy, "is_clean_candidate", return_value=False),
        ):
            self.assertEqual(
                scout.verify(issue(), "t", {}, {})[1],
                "failed basic eligibility filter after source refresh",
            )

        bad_repo = issue(html_url="not-github", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(bad_repo, None)),
            patch.object(paid_policy, "is_clean_candidate", return_value=True),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value=None),
        ):
            self.assertEqual(
                scout.verify(bad_repo, "t", {}, {})[1],
                "could not identify repository/issue number",
            )

    def test_verify_aggregator_wrapper_failure_paths(self) -> None:
        wrapper = issue(
            html_url="https://github.com/aggregator/jobs/issues/4",
            title="[READY FOR ENGINEERING] upstream task",
            body=(
                "TARGET_REPOSITORY https://github.com/example/project\n"
                "ORIGINAL_ISSUE_URL https://github.com/example/project/issues/42"
            ),
        )
        upstream = issue()

        with (
            patch.object(scout, "refresh_issue", return_value=(wrapper, None)),
            patch.object(scout, "issue_from_github_url", return_value=None),
        ):
            self.assertEqual(
                scout.verify(wrapper, "t", {}, {})[1],
                "could not refresh upstream issue from aggregator wrapper",
            )

        with (
            patch.object(
                scout,
                "refresh_issue",
                side_effect=[(wrapper, None), (None, "issue is no longer open")],
            ),
            patch.object(scout, "issue_from_github_url", return_value=upstream),
        ):
            self.assertEqual(
                scout.verify(wrapper, "t", {}, {})[1],
                "upstream source: issue is no longer open",
            )

        with (
            patch.object(
                scout,
                "refresh_issue",
                side_effect=[(wrapper, None), (None, None)],
            ),
            patch.object(scout, "issue_from_github_url", return_value=upstream),
        ):
            self.assertEqual(
                scout.verify(wrapper, "t", {}, {})[1],
                "could not refresh upstream issue from aggregator wrapper",
            )

        with (
            patch.object(
                scout,
                "refresh_issue",
                side_effect=[(wrapper, None), (upstream, None)],
            ),
            patch.object(scout, "issue_from_github_url", return_value=upstream),
            patch.object(paid_policy, "is_clean_candidate", return_value=False),
        ):
            self.assertEqual(
                scout.verify(wrapper, "t", {}, {})[1],
                "failed basic eligibility filter after source refresh",
            )

    def test_verify_paid_issue_signal_success_and_repo_failures(self) -> None:
        fresh = issue(body="bounty $100", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value={}),
        ):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}, True)[1], "repository metadata unavailable"
            )

        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value=repo_meta(archived=True)),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True)[1], "repository is archived")

        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "contribution_guide", return_value="guide"),
            patch.object(scout, "build_candidate", return_value={"ok": True}),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True), ({"ok": True}, None))

    def test_verify_rejects_hall_of_fame_before_paid_scoring(self) -> None:
        fresh = issue(
            title="🏆 Hall of Fame — October 2026",
            body=(
                "## 🥇 Top Contributors\n## 📊 Monthly Stats\nTotal Bounty Distributed | **$4770**"
            ),
            labels=[{"name": "hall-of-fame"}],
            comments=0,
        )
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_verification, "candidate_rejection_reason") as upstream,
        ):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}, require_paid=True)[1],
                "bounty history/leaderboard, not an open paid task",
            )
        upstream.assert_not_called()

    def test_verify_paid_rejects_recent_issue_author_prepared_patch(self) -> None:
        fresh = issue(
            body=(
                "**Bounty proposal**\n\n"
                "I prepared a focused candidate implementation with regression tests. "
                "Would you approve **US$100 cash upon acceptance and merge**?"
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
            comments=0,
        )
        with patch.object(scout, "refresh_issue", return_value=(fresh, None)):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}, require_paid=True)[1],
                "issue author already has an implementation/fix in progress",
            )

    def test_verify_comment_or_override_runs_competition_checks(self) -> None:
        fresh = issue(body="", title="Task", comments=1)
        base = [
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=("no explicit payment signal", None),
            ),
        ]
        for p in base:
            p.start()
            self.addCleanup(p.stop)

        with (
            patch.object(
                scout, "comment_payment_signal", return_value="explicit /reward comment: $50"
            ),
            patch.object(scout, "extended_competition_reason", return_value="pr"),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True)[1], "pr")

        with (
            patch.object(
                scout, "comment_payment_signal", return_value="explicit /reward comment: $50"
            ),
            patch.object(scout, "extended_competition_reason", return_value="claim"),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True)[1], "claim")

        with patch.object(scout, "comment_payment_signal", return_value=None):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}, True)[1], "no explicit payment signal"
            )

    def test_verify_strategic_fails_closed_when_comments_cannot_refresh(self) -> None:
        fresh = issue(body="", title="Feature", comments=2)
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                github,
                "issue_comments_checked",
                return_value=([], "could not refresh issue comments"),
            ),
            patch.object(scout, "strategic_rejection") as rejection,
        ):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {})[1],
                "could not refresh issue comments",
            )
        rejection.assert_not_called()

    def test_verify_strategic_uses_supplied_comments_and_handles_missing_repo(self) -> None:
        fresh = issue(body="", title="Feature", comments=1)
        supplied: list[GitHubComment] = [{"body": "Maintainer context"}]
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(scout, "strategic_basic_candidate", return_value=True),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value=None) as rejection,
            patch.object(github, "issue_repo_and_number", return_value=(None, None)),
        ):
            self.assertEqual(
                scout.verify(
                    fresh,
                    "t",
                    {},
                    {},
                    activity_comments=supplied,
                )[1],
                "could not identify repository/issue number",
            )
        rejection.assert_called_once_with(fresh, "t", supplied)

    def test_verify_strategic_reuses_one_checked_comment_fetch(self) -> None:
        fresh = issue(body="", title="Parser task", comments=2)
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": "The previous implementation needs cancellation regression tests.",
            }
        ]
        meta = repo_meta()
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                github,
                "issue_comments_checked",
                return_value=(comments, None),
            ) as comments_fetch,
            patch.object(scout, "issue_comments") as unchecked_fetch,
            patch.object(scout, "strategic_rejection", return_value=None) as rejection,
            patch.object(scout, "fetch_repo_metadata", return_value=meta),
            patch.object(scout, "contribution_guide", return_value=None),
            patch.object(
                scout,
                "build_candidate",
                return_value={"lane": "strategic"},
            ) as build,
        ):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}),
                ({"lane": "strategic"}, None),
            )

        comments_fetch.assert_called_once_with(fresh, "t")
        unchecked_fetch.assert_not_called()
        rejection.assert_called_once_with(fresh, "t", comments)
        build.assert_called_once_with(
            fresh,
            "strategic",
            None,
            meta,
            None,
            comments,
        )

    def test_strategic_comment_payment_signal_reuses_checked_comments(self) -> None:
        fresh = issue(body="", title="Task", comments=1)
        comments: list[GitHubComment] = [
            {
                "body": "/reward 50",
                "author_association": "MEMBER",
                "user": {"login": "maintainer"},
            }
        ]
        meta = repo_meta()
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                github,
                "issue_comments_checked",
                return_value=(comments, None),
            ) as comments_fetch,
            patch.object(scout, "issue_comments") as unchecked_fetch,
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=("no explicit payment signal", None),
            ),
            patch.object(scout, "extended_competition_reason", return_value=None) as competition,
            patch.object(scout, "fetch_repo_metadata", return_value=meta),
            patch.object(scout, "contribution_guide", return_value=None),
            patch.object(
                scout,
                "build_candidate",
                return_value={"lane": "paid"},
            ) as build,
        ):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}),
                ({"lane": "paid"}, None),
            )

        comments_fetch.assert_called_once_with(fresh, "t")
        unchecked_fetch.assert_not_called()
        competition.assert_called_once_with(fresh, "t", comments)
        build.assert_called_once_with(
            fresh,
            "paid",
            "explicit /reward comment: $50",
            meta,
            None,
            None,
        )

    def test_verify_non_payment_rejection_and_strategic_success(self) -> None:
        paid = issue(body="bounty $100", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(paid, None)),
            patch.object(
                paid_verification, "candidate_rejection_reason", return_value=("unfunded", "x")
            ),
        ):
            self.assertEqual(scout.verify(paid, "t", {}, {}, True)[1], "unfunded")

        strategic = issue(body="", title="Feature", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(strategic, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value=None),
            patch.object(scout, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "contribution_guide", return_value=None),
            patch.object(scout, "build_candidate", return_value={"lane": "strategic"}),
        ):
            self.assertEqual(
                scout.verify(strategic, "t", {}, {}),
                ({"lane": "strategic"}, None),
            )

    def test_add_reject_caps_examples(self) -> None:
        counts: dict[str, int] = {}
        examples: list[RejectionRecord] = []
        for i in range(15):
            scout.add_reject(counts, examples, {"html_url": str(i), "title": str(i)}, "why")
        self.assertEqual(counts["why"], 15)
        self.assertEqual(len(examples), 12)

    def test_timeline_wrapper_and_preflight_diagnostic_boundary(self) -> None:
        with patch.object(
            competition_policy,
            "timeline_open_pr_reason",
            return_value="timeline reason",
        ):
            self.assertEqual(scout.timeline_open_pr_reason(issue(), "t"), "timeline reason")

        diagnostic = issue(
            title="macOS M1 black screen after network change",
            body=(
                "Hard hang followed by kernel panic on Apple Silicon macOS. "
                "No deterministic repro; collected sysdiagnose and panic logs."
            ),
            comments=0,
        )
        self.assertEqual(
            scout.strategic_preflight_rejection(diagnostic),
            "hardware/kernel diagnostic report without actionable contributor scope",
        )


class DiscoveryTests(unittest.TestCase):
    def test_strategic_inspection_items_keeps_top_fifteen_per_repo(self) -> None:
        rows = [
            (
                100 - i,
                100 - i,
                0,
                issue(html_url=f"https://github.com/a/a/issues/{i + 1}"),
            )
            for i in range(18)
        ]
        rows.extend(
            [
                (90, 90, 0, issue(html_url="https://github.com/b/b/issues/1")),
                (89, 89, 0, issue(html_url="https://github.com/b/b/issues/2")),
                (120, 120, 0, issue(html_url="bad")),
            ]
        )
        with patch.object(scout, "STRATEGIC_ADAPTIVE_INSPECT_BUDGET", 0):
            selected = scout.strategic_inspection_items(rows)

        self.assertEqual(len(selected["a/a"]), 15)
        self.assertEqual(len(selected["b/b"]), 2)
        self.assertEqual(
            selected["a/a"][0]["html_url"],
            "https://github.com/a/a/issues/1",
        )
        self.assertNotIn("bad", selected)

    def test_strategic_inspection_adaptively_adds_contributor_wanted_overflow(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=300)
        rows = [
            (
                100 - i,
                100 - i,
                0,
                issue(
                    html_url=f"https://github.com/a/a/issues/{i + 1}",
                    title="ordinary task",
                    updated_at=old.isoformat(),
                ),
            )
            for i in range(15)
        ]
        strong = issue(
            html_url="https://github.com/a/a/issues/16",
            title="Add query parameter middleware",
            labels=[{"name": "contributor/wanted"}],
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        rows.append((70, 70, 0, strong))

        with (
            patch.object(scout, "STRATEGIC_INSPECT_PER_REPO", 15),
            patch.object(scout, "STRATEGIC_ADAPTIVE_INSPECT_BUDGET", 1),
        ):
            selected = scout.strategic_inspection_items(rows)

        self.assertEqual(len(selected["a/a"]), 16)
        self.assertEqual(selected["a/a"][-1]["html_url"], strong["html_url"])
        self.assertTrue(scout.possible_miss_signal(strong))

    def test_basic_rejection_audit_reason_filters_known_noise(self) -> None:
        self.assertIsNone(scout.basic_rejection_audit_reason(issue(pull_request={"url": "x"})))
        self.assertIsNone(scout.basic_rejection_audit_reason(issue(assignees=[{"login": "dev"}])))
        self.assertIsNone(
            scout.basic_rejection_audit_reason(
                issue(html_url="https://github.com/laclance/BountyScout/issues/1")
            )
        )
        self.assertIsNone(scout.basic_rejection_audit_reason(issue(title="Bounty Alert: test")))
        self.assertIsNone(
            scout.basic_rejection_audit_reason(issue(body="article writing proposal"))
        )

        crowded = scout.basic_rejection_audit_reason(issue(comments=paid_policy.MAX_COMMENTS + 1))
        self.assertEqual(
            crowded,
            "strong-looking result rejected by an unrecognized basic eligibility filter rule",
        )

        unknown = scout.basic_rejection_audit_reason(issue())
        self.assertEqual(
            unknown,
            "strong-looking result rejected by an unrecognized basic eligibility filter rule",
        )

    def test_possible_miss_signal_and_audit_cap(self) -> None:
        now = datetime.now(timezone.utc)
        strong = issue(
            title="Regression in proxy",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
            updated_at=(now - timedelta(days=3)).isoformat(),
        )
        stale = issue(
            title="Old bug",
            labels=[{"name": "bug"}],
            updated_at=(now - timedelta(days=200)).isoformat(),
        )
        self.assertTrue(scout.possible_miss_signal(strong))
        self.assertFalse(scout.possible_miss_signal(stale))
        self.assertFalse(
            scout.possible_miss_signal(
                issue(
                    title="🏆 Hall of Fame — October 2026",
                    labels=[{"name": "hall-of-fame"}],
                    body="Top Contributors\nMonthly Stats\nTotal Bounty Distributed: $4770",
                )
            )
        )

        detection_tracker = issue(
            html_url="https://github.com/kubestellar/docs/issues/7162",
            title="[aw] Detection Runs",
            user={"login": "github-actions[bot]"},
            labels=[{"name": "help wanted"}, {"name": "agentic-workflows"}],
            updated_at=now.isoformat(),
            body=(
                "This issue tracks all runs where threat detection flagged problems in "
                "agentic workflows in this repository. Each workflow run that completes "
                "with a detection warning or failure posts a comment here.\n\n"
                "This issue helps monitor the health of the threat detection system.\n\n"
                "This issue is automatically managed by GitHub Agentic Workflows. "
                "Do not close this issue manually.\n\n"
                "No action to take - Do not assign to an agent."
            ),
        )
        self.assertFalse(scout.possible_miss_signal(detection_tracker))

        audit: list[RejectionRecord] = []
        with patch.object(scout, "STRATEGIC_AUDIT_LIMIT", 2):
            scout.add_audit(audit, strong, "one")
            scout.add_audit(audit, strong, "two")
            scout.add_audit(audit, strong, "three")
        self.assertEqual(len(audit), 2)

    def test_detection_tracker_rejection_does_not_reach_near_miss_audit(self) -> None:
        tracker = issue(
            html_url="https://github.com/kubestellar/docs/issues/7162",
            title="[aw] Detection Runs",
            user={"login": "github-actions[bot]"},
            labels=[{"name": "help wanted"}, {"name": "agentic-workflows"}],
            updated_at=datetime.now(timezone.utc).isoformat(),
            body=(
                "This issue tracks all runs where threat detection flagged problems in "
                "agentic workflows in this repository. Each workflow run with a detection "
                "warning or failure posts a comment here.\n\n"
                "This issue helps monitor the health of the threat detection system.\n\n"
                "This issue is automatically managed by GitHub Agentic Workflows. "
                "Do not close this issue manually.\n\n"
                "No action to take - Do not assign to an agent."
            ),
        )
        reason = "automated monitoring tracker, not an implementation task"
        preview = candidate(
            url=tracker["html_url"],
            paid=False,
            career_score=51,
            priority_score=51,
        )
        with (
            patch.object(scout, "TARGET_REPOS", ["kubestellar/docs"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(
                scout,
                "target_repo_issue_pool",
                return_value=([tracker], None),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "build_candidate", return_value=preview),
            patch.object(scout, "verify", return_value=(None, reason)),
        ):
            found, rejected, examples, audit = scout.discover_strategic("t", set(), set(), {}, {})

        self.assertEqual(found, [])
        self.assertEqual(rejected[reason], 1)
        self.assertEqual(examples[0]["url"], tracker["html_url"])
        self.assertEqual(audit, [])

    def test_discover_strategic_keeps_crowded_issue_for_real_competition_checks(self) -> None:
        crowded = issue(
            html_url="https://github.com/g/g/issues/9",
            title="API compatibility regression",
            labels=[{"name": "good first issue"}, {"name": "bug"}],
            comments=paid_policy.MAX_COMMENTS + 2,
        )
        with (
            patch.object(scout, "TARGET_REPOS", ["g/g"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(
                scout,
                "target_repo_issue_pool",
                return_value=([crowded], None),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "issue_comments", return_value=[]),
            patch.object(
                scout,
                "verify",
                return_value=(
                    candidate(
                        url=crowded["html_url"],
                        paid=False,
                        career_score=70,
                        priority_score=70,
                        comments=crowded["comments"],
                    ),
                    None,
                ),
            ) as verify_mock,
        ):
            found, rejected, examples, audit = scout.discover_strategic("t", set(), set(), {}, {})

        self.assertEqual([item["url"] for item in found], [crowded["html_url"]])
        verify_mock.assert_called_once()
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])
        self.assertEqual(audit, [])

    def test_discover_strategic_audits_early_misses_and_top_pool_overflow(self) -> None:
        now = datetime.now(timezone.utc)
        dirty_weak = issue(
            html_url="https://github.com/d/d/issues/1",
            title="Documentation cleanup",
            body="",
            updated_at=(now - timedelta(days=200)).isoformat(),
        )
        archived_weak = issue(
            html_url="https://github.com/x/y/issues/2",
            title="Documentation cleanup",
            body="",
            updated_at=(now - timedelta(days=200)).isoformat(),
        )
        best = issue(
            html_url="https://github.com/g/g/issues/1",
            title="Proxy regression",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        overflow = issue(
            html_url="https://github.com/g/g/issues/2",
            title="DNS regression",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )

        def meta(repo: str, token: str | None) -> RepositoryMetadata:
            if repo == "x/y":
                return repo_meta(archived=True)
            return repo_meta()

        def preview(
            item_: dict[str, Any],
            lane: str,
            signal: str | None,
            meta_: dict[str, Any],
            guide: str | None,
            *rest: Any,
        ) -> dict[str, Any]:
            return candidate(
                url=item_["html_url"],
                paid=False,
                priority_score=90 if item_ is best else 80,
                career_score=90 if item_ is best else 80,
            )

        with (
            patch.object(scout, "TARGET_REPOS", ["g/g"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(
                scout,
                "target_repo_issue_pool",
                return_value=([dirty_weak, archived_weak, best, overflow], None),
            ),
            patch.object(
                paid_policy,
                "is_clean_candidate",
                side_effect=lambda item_: item_ is not dirty_weak,
            ),
            patch.object(scout, "fetch_repo_metadata", side_effect=meta),
            patch.object(scout, "build_candidate", side_effect=preview),
            patch.object(scout, "issue_comments", return_value=[]),
            patch.object(
                scout,
                "verify",
                return_value=(candidate(url=best["html_url"], paid=False, career_score=90), None),
            ),
            patch.object(scout, "STRATEGIC_INSPECT_PER_REPO", 1),
            patch.object(scout, "STRATEGIC_ADAPTIVE_INSPECT_BUDGET", 0),
        ):
            found, _, _, audit = scout.discover_strategic("t", set(), set(), {}, {})

        self.assertEqual([item["url"] for item in found], [best["html_url"]])
        self.assertTrue(any("adaptive repo inspection pool" in item["reason"] for item in audit))
        self.assertFalse(any(item["url"] == dirty_weak["html_url"] for item in audit))
        self.assertFalse(any(item["url"] == archived_weak["html_url"] for item in audit))

    def test_strategic_preflight_rejects_only_source_visible_states(self) -> None:
        with patch.object(paid_policy, "is_clean_candidate", return_value=False):
            self.assertEqual(
                scout.strategic_preflight_rejection(issue(comments=0)),
                "failed basic eligibility filter",
            )
        self.assertEqual(
            scout.strategic_preflight_rejection(
                issue(title="OSS Opportunity Queue: generated report", comments=0)
            ),
            "generated opportunity-scout report",
        )
        self.assertEqual(
            scout.strategic_preflight_rejection(issue(labels=[{"name": "support"}], comments=0)),
            "support/triage issue rather than a contributor task",
        )
        self.assertEqual(
            scout.strategic_preflight_rejection(
                issue(labels=[{"name": "lifecycle/rotten"}], comments=0)
            ),
            "issue is in an abandoned/rotten lifecycle state",
        )
        self.assertEqual(
            scout.strategic_preflight_rejection(
                issue(labels=[{"name": "needs reproduction"}], comments=0)
            ),
            "awaiting reproduction confirmation",
        )
        self.assertIsNone(scout.strategic_preflight_rejection(issue(comments=0)))
        self.assertEqual(
            scout.strategic_preflight_rejection(issue(labels=[{"name": "claimed"}], comments=2)),
            "issue is marked claimed by the project",
        )
        self.assertEqual(
            scout.strategic_preflight_rejection(issue(title="Plan to release v1.2.3", comments=1)),
            "release planning/tracking issue, not implementation work",
        )
        self.assertEqual(
            scout.strategic_preflight_rejection(
                issue(labels=[{"name": "needs-triage"}], comments=0)
            ),
            "awaiting maintainer triage",
        )
        self.assertIsNone(
            scout.strategic_preflight_rejection(
                issue(labels=[{"name": "needs-triage"}], comments=1)
            )
        )

    def test_discover_strategic_audits_target_source_failure(self) -> None:
        with (
            patch.object(scout, "TARGET_REPOS", ["a/a"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", ["global-q"]),
            patch.object(
                scout,
                "target_repo_issue_pool",
                return_value=([], "target repo discovery failed for a/a; scan coverage incomplete"),
            ),
            patch.object(github, "search_github", return_value={"items": []}),
        ):
            found, rejected, examples, audit = scout.discover_strategic("t", set(), set(), {}, {})
        self.assertEqual(found, [])
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["url"], "https://github.com/a/a/issues")
        self.assertIn("coverage incomplete", audit[0]["reason"])

    def test_default_discovery_searches_fit_five_request_budget(self) -> None:
        self.assertEqual(
            len(scout.PAID_DISCOVERY_QUERIES) + len(scout.STRATEGIC_GLOBAL_QUERIES),
            5,
        )
        self.assertEqual(
            len(scout.STRATEGIC_GLOBAL_QUERIES) * scout.STRATEGIC_GLOBAL_SEARCH_PER_PAGE,
            60,
        )
        self.assertIn('label:"bug" OR regression', scout.STRATEGIC_GLOBAL_QUERIES[0])

    def test_prefetch_discovery_searches_paces_all_queries_in_order(self) -> None:
        with (
            patch.object(scout, "PAID_DISCOVERY_QUERIES", ["p1", "p2"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", ["s1", "s2"]),
            patch.object(
                github,
                "search_github",
                side_effect=[
                    {"items": [{"id": 1}]},
                    {"items": [{"id": 2}]},
                    {"items": [{"id": 3}]},
                    {"items": [{"id": 4}]},
                ],
            ) as search,
            patch.object(scout, "sleep") as sleeper,
        ):
            paid, strategic = scout.prefetch_discovery_searches("t")

        self.assertEqual([query for query, _ in paid], ["p1", "p2"])
        self.assertEqual([query for query, _ in strategic], ["s1", "s2"])
        self.assertEqual([call.args[0] for call in search.call_args_list], ["p1", "p2", "s1", "s2"])
        self.assertEqual(search.call_args_list[0].kwargs["per_page"], 15)
        self.assertTrue(all(call.args[1] == "t" for call in search.call_args_list))
        self.assertEqual(sleeper.call_count, 3)
        sleeper.assert_called_with(scout.DISCOVERY_SEARCH_INTERVAL_SECONDS)
        self.assertEqual(
            search.call_args_list[-1].kwargs["per_page"], scout.STRATEGIC_GLOBAL_SEARCH_PER_PAGE
        )

    def test_discover_strategic_audits_global_search_failure(self) -> None:
        with (
            patch.object(scout, "TARGET_REPOS", []),
            patch.object(github, "search_github") as search,
        ):
            found, rejected, examples, audit = scout.discover_strategic(
                "t", set(), set(), {}, {}, [("global-q", {})]
            )
        search.assert_not_called()

        self.assertEqual(found, [])
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["url"], "https://github.com/issues")
        self.assertIn("global strategic discovery search failed", audit[0]["reason"])
        self.assertIn("coverage incomplete", audit[0]["reason"])

    def test_discover_paid_prefetched_failure_skips_without_duplicate_search(self) -> None:
        with (
            patch.object(github, "search_github") as search,
            patch.object(scout, "platform_paid_refs", return_value={}),
        ):
            found, rejected, examples = scout.discover_paid("t", set(), {}, {}, [("paid-q", {})])

        search.assert_not_called()
        self.assertEqual(found, [])
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])

    def test_discover_paid_search_and_platform_paths(self) -> None:
        a = issue(html_url="https://github.com/a/a/issues/1")
        duplicate = dict(a)
        dirty = issue(html_url="https://github.com/a/a/issues/2")
        platform = issue(html_url="https://github.com/p/p/issues/3")
        platform_dirty = issue(html_url="https://github.com/p/p/issues/4")
        platform_bad = "https://github.com/p/p/issues/5"

        with (
            patch.object(github, "search_github", return_value={"items": [a, duplicate, dirty]}),
            patch.object(
                paid_policy,
                "is_clean_candidate",
                side_effect=lambda x: (
                    x["html_url"] != dirty["html_url"]
                    and x["html_url"] != platform_dirty["html_url"]
                ),
            ),
            patch.object(
                scout,
                "verify",
                side_effect=[
                    (candidate(url=a["html_url"]), None),
                    (candidate(url=platform["html_url"]), None),
                ],
            ),
            patch.object(
                scout,
                "platform_paid_refs",
                return_value={
                    platform["html_url"]: "sig",
                    platform_dirty["html_url"]: "sig",
                    platform_bad: "sig",
                },
            ),
            patch.object(
                scout,
                "issue_from_github_url",
                side_effect=lambda url, token: {
                    platform["html_url"]: platform,
                    platform_dirty["html_url"]: platform_dirty,
                    platform_bad: None,
                }[url],
            ),
        ):
            found, rejected, examples = scout.discover_paid("t", set(), {}, {})
        selfEqual = self.assertEqual
        selfEqual({x["url"] for x in found}, {a["html_url"], platform["html_url"]})
        selfEqual(rejected, {})
        selfEqual(examples, [])

    def test_discover_paid_rejects_low_value_direct_and_platform_candidates(self) -> None:
        direct = issue(html_url="https://github.com/a/a/issues/1")
        platform = issue(html_url="https://github.com/p/p/issues/2")
        low_direct = candidate(url=direct["html_url"], cash_score=41, priority_score=41)
        low_platform = candidate(url=platform["html_url"], cash_score=42, priority_score=42)

        with (
            patch.object(github, "search_github", return_value={"items": [direct]}),
            patch.object(paid_policy, "is_clean_candidate", return_value=True),
            patch.object(
                scout,
                "verify",
                side_effect=[(low_direct, None), (low_platform, None)],
            ),
            patch.object(
                scout,
                "platform_paid_refs",
                return_value={
                    platform["html_url"]: "confirmed bounty platform feed (IssueHunt): $2"
                },
            ),
            patch.object(scout, "issue_from_github_url", return_value=platform),
        ):
            found, rejected, examples = scout.discover_paid("t", set(), {}, {})

        self.assertEqual(found, [])
        direct_reason = "cash score 41/100 below paid threshold 55/100"
        platform_reason = "cash score 42/100 below paid threshold 55/100"
        self.assertEqual(rejected[direct_reason], 1)
        self.assertEqual(rejected[platform_reason], 1)
        self.assertEqual({item["reason"] for item in examples}, {direct_reason, platform_reason})

    def test_discover_paid_records_rejections_and_seen(self) -> None:
        seen = issue(html_url="https://github.com/a/a/issues/1")
        bad = issue(html_url="https://github.com/a/a/issues/2")
        with (
            patch.object(github, "search_github", return_value={"items": [seen, bad]}),
            patch.object(paid_policy, "is_clean_candidate", return_value=True),
            patch.object(scout, "verify", return_value=(None, "claimed")),
            patch.object(scout, "platform_paid_refs", return_value={}),
        ):
            found, rejected, examples = scout.discover_paid("t", {seen["html_url"]}, {}, {})
        self.assertEqual(found, [])
        self.assertGreater(rejected["claimed"], 0)
        self.assertEqual(examples[0]["reason"], "claimed")

    def test_discover_strategic_filters_previews_and_verifies(self) -> None:
        invalid = issue(html_url="bad")
        archived = issue(html_url="https://github.com/x/y/issues/2")
        good = issue(html_url="https://github.com/g/g/issues/3", title="Feature")
        paid = issue(html_url="https://github.com/p/p/issues/4", body="bounty $100")
        items = [invalid, archived, good, paid]

        def meta(repo: str, token: str | None) -> RepositoryMetadata:
            if repo == "x/y":
                return repo_meta(archived=True)
            return repo_meta()

        with (
            patch.object(scout, "TARGET_REPOS", ["example/project"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(scout, "target_repo_issue_pool", return_value=(items, None)),
            patch.object(paid_policy, "is_clean_candidate", return_value=True),
            patch.object(scout, "fetch_repo_metadata", side_effect=meta),
            patch.object(
                scout,
                "build_candidate",
                side_effect=lambda item_, lane, signal, meta_, guide, *rest: candidate(
                    url=item_["html_url"],
                    paid=(lane == "paid"),
                    priority_score=90 if item_ is paid else 80,
                ),
            ),
            patch.object(
                scout,
                "verify",
                side_effect=lambda item_, *args, **kwargs: (
                    (None, "reject") if item_ is paid else (candidate(url=item_["html_url"]), None)
                ),
            ),
        ):
            found, rejected, examples, audit = scout.discover_strategic("t", set(), set(), {}, {})
        self.assertEqual([x["url"] for x in found], [good["html_url"]])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["url"], archived["html_url"])
        self.assertIn("repository metadata", audit[0]["reason"])
        self.assertEqual(rejected["reject"], 1)
        self.assertEqual(examples[0]["url"], paid["html_url"])


class FormattingAndMainTests(unittest.TestCase):
    def test_main_prefetches_all_discovery_searches_before_paid_work(self) -> None:
        order: list[str] = []
        paid_prefetch: list[SearchBatch] = [("paid-q", {"items": []})]
        strategic_prefetch: list[SearchBatch] = [("global-q", {"items": []})]

        def prefetch(
            _token: str | None,
        ) -> tuple[list[SearchBatch], list[SearchBatch]]:
            order.append("searches")
            return paid_prefetch, strategic_prefetch

        def paid(*args: Any) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
            order.append("paid")
            return [], {}, []

        def strategic_discovery(
            *args: Any,
        ) -> tuple[
            list[dict[str, Any]],
            dict[str, int],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]:
            order.append("strategic")
            return [], {}, [], []

        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/repo"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "prefetch_discovery_searches", side_effect=prefetch),
            patch.object(scout, "discover_paid", side_effect=paid) as paid_discovery,
            patch.object(scout, "discover_strategic", side_effect=strategic_discovery) as strategic,
        ):
            scout.main()

        self.assertEqual(order, ["searches", "paid", "strategic"])
        self.assertIs(paid_discovery.call_args.args[4], paid_prefetch)
        self.assertIs(strategic.call_args.args[5], strategic_prefetch)

    def test_main_state_load_error_stops_before_discovery(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                state,
                "load_seen_state",
                side_effect=state.SeenStateLoadError("corrupt state"),
            ),
            patch.object(scout, "discover_paid") as discover_paid,
        ):
            with self.assertRaises(state.SeenStateLoadError):
                scout.main()

        discover_paid.assert_not_called()

    def test_main_no_queue(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            io.StringIO() as buf,
            redirect_stdout(buf),
        ):
            scout.main()
            self.assertIn("No new verified OSS opportunities found.", buf.getvalue())

    def test_main_dedupes_notifies_reports_and_saves(self) -> None:
        low = candidate(priority_score=50, cash_score=50, career_score=50)
        high = candidate(priority_score=90, cash_score=90, career_score=90)
        strategic = candidate(
            url="https://github.com/example/project/issues/43",
            issue_number=43,
            paid=False,
            reward=None,
            priority_score=70,
            expected_hourly=None,
        )
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/BountyScout",
            "TELEGRAM_BOT_TOKEN": "tb",
            "TELEGRAM_CHAT_ID": "chat",
            "DISCORD_WEBHOOK_URL": "hook",
        }
        reject = {
            "title": "Related to #123",
            "url": "https://github.com/acme/upstream/issues/123",
            "reason": (
                "existing open implementation PR: https://github.com/acme/upstream/pull/456"
            ),
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState.from_urls(["old"])),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([low, high], {"r1": 1}, [reject])),
            patch.object(
                scout,
                "discover_strategic",
                return_value=(
                    [strategic],
                    {"r2": 2},
                    [],
                    [
                        {
                            "url": "https://github.com/acme/missed/issues/9",
                            "title": "Possible miss",
                            "reason": "strong-looking near miss",
                        }
                    ],
                ),
            ),
            patch.object(delivery, "send_telegram_notification", return_value=True) as tg,
            patch.object(delivery, "send_discord_notification", return_value=False) as dc,
            patch.object(delivery, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
        tg.assert_called_once()
        dc.assert_called_once()
        gh.assert_called_once()
        github_body = gh.call_args.args[3]
        self.assertNotIn("https://github.com/example/project/issues/", github_body)
        self.assertNotIn("https://github.com/acme/upstream/issues/", github_body)
        self.assertNotIn("https://github.com/acme/upstream/pull/", github_body)
        self.assertIn(
            "https://redirect.github.com/example/project/issues/42",
            github_body,
        )
        self.assertIn(
            "https://redirect.github.com/acme/upstream/issues/123",
            github_body,
        )
        self.assertIn(
            "https://redirect.github.com/acme/upstream/pull/456",
            github_body,
        )

        telegram_message = tg.call_args.args[2]
        self.assertIn(high["url"], telegram_message)

        saved = save.call_args.args[0]
        self.assertIn("old", saved)
        self.assertIn(high["url"], saved)
        self.assertIn(strategic["url"], saved)

    def test_main_incomplete_coverage_reports_and_preserves_seen_state(self) -> None:
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/BountyScout",
        }
        buf = io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState.from_urls(["old"])),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(
                scout,
                "discover_strategic",
                return_value=(
                    [],
                    {
                        "could not refresh source issue": 2,
                        "could not refresh issue comments": 2,
                        "could not verify open implementation PR timeline": 1,
                    },
                    [],
                    [],
                ),
            ),
            patch.object(delivery, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state") as save,
            redirect_stdout(buf),
        ):
            scout.main()

        gh.assert_called_once()
        self.assertIn("0 new verified candidates", gh.call_args.args[2])
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete", gh.call_args.args[3]
        )
        save.assert_not_called()
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_main_paid_search_failure_warns_and_preserves_seen_state(self) -> None:
        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/BountyScout"}
        strategic = candidate(paid=False)
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState.from_urls(["old"])),
            patch.object(
                scout,
                "prefetch_discovery_searches",
                return_value=([("paid-q", {})], []),
            ),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(
                scout,
                "discover_strategic",
                return_value=([strategic], {}, [], []),
            ),
            patch.object(delivery, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()

        gh.assert_called_once()
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete", gh.call_args.args[3]
        )
        self.assertIn("paid discovery search failed for query: paid-q", gh.call_args.args[3])
        save.assert_not_called()

    def test_main_discovery_failure_warns_and_preserves_seen_state(self) -> None:
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/BountyScout",
        }
        buf = io.StringIO()
        audit = [
            {
                "url": "https://github.com/issues",
                "title": "Global GitHub Search: global-q",
                "reason": (
                    "global strategic discovery search failed for query: global-q; "
                    "scan coverage incomplete"
                ),
            }
        ]
        strategic = candidate(paid=False)
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState.from_urls(["old"])),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(
                scout,
                "discover_strategic",
                return_value=([strategic], {}, [], audit),
            ),
            patch.object(delivery, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state") as save,
            redirect_stdout(buf),
        ):
            scout.main()

        gh.assert_called_once()
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete", gh.call_args.args[3]
        )
        self.assertIn("1 discovery/source/comment/competition checks failed", gh.call_args.args[3])
        save.assert_not_called()
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_main_github_delivery_failure_does_not_advance_state(self) -> None:
        paid = candidate()
        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/BountyScout"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([paid], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "create_github_issue", return_value=False),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()

        save.assert_not_called()

    def test_main_state_save_failure_is_reported(self) -> None:
        paid = candidate()
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}
        buf = io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "discover_paid", return_value=([paid], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "send_telegram_notification", return_value=True),
            patch.object(
                state,
                "save_seen_state",
                side_effect=state.SeenStateSaveError("save failed"),
            ),
            redirect_stdout(buf),
        ):
            scout.main()

        self.assertIn("Error saving state file: save failed", buf.getvalue())

    def test_main_no_delivery_does_not_save(self) -> None:
        paid = candidate()
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "discover_paid", return_value=([paid], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "send_telegram_notification", return_value=False),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
            save.assert_not_called()

    def test_main_quiet_complete_run_performs_bounded_maintenance(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        seen = state.SeenState.from_urls([old_url])
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(state, "load_seen_state", return_value=seen),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(
                github,
                "issue_lifecycle",
                return_value=github.IssueLifecycleResult("open"),
            ) as lifecycle,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()

        lifecycle.assert_called_once_with(old_url, None)
        save.assert_called_once()
        saved = save.call_args.args[0]
        record = saved.record(old_url)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNotNone(record.last_checked_at)

    def test_main_quiet_maintenance_save_failure_is_reported(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        buf = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([old_url]),
            ),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(
                github,
                "issue_lifecycle",
                return_value=github.IssueLifecycleResult("failed"),
            ),
            patch.object(
                state,
                "save_seen_state",
                side_effect=state.SeenStateSaveError("maintenance save failed"),
            ),
            redirect_stdout(buf),
        ):
            scout.main()

        self.assertIn("Error saving state file: maintenance save failed", buf.getvalue())

    def test_main_successful_delivery_saves_maintenance_and_new_urls_atomically(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        new = candidate(url="https://github.com/example/project/issues/100", issue_number=100)
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([old_url]),
            ),
            patch.object(scout, "discover_paid", return_value=([new], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "send_telegram_notification", return_value=True),
            patch.object(
                github,
                "issue_lifecycle",
                return_value=github.IssueLifecycleResult("closed"),
            ),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()

        saved = save.call_args.args[0]
        self.assertFalse(saved.contains(old_url))
        self.assertTrue(saved.contains(new["url"]))

    def test_main_failed_delivery_and_incomplete_coverage_skip_maintenance(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        paid = candidate()
        with (
            patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"},
                clear=True,
            ),
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([old_url]),
            ),
            patch.object(scout, "discover_paid", return_value=([paid], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "send_telegram_notification", return_value=False),
            patch.object(github, "issue_lifecycle") as lifecycle,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
        lifecycle.assert_not_called()
        save.assert_not_called()

        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/BountyScout"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([old_url]),
            ),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(
                scout,
                "discover_strategic",
                return_value=(
                    [],
                    {"could not refresh source issue": scout.STRATEGIC_COVERAGE_WARNING_THRESHOLD},
                    [],
                    [],
                ),
            ),
            patch.object(delivery, "create_github_issue", return_value=True),
            patch.object(github, "issue_lifecycle") as lifecycle,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
        lifecycle.assert_not_called()
        save.assert_not_called()


class CoverageGapTests(unittest.TestCase):
    def test_comment_payment_signal_can_reuse_supplied_comments(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": "/reward 9",
                "author_association": "OWNER",
                "user": {"login": "owner"},
            }
        ]
        with patch.object(scout, "issue_comments") as fetch:
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t", comments),
                "explicit /reward comment: $9",
            )
        fetch.assert_not_called()

    def test_trusted_non_command_comment_falls_through_to_next_comment(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": "I support funding this",
                "author_association": "OWNER",
                "user": {"login": "owner"},
            },
            {
                "body": "/reward 7",
                "author_association": "MEMBER",
                "user": {"login": "member"},
            },
        ]
        with patch.object(scout, "issue_comments", return_value=comments):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"),
                "explicit /reward comment: $7",
            )

    def test_platform_detail_empty_amount_and_no_amount_branches(self) -> None:
        opire_pages = {
            "https://app.opire.dev/home": (
                '<a href="/issues/A">a</a><a href="/issues/B">b</a><a href="/issues/C">c</a>'
            ),
            "https://app.opire.dev/issues/A": "",
            "https://app.opire.dev/issues/B": (
                "$50 bounty https://github.com/acme/widget/issues/2"
            ),
            "https://app.opire.dev/issues/C": ("funded https://github.com/acme/widget/issues/3"),
        }
        with patch.object(scout, "fetch_text", side_effect=opire_pages.get):
            refs = scout.opire_platform_refs()
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (Opire): $50",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/3"],
            "confirmed bounty platform feed (Opire)",
        )

        bountyhub_pages = {
            "https://www.bountyhub.dev/en/bounties": (
                '<a href="/en/bounty/view/A">a</a>'
                '<a href="/en/bounty/view/B">b</a>'
                '<a href="/en/bounty/view/C">c</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": "",
            "https://www.bountyhub.dev/en/bounty/view/B": (
                "$75 https://github.com/acme/widget/issues/4"
            ),
            "https://www.bountyhub.dev/en/bounty/view/C": (
                "funded https://github.com/acme/widget/issues/5"
            ),
        }
        with patch.object(scout, "fetch_text", side_effect=bountyhub_pages.get):
            refs = scout.bountyhub_platform_refs()
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/4"],
            "confirmed bounty platform feed (BountyHub): $75",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/5"],
            "confirmed bounty platform feed (BountyHub)",
        )

    def test_verify_success_with_comment_signal_cached_repo_and_guide(self) -> None:
        fresh = issue(body="", title="Task", comments=1)
        repo_cache: dict[str, RepositoryMetadata] = {"example/project": repo_meta()}
        guide_cache: dict[str, str | None] = {"example/project": "cached-guide"}
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                scout, "comment_payment_signal", return_value="explicit /reward comment: $50"
            ),
            patch.object(
                paid_verification,
                "candidate_rejection_reason",
                return_value=("no explicit payment signal", None),
            ),
            patch.object(paid_verification, "has_existing_implementation_pr", return_value=None),
            patch.object(paid_verification, "active_claim_reason", return_value=None),
            patch.object(scout, "fetch_repo_metadata") as fetch_meta,
            patch.object(scout, "contribution_guide") as guide,
            patch.object(scout, "build_candidate", return_value={"ok": True}),
        ):
            self.assertEqual(
                scout.verify(fresh, "t", repo_cache, guide_cache, True),
                ({"ok": True}, None),
            )
        fetch_meta.assert_not_called()
        guide.assert_not_called()

    def test_verify_strategic_rejection_branch(self) -> None:
        fresh = issue(body="", title="Feature", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(paid_policy, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value="support"),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {})[1], "support")

    def test_discover_paid_platform_seen_and_rejection_branches(self) -> None:
        source_seen = "https://github.com/a/a/issues/1"
        source_reject = "https://github.com/a/a/issues/2"
        item_reject = issue(html_url=source_reject)
        with (
            patch.object(github, "search_github", return_value={"items": []}),
            patch.object(
                scout,
                "platform_paid_refs",
                return_value={source_seen: "sig", source_reject: "sig"},
            ),
            patch.object(scout, "issue_from_github_url", return_value=item_reject),
            patch.object(paid_policy, "is_clean_candidate", return_value=True),
            patch.object(scout, "verify", return_value=(None, "claimed")),
        ):
            found, rejected, examples = scout.discover_paid("t", {source_seen}, {}, {})
        self.assertEqual(found, [])
        self.assertEqual(rejected["claimed"], 1)
        self.assertEqual(examples[0]["url"], source_reject)

    def test_discover_strategic_seen_dirty_and_cached_repo_branches(self) -> None:
        seen_item = issue(html_url="https://github.com/a/a/issues/1")
        dirty = issue(html_url="https://github.com/a/a/issues/2")
        cached = issue(html_url="https://github.com/a/a/issues/3", title="Feature")
        cache = {"a/a": repo_meta()}
        with (
            patch.object(scout, "TARGET_REPOS", ["a/a"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(
                scout,
                "target_repo_issue_pool",
                return_value=([seen_item, dirty, cached], None),
            ),
            patch.object(
                paid_policy,
                "is_clean_candidate",
                side_effect=lambda x: x["html_url"] != dirty["html_url"],
            ),
            patch.object(scout, "fetch_repo_metadata") as fetch_meta,
            patch.object(
                scout,
                "build_candidate",
                return_value=candidate(url=cached["html_url"], priority_score=70),
            ),
            patch.object(
                scout,
                "verify",
                return_value=(candidate(url=cached["html_url"]), None),
            ),
        ):
            found, rejected, examples, audit = scout.discover_strategic(
                "t", {seen_item["html_url"]}, set(), cache, {}
            )
        self.assertEqual([x["url"] for x in found], [cached["html_url"]])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["url"], dirty["html_url"])
        self.assertIn("basic eligibility filter", audit[0]["reason"])
        fetch_meta.assert_not_called()
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])

    def test_main_keeps_higher_duplicate_and_github_report_without_examples(self) -> None:
        high = candidate(priority_score=90)
        low = candidate(priority_score=40)
        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/repo"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([high, low], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state"),
        ):
            scout.main()
        body = gh.call_args.args[3]
        self.assertNotIn("Verification rejects", body)

    def test_main_short_circuit_conditions_with_no_delivery(self) -> None:
        item_ = candidate()
        env = {"TELEGRAM_BOT_TOKEN": "tb", "GITHUB_TOKEN": "tok"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "discover_paid", return_value=([item_], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "send_telegram_notification") as tg,
            patch.object(delivery, "create_github_issue") as gh,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
        tg.assert_not_called()
        gh.assert_not_called()
        save.assert_not_called()

    def test_root_entry_point_invokes_package_main(self) -> None:
        with patch.object(scout, "main") as package_main:
            runpy.run_path("opportunity_scout.py", run_name="__main__")
        package_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
