from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from opportunity_scout import github
from opportunity_scout.strategic import discovery
from opportunity_scout.types import (
    Candidate,
    DiscoveryFailureReason,
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
    def test_excluded_repository_is_removed_before_metadata_and_inspection(self) -> None:
        blocked = issue(html_url="https://github.com/blocked/repo/issues/1")
        allowed = issue(html_url="https://github.com/example/project/issues/2")
        metadata = Mock(return_value=repo_meta())
        result = discovery.select_strategic_candidates(
            None,
            set(),
            set(),
            {},
            [("global", {"items": [blocked, allowed]})],
            target_repos=[],
            network_workers=1,
            target_repo_pool=lambda *_args: ([], None),
            basic_candidate=lambda _item: True,
            fetch_repo_metadata=metadata,
            payment_signal=lambda _item: None,
            build_candidate=lambda item, *_args: candidate(url=item["html_url"]),
            cache_locks=github.KeyedLockPool(),
            inspect_per_repo=1,
            adaptive_budget=0,
            repository_excluded=lambda repository: repository == "blocked/repo",
        )
        metadata.assert_called_once_with("example/project", None)
        self.assertEqual(list(result.ranked_by_repo), ["example/project"])
        self.assertEqual(result.ranked_by_repo["example/project"][0][3], allowed)
        self.assertEqual(result.audit, [])

    def test_language_filter_runs_before_shared_adaptive_overflow(self) -> None:
        rust_base = issue(
            html_url="https://github.com/example/rust/issues/1",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        rust_overflow = issue(
            html_url="https://github.com/example/rust/issues/2",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        go_base = issue(
            html_url="https://github.com/example/go/issues/1",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        go_overflow = issue(
            html_url="https://github.com/example/go/issues/2",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        items = [rust_base, rust_overflow, go_base, go_overflow]
        scores = {
            rust_base["html_url"]: 100,
            rust_overflow["html_url"]: 99,
            go_base["html_url"]: 98,
            go_overflow["html_url"]: 97,
        }
        metadata: dict[str, RepositoryMetadata] = {
            "example/rust": {**repo_meta(), "language": "Rust"},
            "example/go": repo_meta(),
        }

        def fetch(repo: str, _token: str | None) -> RepositoryMetadata:
            return metadata[repo]

        def build(
            item: GitHubIssue,
            _lane: CandidateLane,
            _signal: str | None,
            meta: RepositoryMetadata,
            _guide: str | None,
        ) -> Candidate:
            repo, number = github.issue_repo_and_number(item)
            score = scores[item["html_url"]]
            return candidate(
                repo=repo,
                issue_number=number,
                url=item["html_url"],
                priority_score=score,
                career_score=score,
                language=meta.get("language") or "Unknown",
            )

        unfiltered = discovery.select_strategic_candidates(
            None,
            set(),
            set(),
            {},
            [("global", {"items": items})],
            target_repos=[],
            network_workers=1,
            target_repo_pool=lambda *_args: ([], None),
            basic_candidate=lambda _item: True,
            fetch_repo_metadata=fetch,
            payment_signal=lambda _item: None,
            build_candidate=build,
            cache_locks=github.KeyedLockPool(),
            inspect_per_repo=1,
            adaptive_budget=1,
        )
        self.assertEqual(
            [row[3]["html_url"] for row in unfiltered.ranked_by_repo["example/rust"]],
            [rust_base["html_url"], rust_overflow["html_url"]],
        )
        self.assertEqual(
            [row[3]["html_url"] for row in unfiltered.ranked_by_repo["example/go"]],
            [go_base["html_url"]],
        )

        filtered = discovery.select_strategic_candidates(
            None,
            set(),
            set(),
            {},
            [("global", {"items": items})],
            target_repos=[],
            network_workers=1,
            target_repo_pool=lambda *_args: ([], None),
            basic_candidate=lambda _item: True,
            fetch_repo_metadata=fetch,
            payment_signal=lambda _item: None,
            build_candidate=build,
            cache_locks=github.KeyedLockPool(),
            inspect_per_repo=1,
            adaptive_budget=1,
            language_eligible=lambda _item, meta: meta.get("language") == "Go",
        )
        self.assertNotIn("example/rust", filtered.ranked_by_repo)
        self.assertEqual(
            [row[3]["html_url"] for row in filtered.ranked_by_repo["example/go"]],
            [go_base["html_url"], go_overflow["html_url"]],
        )
        self.assertEqual(filtered.audit, [])

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
        for rejected in (
            issue(assignees=[{"login": "dev"}]),
            issue(html_url="https://github.com/laclance/BountyScout/issues/1"),
            issue(title="Bounty Alert: test"),
            issue(body="article writing proposal"),
        ):
            self.assertIsNone(discovery.basic_rejection_audit_reason(rejected))
        self.assertIn(
            "unrecognized basic eligibility",
            discovery.basic_rejection_audit_reason(strong) or "",
        )

        audit: list[RejectionRecord] = []
        discovery.add_audit(audit, strong, "one", limit=1)
        discovery.add_audit(audit, strong, "two", limit=1)
        self.assertEqual(
            audit,
            [
                {
                    "url": strong.get("html_url"),
                    "title": strong.get("title"),
                    "reason": "one",
                }
            ],
        )

    def test_possible_miss_ignores_automated_ci_incident(self) -> None:
        cmux = issue(
            title="cmux NIGHTLY build is failing on main",
            body="This issue closes itself on the next successful publish.",
            labels=[{"name": "bug"}, {"name": "nightly-failure"}, {"name": "help wanted"}],
            user={"login": "github-actions[bot]"},
            comments=178,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertFalse(discovery.possible_miss_signal(cmux))

    def test_possible_miss_ignores_manual_umbrella_tracker(self) -> None:
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
        self.assertFalse(discovery.possible_miss_signal(grpc_umbrella))

    def test_possible_miss_ignores_security_disclosure(self) -> None:
        security = issue(
            html_url="https://github.com/kubernetes-sigs/external-dns/issues/6780",
            title="[Security Disclosure] Annotation-driven DNS record injection in external-dns",
            body=(
                "Severity: HIGH. CWE: CWE-285. CVSS 3.1: 8.1. "
                "Disclosure timeline: vulnerability discovered today."
            ),
            comments=0,
        )
        self.assertFalse(discovery.possible_miss_signal(security))

    def test_possible_miss_ignores_reward_history(self) -> None:
        hall_of_fame = issue(
            title="🏆 Hall of Fame — October 2026",
            labels=[{"name": "hall-of-fame"}],
            body="Top Contributors\nMonthly Stats\nTotal Bounty Distributed: $4770",
        )
        self.assertFalse(discovery.possible_miss_signal(hall_of_fame))

    def test_possible_miss_ignores_automated_monitoring_tracker(self) -> None:
        tracker = issue(
            html_url="https://github.com/kubestellar/docs/issues/7162",
            title="[aw] Detection Runs",
            user={"login": "github-actions[bot]"},
            labels=[{"name": "help wanted"}, {"name": "agentic-workflows"}],
            updated_at=datetime.now(timezone.utc).isoformat(),
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
        self.assertFalse(discovery.possible_miss_signal(tracker))

    def test_strategic_inspection_keeps_base_and_adds_strong_overflow(self) -> None:
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

        base = discovery.strategic_inspection_items(rows, adaptive_budget=0)
        self.assertEqual(len(base["a/a"]), 15)

        expanded = discovery.strategic_inspection_items(
            rows,
            base_per_repo=15,
            adaptive_budget=1,
        )
        self.assertEqual(len(expanded["a/a"]), 16)
        self.assertEqual(expanded["a/a"][-1].get("html_url"), strong.get("html_url"))

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
            self.assertEqual(meta.get("language"), "Go")
            self.assertIsNone(guide)
            score = 80 if item is first else 90
            return candidate(
                url=str(item.get("html_url")),
                priority_score=score,
                career_score=score,
            )

        selection = discovery.select_strategic_candidates(
            "tok",
            {str(seen.get("html_url"))},
            {str(paid.get("html_url"))},
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
            [row[3].get("html_url") for row in ranked],
            [second.get("html_url"), first.get("html_url")],
        )
        self.assertEqual(metadata_calls, ["a/a"])
        self.assertEqual(selection.audit, [])

    def test_selection_classifies_source_search_failures_but_not_archived_repo(self) -> None:
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
        failures = [
            item["reason"]
            for item in selection.audit
            if isinstance(item["reason"], DiscoveryFailureReason)
        ]
        self.assertEqual(len(failures), 2)
        self.assertTrue(any("target repo discovery failed" in reason for reason in failures))
        self.assertTrue(
            any("global strategic discovery search failed" in reason for reason in failures)
        )
        archived = next(
            item["reason"] for item in selection.audit if "repository is archived" in item["reason"]
        )
        self.assertNotIsInstance(archived, DiscoveryFailureReason)

    def test_repository_metadata_unavailable_is_discovery_failure_even_at_audit_limit(self) -> None:
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
            [],
            target_repos=["x/y"],
            network_workers=1,
            target_repo_pool=lambda *_: ([strong], None),
            basic_candidate=lambda _: True,
            fetch_repo_metadata=lambda *_: {},
            payment_signal=lambda _: None,
            build_candidate=lambda *_: self.fail("missing metadata must not build candidate"),
            cache_locks=github.KeyedLockPool(),
            inspect_per_repo=15,
            adaptive_budget=0,
            audit_limit=0,
        )

        self.assertEqual(selection.ranked_by_repo, {})
        self.assertEqual(len(selection.audit), 1)
        self.assertIsInstance(selection.audit[0]["reason"], DiscoveryFailureReason)
        self.assertIn("repository metadata unavailable", selection.audit[0]["reason"])


if __name__ == "__main__":
    unittest.main()
