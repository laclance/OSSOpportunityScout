from __future__ import annotations

import unittest
import urllib.request
from typing import Any, cast
from unittest.mock import patch

from bountyscout import sources
from bountyscout import github
from bountyscout.types import GitHubIssue
from tests.helpers import FakeResponse, candidate


def issue(number: int, **overrides: Any) -> GitHubIssue:
    item: dict[str, Any] = {
        "html_url": f"https://github.com/example/project/issues/{number}",
        "title": f"Issue {number}",
        "body": "",
        "comments": 1,
        "labels": [],
    }
    item.update(overrides)
    return cast(GitHubIssue, item)


class TargetRepoSourceTests(unittest.TestCase):
    def test_target_repo_pool_paginates_past_prs_and_stops_at_real_issue_limit(self) -> None:
        pr = {
            "html_url": "https://github.com/example/project/pull/99",
            "pull_request": {"url": "x"},
        }
        i1, i2, i3 = issue(1), issue(2), issue(3)
        with patch.object(
            github,
            "github_get",
            side_effect=[
                [i1, pr, "bad", pr],
                [i2, i3, pr],
            ],
        ) as getter:
            items, error = sources.target_repo_issue_pool(
                "example/project",
                "t",
                fetch_per_page=4,
                fetch_pages=3,
                result_limit=3,
            )

        self.assertIsNone(error)
        self.assertEqual(
            [item["html_url"] for item in items], [i1["html_url"], i2["html_url"], i3["html_url"]]
        )
        self.assertEqual(getter.call_count, 2)
        self.assertIn("page=2", getter.call_args.args[0])

    def test_target_repo_pool_returns_partial_results_and_explicit_failure(self) -> None:
        i1 = issue(1)
        with patch.object(github, "github_get", side_effect=[[i1, i1], None]):
            items, error = sources.target_repo_issue_pool(
                "example/project",
                "t",
                fetch_per_page=2,
                fetch_pages=3,
                result_limit=5,
            )
        self.assertEqual([item["html_url"] for item in items], [i1["html_url"], i1["html_url"]])
        self.assertIn("scan coverage incomplete", str(error))

    def test_target_repo_pool_exhausts_pages_when_results_are_only_prs(self) -> None:
        pr = {"pull_request": {"url": "x"}}
        with patch.object(github, "github_get", side_effect=[[pr, pr], [pr, pr]]) as getter:
            items, error = sources.target_repo_issue_pool(
                "example/project",
                "t",
                fetch_per_page=2,
                fetch_pages=2,
                result_limit=5,
            )
        self.assertEqual(items, [])
        self.assertIsNone(error)
        self.assertEqual(getter.call_count, 2)


class GenericSourceTests(unittest.TestCase):
    def test_optional_json_fetch_auth_and_failure(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"html_url":"guide"}'),
        ) as opened:
            self.assertEqual(
                sources.github_get_optional("https://api.github.com/x", "tok"),
                {"html_url": "guide"},
            )
            self.assertEqual(opened.call_args.args[0].headers["Authorization"], "Bearer tok")

        with patch.object(urllib.request, "urlopen", side_effect=OSError("boom")):
            self.assertIsNone(sources.github_get_optional("https://api.github.com/x", None))

    def test_public_text_fetch_success_and_failure(self) -> None:
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(b"hello")):
            self.assertEqual(sources.fetch_text("https://example.test"), "hello")
        with patch.object(urllib.request, "urlopen", side_effect=OSError("boom")):
            self.assertEqual(sources.fetch_text("https://example.test"), "")

    def test_issue_from_github_url_validates_and_fetches(self) -> None:
        self.assertIsNone(sources.issue_from_github_url("bad", "t"))
        with patch.object(github, "github_get", return_value=[]):
            self.assertIsNone(
                sources.issue_from_github_url("https://github.com/a/b/issues/12", "t")
            )
        with patch.object(github, "github_get", return_value={"state": "open"}) as getter:
            self.assertEqual(
                sources.issue_from_github_url("https://github.com/a/b/issues/12", "t"),
                {"state": "open"},
            )
            self.assertIn("/repos/a/b/issues/12", getter.call_args.args[0])

    def test_contribution_guide_checks_common_paths(self) -> None:
        calls: list[str] = []

        def getter(url: str, token: str | None) -> Any:
            calls.append(url)
            return {"html_url": "guide"} if "docs/CONTRIBUTING.md" in url else None

        self.assertEqual(sources.contribution_guide("a/b", "t", getter), "guide")
        self.assertEqual(len(calls), 3)
        self.assertIsNone(sources.contribution_guide("a/b", "t", lambda *_: None))


class PlatformAdapterTests(unittest.TestCase):
    def test_issuehunt_handles_pagination_amount_and_empty_pages(self) -> None:
        pages = {
            "https://oss.issuehunt.io/issues": (
                '<a href="/r/apache/superset/issues/3821">x</a><span>$17.00</span>'
            ),
            "https://oss.issuehunt.io/issues?page=2": (
                '<a href="/r/acme/widget/issues/9">x</a><span>funded</span>'
            ),
        }
        refs = sources.issuehunt_platform_refs(lambda url: pages.get(url, ""), pages=2)
        self.assertEqual(
            refs["https://github.com/apache/superset/issues/3821"],
            "confirmed bounty platform feed (IssueHunt): $17.00",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/9"],
            "confirmed bounty platform feed (IssueHunt)",
        )
        self.assertEqual(sources.issuehunt_platform_refs(lambda _: "", pages=2), {})

    def test_opire_direct_and_detail_sources(self) -> None:
        pages = {
            "https://app.opire.dev/home": (
                r"https:\/\/github.com\/direct\/repo\/issues\/1 "
                '<a href="/issues/A">a</a><a href="/issues/B">b</a>'
            ),
            "https://app.opire.dev/issues/A": "",
            "https://app.opire.dev/issues/B": (
                "$50 bounty https://github.com/acme/widget/issues/2"
            ),
        }
        refs = sources.opire_platform_refs(
            lambda url: pages.get(url, ""),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertIn("https://github.com/direct/repo/issues/1", refs)
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (Opire): $50",
        )
        self.assertEqual(
            sources.opire_platform_refs(lambda _: "", fetch_limit=20, network_workers=2),
            {},
        )

    def test_bountyhub_direct_detail_and_amount(self) -> None:
        pages = {
            "https://www.bountyhub.dev/en/bounties": (
                r"https:\/\/github.com\/direct\/repo\/issues\/1 "
                '<a href="/en/bounty/view/A">a</a><a href="/en/bounty/view/B">b</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": "",
            "https://www.bountyhub.dev/en/bounty/view/B": (
                "Reward $125 https://github.com/acme/widget/issues/2"
            ),
        }
        amount_pattern = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"
        refs = sources.bountyhub_platform_refs(
            amount_pattern,
            lambda url: pages.get(url, ""),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertIn("https://github.com/direct/repo/issues/1", refs)
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (BountyHub): $125",
        )
        self.assertEqual(
            sources.bountyhub_platform_refs(
                amount_pattern,
                lambda _: "",
                fetch_limit=20,
                network_workers=2,
            ),
            {},
        )

    def test_platform_merge_isolates_one_loader_failure(self) -> None:
        def broken() -> dict[str, str]:
            raise RuntimeError("parser broke")

        refs = sources.platform_paid_refs(
            (
                lambda: {"a": "issuehunt"},
                broken,
                lambda: {"b": "bountyhub"},
            ),
            network_workers=3,
        )
        self.assertEqual(refs, {"a": "issuehunt", "b": "bountyhub"})

    def test_platform_merge_is_deduplicated_with_later_loader_precedence(self) -> None:
        refs = sources.platform_paid_refs(
            (
                lambda: {"u": "issuehunt"},
                lambda: {"v": "opire"},
                lambda: {"u": "bountyhub"},
            ),
            network_workers=3,
        )
        self.assertEqual(refs, {"u": "bountyhub", "v": "opire"})
        self.assertEqual(sources.platform_paid_refs((), network_workers=3), {})


class AdaptiveInspectionTests(unittest.TestCase):
    def test_keeps_base_rows_and_spends_global_budget_on_strong_overflow(self) -> None:
        provisional: list[sources.IssueRow] = []
        for repo in ("a/a", "b/b"):
            for i in range(4):
                provisional.append(
                    (
                        100 - i,
                        100 - i,
                        0,
                        issue(
                            i + 1,
                            html_url=f"https://github.com/{repo}/issues/{i + 1}",
                            labels=[{"name": "bug"}] if i >= 2 else [],
                        ),
                    )
                )

        selected = sources.strategic_inspection_items(
            provisional,
            base_per_repo=2,
            adaptive_budget=2,
            should_expand=lambda item: bool(item.get("labels")),
        )
        self.assertEqual(len(selected["a/a"]), 3)
        self.assertEqual(len(selected["b/b"]), 3)
        selected_urls = {item["html_url"] for rows in selected.values() for item in rows}
        self.assertIn("https://github.com/a/a/issues/3", selected_urls)
        self.assertIn("https://github.com/b/b/issues/3", selected_urls)
        self.assertNotIn("https://github.com/a/a/issues/4", selected_urls)
        self.assertNotIn("https://github.com/b/b/issues/4", selected_urls)

    def test_adaptive_overflow_skips_rows_without_expansion_signal(self) -> None:
        selected = sources.strategic_inspection_items(
            [
                (100, 100, 0, issue(1)),
                (90, 90, 0, issue(2)),
            ],
            base_per_repo=1,
            adaptive_budget=1,
            should_expand=lambda _: False,
        )
        self.assertEqual(
            [item["html_url"] for item in selected["example/project"]],
            [issue(1)["html_url"]],
        )

    def test_invalid_issue_urls_are_not_selected(self) -> None:
        selected = sources.strategic_inspection_items(
            [(100, 100, 0, issue(1, html_url="bad"))],
            base_per_repo=1,
            adaptive_budget=1,
            should_expand=lambda _: True,
        )
        self.assertEqual(selected, {})


class VerificationSettlementTests(unittest.TestCase):
    def test_candidate_rank_key_matches_queue_order(self) -> None:
        self.assertEqual(
            sources.candidate_rank_key(
                candidate(
                    priority_score=80,
                    career_score=70,
                    cash_score=0,
                    comments=2,
                )
            ),
            (80, 70, 0, -2),
        )

    def test_verification_upper_bound_accounts_for_score_uplift(self) -> None:
        row: sources.IssueRow = (70, 60, 0, issue(1, comments=3))
        self.assertEqual(
            sources.strategic_verification_upper_bound(
                row,
                score_uplift_bound=11,
            ),
            (81, 71, 0, -3),
        )
        capped: sources.IssueRow = (95, 96, 0, issue(2, comments=0))
        self.assertEqual(
            sources.strategic_verification_upper_bound(
                capped,
                score_uplift_bound=11,
            ),
            (100, 100, 0, 0),
        )

    def test_repo_slots_settle_only_when_remaining_cannot_displace_cutoff(self) -> None:
        verified = [
            candidate(priority_score=90, career_score=90, cash_score=0, comments=0),
            candidate(priority_score=85, career_score=85, cash_score=0, comments=1),
            candidate(priority_score=80, career_score=80, cash_score=0, comments=2),
        ]
        self.assertFalse(
            sources.strategic_repo_slots_settled(
                verified[:2],
                [],
                keep_per_repo=3,
                score_uplift_bound=11,
            )
        )
        self.assertTrue(
            sources.strategic_repo_slots_settled(
                verified,
                [],
                keep_per_repo=3,
                score_uplift_bound=11,
            )
        )

        competitive: list[sources.IssueRow] = [(75, 75, 0, issue(3))]
        self.assertFalse(
            sources.strategic_repo_slots_settled(
                verified,
                competitive,
                keep_per_repo=3,
                score_uplift_bound=11,
            )
        )

        safely_below: list[sources.IssueRow] = [(68, 68, 0, issue(4))]
        self.assertTrue(
            sources.strategic_repo_slots_settled(
                verified,
                safely_below,
                keep_per_repo=3,
                score_uplift_bound=11,
            )
        )


if __name__ == "__main__":
    unittest.main()
