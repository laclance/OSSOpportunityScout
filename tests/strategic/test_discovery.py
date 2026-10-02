from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bountyscout import github
from bountyscout.strategic import discovery
from bountyscout.types import (
    Candidate,
    CandidateLane,
    GitHubIssue,
    GitHubSearchResult,
    RejectionRecord,
    RepositoryMetadata,
)
from tests.helpers import candidate, issue


def repo_meta(*, archived: bool = False) -> RepositoryMetadata:
    return {
        "stargazers_count": 1500,
        "pushed_at": "2026-10-01T00:00:00Z",
        "archived": archived,
        "language": "Go",
    }


class StrategicDiscoveryTests(unittest.TestCase):
    def test_possible_miss_and_bounded_audit_helpers(self) -> None:
        now = datetime.now(timezone.utc)
        strong = issue(
            title="Proxy regression",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
            updated_at=(now - timedelta(days=2)).isoformat(),
        )
        stale = issue(
            title="Old bug",
            labels=[{"name": "bug"}],
            updated_at=(now - timedelta(days=90)).isoformat(),
        )

        self.assertTrue(discovery.possible_miss_signal(strong))
        self.assertFalse(discovery.possible_miss_signal(stale))
        self.assertIsNone(discovery.basic_rejection_audit_reason(issue(pull_request={"url": "x"})))
        self.assertIn(
            "unrecognized basic eligibility",
            discovery.basic_rejection_audit_reason(strong) or "",
        )

        audit: list[RejectionRecord] = []
        discovery.add_audit(audit, strong, "one", limit=1)
        discovery.add_audit(audit, strong, "two", limit=1)
        self.assertEqual([item["reason"] for item in audit], ["one"])

    def test_global_search_results_preserve_query_order_and_page_budget(self) -> None:
        calls: list[tuple[str, str | None, int]] = []

        def search(query: str, token: str | None, per_page: int) -> GitHubSearchResult:
            calls.append((query, token, per_page))
            return {"items": []}

        results = discovery.strategic_global_search_results(
            "tok",
            search,
            queries=["q1", "q2"],
            per_page=17,
        )

        self.assertEqual([query for query, _ in results], ["q1", "q2"])
        self.assertEqual(calls, [("q1", "tok", 17), ("q2", "tok", 17)])

    def test_selection_deduplicates_sources_and_preserves_rank_order(self) -> None:
        first = issue(
            html_url="https://github.com/a/a/issues/1",
            title="First regression",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        second = issue(
            html_url="https://github.com/a/a/issues/2",
            title="Second regression",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        seen = issue(html_url="https://github.com/a/a/issues/3")
        paid = issue(html_url="https://github.com/a/a/issues/4")
        cache: dict[str, RepositoryMetadata] = {}
        metadata_calls: list[str] = []

        def pool(
            repo: str,
            token: str | None,
        ) -> tuple[list[GitHubIssue], str | None]:
            self.assertEqual((repo, token), ("a/a", "tok"))
            return [first, second, seen, paid], None

        def metadata(repo: str, token: str | None) -> RepositoryMetadata:
            self.assertEqual(token, "tok")
            metadata_calls.append(repo)
            return repo_meta()

        def build(
            item: GitHubIssue,
            lane: CandidateLane,
            signal: str | None,
            meta: RepositoryMetadata,
            guide: str | None,
        ) -> Candidate:
            self.assertEqual(lane, "strategic")
            self.assertIsNone(signal)
            self.assertEqual(meta["language"], "Go")
            self.assertIsNone(guide)
            score = 80 if item is first else 90
            return candidate(
                url=str(item["html_url"]),
                priority_score=score,
                career_score=score,
            )

        selection = discovery.select_strategic_candidates(
            "tok",
            {str(seen["html_url"])},
            {str(paid["html_url"])},
            cache,
            [("global", {"items": [first]})],
            target_repos=["a/a"],
            network_workers=2,
            target_repo_pool=pool,
            basic_candidate=lambda _: True,
            fetch_repo_metadata=metadata,
            payment_signal=lambda _: None,
            build_candidate=build,
            cache_locks=github.KeyedLockPool(),
            inspect_per_repo=15,
            adaptive_budget=0,
            audit_limit=20,
        )

        ranked = selection.ranked_by_repo["a/a"]
        self.assertEqual(
            [row[3]["html_url"] for row in ranked],
            [second["html_url"], first["html_url"]],
        )
        self.assertEqual(metadata_calls, ["a/a"])
        self.assertEqual(selection.audit, [])

    def test_selection_audits_source_search_and_archived_metadata_failures(self) -> None:
        strong = issue(
            html_url="https://github.com/x/y/issues/2",
            title="Proxy regression",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        selection = discovery.select_strategic_candidates(
            "tok",
            set(),
            set(),
            {},
            [("broken", {})],
            target_repos=["x/y"],
            network_workers=1,
            target_repo_pool=lambda *_: (
                [strong],
                "target repo discovery failed for x/y; scan coverage incomplete",
            ),
            basic_candidate=lambda _: True,
            fetch_repo_metadata=lambda *_: repo_meta(archived=True),
            payment_signal=lambda _: None,
            build_candidate=lambda *_: self.fail("archived candidate should not be built"),
            cache_locks=github.KeyedLockPool(),
            inspect_per_repo=15,
            adaptive_budget=0,
            audit_limit=20,
        )

        self.assertEqual(selection.ranked_by_repo, {})
        self.assertEqual(len(selection.audit), 3)
        self.assertTrue(
            any("target repo discovery failed" in item["reason"] for item in selection.audit)
        )
        self.assertTrue(
            any(
                "global strategic discovery search failed" in item["reason"]
                for item in selection.audit
            )
        )
        self.assertTrue(any("repository metadata" in item["reason"] for item in selection.audit))


if __name__ == "__main__":
    unittest.main()
