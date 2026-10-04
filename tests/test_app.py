from __future__ import annotations

import io
import os
import runpy
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import opportunity_scout.app as scout
from opportunity_scout import delivery, github, preferences, run, sources
from opportunity_scout import paid as paid_policy
from opportunity_scout import paid_verification
from opportunity_scout import state
from opportunity_scout.strategic import competition as competition_policy
from opportunity_scout.types import (
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
            [item.get("html_url") for item in items],
            [
                i1.get("html_url"),
                i2.get("html_url"),
                i3.get("html_url"),
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
        self.assertEqual([item.get("html_url") for item in items], [i1.get("html_url")])
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

    def test_issue_comments_paths(self) -> None:
        self.assertEqual(scout.issue_comments({"html_url": "bad", "comments": 2}, "t"), [])
        self.assertEqual(scout.issue_comments(issue(comments=0), "t"), [])
        with patch.object(github, "github_collection", return_value={"not": "list"}):
            self.assertEqual(scout.issue_comments(issue(comments=1), "t"), [])
        with patch.object(github, "github_collection", return_value=[{"body": "x"}]):
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

    def test_platform_paid_refs_merge_precedence(self) -> None:
        with (
            patch.object(
                sources,
                "issuehunt_platform_refs",
                return_value={"u": "issuehunt"},
            ),
            patch.object(
                sources,
                "opire_platform_refs",
                return_value={"v": "opire"},
            ),
            patch.object(
                sources,
                "bountyhub_platform_refs",
                return_value={"u": "bountyhub"},
            ),
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
                return_value={"url": upstream.get("html_url")},
            ),
        ):
            result, reason = scout.verify(wrapper, "t", {}, {}, require_paid=True)
        self.assertIsNone(reason)
        self.assertEqual(result, {"url": upstream.get("html_url")})


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
            self.assertEqual(fresh.get("state"), "open")

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
            scout.strategic_rejection(issue(labels=["claimed"]), "t"),
            "issue is marked claimed by the project",
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

        with (
            patch.object(scout, "abandoned_lifecycle_reason", return_value="abandoned"),
            patch.object(scout, "readiness_pending_label_reason", return_value="readiness"),
        ):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "abandoned")

        with (
            patch.object(scout, "readiness_pending_label_reason", return_value="readiness"),
            patch.object(
                scout, "_strategic_classification_rejection", return_value="classification"
            ),
        ):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "readiness")

        with (
            patch.object(
                scout, "_strategic_classification_rejection", return_value="classification"
            ),
            patch.object(scout, "reporter_resolution_reason", return_value="resolved"),
            patch.object(
                scout,
                "maintainer_readiness_comment_state",
                return_value=(False, "comment hold"),
            ),
        ):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "classification")

        with (
            patch.object(scout, "reporter_resolution_reason", return_value="resolved"),
            patch.object(
                scout,
                "maintainer_readiness_comment_state",
                return_value=(False, "comment hold"),
            ),
        ):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "resolved")

        with patch.object(
            scout,
            "maintainer_readiness_comment_state",
            return_value=(False, "comment hold"),
        ):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "comment hold")

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
    def test_possible_miss_signal_delegates_to_strategic_discovery(self) -> None:
        item = issue()
        with patch(
            "opportunity_scout.strategic.discovery.possible_miss_signal",
            return_value=True,
        ) as signal:
            self.assertTrue(scout.possible_miss_signal(item))
        signal.assert_called_once_with(item)

    def test_strategic_discovery_audit_wrappers_delegate(self) -> None:
        item = issue()
        audit: list[RejectionRecord] = []
        provisional: list[sources.IssueRow] = []

        with patch(
            "opportunity_scout.strategic.discovery.basic_rejection_audit_reason",
            return_value="reason",
        ) as reason:
            self.assertEqual(scout.basic_rejection_audit_reason(item), "reason")
        reason.assert_called_once_with(item)

        with patch("opportunity_scout.strategic.discovery.add_audit") as add:
            scout.add_audit(audit, item, "reason")
        add.assert_called_once_with(
            audit,
            item,
            "reason",
            limit=scout.STRATEGIC_AUDIT_LIMIT,
        )

        with patch(
            "opportunity_scout.strategic.discovery.strategic_inspection_items",
            return_value={"a/a": [item]},
        ) as inspect:
            self.assertEqual(scout.strategic_inspection_items(provisional), {"a/a": [item]})
        inspect.assert_called_once_with(
            provisional,
            base_per_repo=scout.STRATEGIC_INSPECT_PER_REPO,
            adaptive_budget=scout.STRATEGIC_ADAPTIVE_INSPECT_BUDGET,
        )

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
            url=tracker.get("html_url"),
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
        self.assertEqual(examples[0].get("url"), tracker.get("html_url"))
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
                        url=crowded.get("html_url"),
                        paid=False,
                        career_score=70,
                        priority_score=70,
                        comments=crowded.get("comments"),
                    ),
                    None,
                ),
            ) as verify_mock,
        ):
            found, rejected, examples, audit = scout.discover_strategic("t", set(), set(), {}, {})

        self.assertEqual([item["url"] for item in found], [crowded.get("html_url")])
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
                url=item_.get("html_url"),
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
                return_value=(
                    candidate(url=best.get("html_url"), paid=False, career_score=90),
                    None,
                ),
            ),
            patch.object(scout, "STRATEGIC_INSPECT_PER_REPO", 1),
            patch.object(scout, "STRATEGIC_ADAPTIVE_INSPECT_BUDGET", 0),
        ):
            found, _, _, audit = scout.discover_strategic("t", set(), set(), {}, {})

        self.assertEqual([item["url"] for item in found], [best.get("html_url")])
        self.assertTrue(any("adaptive repo inspection pool" in item["reason"] for item in audit))
        self.assertFalse(any(item.get("url") == dirty_weak.get("html_url") for item in audit))
        self.assertFalse(any(item.get("url") == archived_weak.get("html_url") for item in audit))

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
        self.assertEqual(audit[0].get("url"), "https://github.com/a/a/issues")
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
        self.assertEqual(audit[0].get("url"), "https://github.com/issues")
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
                    x.get("html_url") != dirty.get("html_url")
                    and x.get("html_url") != platform_dirty.get("html_url")
                ),
            ),
            patch.object(
                scout,
                "verify",
                side_effect=[
                    (candidate(url=a.get("html_url")), None),
                    (candidate(url=platform.get("html_url")), None),
                ],
            ),
            patch.object(
                scout,
                "platform_paid_refs",
                return_value={
                    platform.get("html_url"): "sig",
                    platform_dirty.get("html_url"): "sig",
                    platform_bad: "sig",
                },
            ),
            patch.object(
                scout,
                "issue_from_github_url",
                side_effect=lambda url, token: {
                    platform.get("html_url"): platform,
                    platform_dirty.get("html_url"): platform_dirty,
                    platform_bad: None,
                }[url],
            ),
        ):
            found, rejected, examples = scout.discover_paid("t", set(), {}, {})
        selfEqual = self.assertEqual
        selfEqual({x["url"] for x in found}, {a.get("html_url"), platform.get("html_url")})
        selfEqual(rejected, {})
        selfEqual(examples, [])

    def test_discover_paid_rejects_low_value_direct_and_platform_candidates(self) -> None:
        direct = issue(html_url="https://github.com/a/a/issues/1")
        platform = issue(html_url="https://github.com/p/p/issues/2")
        low_direct = candidate(url=direct.get("html_url"), cash_score=41, priority_score=41)
        low_platform = candidate(url=platform.get("html_url"), cash_score=42, priority_score=42)

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
                    platform.get("html_url"): "confirmed bounty platform feed (IssueHunt): $2"
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
            found, rejected, examples = scout.discover_paid(
                "t", {str(seen.get("html_url"))}, {}, {}
            )
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
                    url=item_.get("html_url"),
                    paid=(lane == "paid"),
                    priority_score=90 if item_ is paid else 80,
                ),
            ),
            patch.object(
                scout,
                "verify",
                side_effect=lambda item_, *args, **kwargs: (
                    (None, "reject")
                    if item_ is paid
                    else (candidate(url=item_.get("html_url")), None)
                ),
            ),
        ):
            found, rejected, examples, audit = scout.discover_strategic("t", set(), set(), {}, {})
        self.assertEqual([x["url"] for x in found], [good.get("html_url")])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].get("url"), archived.get("html_url"))
        self.assertIn("repository metadata", audit[0]["reason"])
        self.assertEqual(rejected["reject"], 1)
        self.assertEqual(examples[0].get("url"), paid.get("html_url"))


class FormattingAndMainTests(unittest.TestCase):
    def test_main_prefetches_all_discovery_searches_before_paid_work(self) -> None:
        order: list[str] = []
        paid_prefetch: list[SearchBatch] = [("paid-q", {"items": []})]
        strategic_prefetch: list[SearchBatch] = [("global-q", {"items": []})]

        def prefetch(
            _token: str | None,
            *,
            scout_preferences: preferences.ScoutPreferences,
        ) -> tuple[list[SearchBatch], list[SearchBatch]]:
            order.append("searches")
            return paid_prefetch, strategic_prefetch

        def paid(
            *args: Any, **kwargs: Any
        ) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
            order.append("paid")
            return [], {}, []

        def strategic_discovery(
            *args: Any,
            **kwargs: Any,
        ) -> tuple[
            list[dict[str, Any]],
            dict[str, int],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]:
            order.append("strategic")
            return [], {}, [], []

        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/repo",
            "GITHUB_REPORTS_ENABLED": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "prefetch_discovery_searches", side_effect=prefetch),
            patch.object(scout, "discover_paid", side_effect=paid) as paid_discovery,
            patch.object(scout, "discover_strategic", side_effect=strategic_discovery) as strategic,
        ):
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

        self.assertEqual(order, ["searches", "paid", "strategic"])
        self.assertIs(paid_discovery.call_args.args[4], paid_prefetch)
        self.assertIs(strategic.call_args.args[5], strategic_prefetch)

    def test_main_github_report_flag_accepts_only_explicit_true(self) -> None:
        for value, expected in (
            (None, False),
            ("false", False),
            ("yes", False),
            ("1", False),
            ("TRUE", True),
            (" true ", True),
        ):
            with self.subTest(value=value):
                env = {
                    "GITHUB_TOKEN": "tok",
                    "GITHUB_REPOSITORY": "me/repo",
                }
                if value is not None:
                    env["GITHUB_REPORTS_ENABLED"] = value

                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(run, "run_combined_scan") as combined,
                ):
                    scout.main(
                        [
                            "--config",
                            str(Path(__file__).resolve().parents[1] / "scout.example.toml"),
                        ]
                    )

                config = combined.call_args.args[0]
                self.assertEqual(config.token, "tok")
                self.assertEqual(config.repo_fullname, "me/repo")
                self.assertIs(config.github_reports_enabled, expected)

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
                scout.main(
                    ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
                )

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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
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
            "GITHUB_REPOSITORY": "me/OSSOpportunityScout",
            "GITHUB_REPORTS_ENABLED": "true",
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
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

    def test_main_below_warning_threshold_preserves_state_with_and_without_candidates(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        for count in range(1, 5):
            for has_candidates in (False, True):
                with self.subTest(count=count, has_candidates=has_candidates):
                    seen = state.SeenState.from_urls([old_url])
                    output = io.StringIO()
                    with (
                        patch.dict(
                            os.environ,
                            {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"},
                            clear=True,
                        ),
                        patch.object(state, "load_seen_state", return_value=seen),
                        patch.object(
                            scout,
                            "discover_paid",
                            return_value=([candidate()] if has_candidates else [], {}, []),
                        ),
                        patch.object(
                            scout,
                            "discover_strategic",
                            return_value=([], {"could not refresh source issue": count}, [], []),
                        ),
                        patch.object(
                            delivery, "send_telegram_notification", return_value=True
                        ) as telegram,
                        patch.object(github, "issue_lifecycle") as lifecycle,
                        patch.object(state, "save_seen_state") as save,
                        redirect_stdout(output),
                    ):
                        scout.main(
                            [
                                "--config",
                                str(Path(__file__).resolve().parents[1] / "scout.example.toml"),
                            ]
                        )

                    self.assertEqual(telegram.call_count, int(has_candidates))
                    lifecycle.assert_not_called()
                    save.assert_not_called()
                    self.assertEqual(seen.urls(), {old_url})
                    self.assertNotIn("WARNING:", output.getvalue())
                    self.assertIn(
                        "Verification coverage incomplete; state was not updated.",
                        output.getvalue(),
                    )

    def test_main_incomplete_coverage_reports_and_preserves_seen_state(self) -> None:
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/OSSOpportunityScout",
            "GITHUB_REPORTS_ENABLED": "true",
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

        gh.assert_called_once()
        self.assertIn("0 new verified candidates", gh.call_args.args[2])
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete", gh.call_args.args[3]
        )
        save.assert_not_called()
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_main_paid_search_failure_warns_and_preserves_seen_state(self) -> None:
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/OSSOpportunityScout",
            "GITHUB_REPORTS_ENABLED": "true",
        }
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

        gh.assert_called_once()
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete", gh.call_args.args[3]
        )
        self.assertIn("paid discovery search failed for query: paid-q", gh.call_args.args[3])
        save.assert_not_called()

    def test_main_discovery_failure_warns_and_preserves_seen_state(self) -> None:
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/OSSOpportunityScout",
            "GITHUB_REPORTS_ENABLED": "true",
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

        gh.assert_called_once()
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete", gh.call_args.args[3]
        )
        self.assertIn("1 discovery/source/comment/competition checks failed", gh.call_args.args[3])
        save.assert_not_called()
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_main_github_delivery_failure_does_not_advance_state(self) -> None:
        paid = candidate()
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/OSSOpportunityScout",
            "GITHUB_REPORTS_ENABLED": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([paid], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "create_github_issue", return_value=False),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

        save.assert_not_called()

    def test_main_state_save_failure_propagates(self) -> None:
        paid = candidate()
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}
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
        ):
            with self.assertRaisesRegex(state.SeenStateSaveError, "save failed"):
                scout.main(
                    ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
                )

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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

        lifecycle.assert_called_once_with(old_url, None)
        save.assert_called_once()
        saved = save.call_args.args[0]
        record = saved.record(old_url)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNotNone(record.last_checked_at)

    def test_main_quiet_maintenance_save_failure_propagates(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
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
        ):
            with self.assertRaisesRegex(
                state.SeenStateSaveError,
                "maintenance save failed",
            ):
                scout.main(
                    ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
                )

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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )

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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
        lifecycle.assert_not_called()
        save.assert_not_called()

        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/OSSOpportunityScout",
            "GITHUB_REPORTS_ENABLED": "true",
        }
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
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
        self.assertEqual(examples[0].get("url"), source_reject)

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
                side_effect=lambda x: x.get("html_url") != dirty.get("html_url"),
            ),
            patch.object(scout, "fetch_repo_metadata") as fetch_meta,
            patch.object(
                scout,
                "build_candidate",
                return_value=candidate(url=cached.get("html_url"), priority_score=70),
            ),
            patch.object(
                scout,
                "verify",
                return_value=(candidate(url=cached.get("html_url")), None),
            ),
        ):
            found, rejected, examples, audit = scout.discover_strategic(
                "t", {str(seen_item.get("html_url"))}, set(), cache, {}
            )
        self.assertEqual([x["url"] for x in found], [cached.get("html_url")])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].get("url"), dirty.get("html_url"))
        self.assertIn("basic eligibility filter", audit[0]["reason"])
        fetch_meta.assert_not_called()
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])

    def test_main_keeps_higher_duplicate_and_github_report_without_examples(self) -> None:
        high = candidate(priority_score=90)
        low = candidate(priority_score=40)
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/repo",
            "GITHUB_REPORTS_ENABLED": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "prefetch_discovery_searches", return_value=([], [])),
            patch.object(scout, "discover_paid", return_value=([high, low], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [], [])),
            patch.object(delivery, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state"),
        ):
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
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
            scout.main(
                ["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")]
            )
        tg.assert_not_called()
        gh.assert_not_called()
        save.assert_not_called()

    def test_root_entry_point_invokes_package_main(self) -> None:
        with patch.object(scout, "main") as package_main:
            runpy.run_path("opportunity_scout.py", run_name="__main__")
        package_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
