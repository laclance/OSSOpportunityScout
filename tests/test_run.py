from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

from bountyscout import run, state
from bountyscout.types import (
    GitHubIssue,
    IssueLifecycleStatus,
    RejectionRecord,
    RepositoryMetadata,
    SearchBatch,
)
from tests.helpers import candidate

FIXED_TIME = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def empty_paid(
    _token: str | None,
    _seen: set[str],
    _repo_cache: dict[str, RepositoryMetadata],
    _guide_cache: dict[str, str | None],
    _search_results: list[SearchBatch] | None,
) -> run.PaidDiscoveryResult:
    return [], {}, []


def empty_strategic(
    _token: str | None,
    _seen: set[str],
    _paid_urls: set[str],
    _repo_cache: dict[str, RepositoryMetadata],
    _guide_cache: dict[str, str | None],
    _search_results: list[SearchBatch] | None,
) -> run.StrategicDiscoveryResult:
    return [], {}, [], []


def empty_prefetch(_token: str | None) -> tuple[list[SearchBatch], list[SearchBatch]]:
    return [], []


def append_audit(
    audit: list[RejectionRecord],
    item: GitHubIssue,
    reason: str,
) -> None:
    audit.append(
        {
            "url": item.get("html_url"),
            "title": item.get("title"),
            "reason": reason,
        }
    )


def false_telegram(_token: str, _chat_id: str, _message: str) -> bool:
    return False


def false_discord(_webhook: str, _message: str) -> bool:
    return False


def false_github(_repo: str, _token: str, _title: str, _body: str) -> bool:
    return False


def open_lifecycle(_url: str) -> IssueLifecycleStatus:
    return "open"


def dependencies() -> run.RunDependencies:
    return run.RunDependencies(
        discover_paid=empty_paid,
        discover_strategic=empty_strategic,
        prefetch_discovery_searches=empty_prefetch,
        append_audit=append_audit,
        send_telegram=false_telegram,
        send_discord=false_discord,
        send_github_report=false_github,
        issue_lifecycle=open_lifecycle,
    )


class QueueAssemblyTests(unittest.TestCase):
    def test_queue_keeps_strictly_higher_duplicate_and_exact_sort_order(self) -> None:
        original = candidate(
            title="original",
            priority_score=90,
            career_score=70,
            cash_score=10,
            comments=9,
        )
        equal = candidate(
            title="equal",
            priority_score=90,
            career_score=99,
            cash_score=99,
            comments=0,
        )
        higher = candidate(
            title="higher",
            priority_score=91,
            career_score=60,
            cash_score=0,
            comments=20,
        )
        other = candidate(
            url="https://github.com/example/project/issues/43",
            issue_number=43,
            priority_score=91,
            career_score=60,
            cash_score=0,
            comments=1,
        )

        equal_queue = run.assemble_queue([original], [equal])
        self.assertEqual(equal_queue[0]["title"], "original")

        queue = run.assemble_queue([original], [higher, other])
        self.assertEqual(queue[0]["url"], other["url"])
        self.assertEqual(queue[1]["title"], "higher")

    def test_queue_report_limit_remains_eight(self) -> None:
        items = [
            candidate(
                url=f"https://github.com/example/project/issues/{index}",
                issue_number=index,
                priority_score=100 - index,
            )
            for index in range(1, 11)
        ]
        queue = run.assemble_queue(items, [])
        self.assertEqual(len(queue), 8)
        self.assertEqual(queue[0]["issue_number"], 1)
        self.assertEqual(queue[-1]["issue_number"], 8)


class CoverageTests(unittest.TestCase):
    def test_coverage_threshold_and_failure_reasons_are_preserved(self) -> None:
        self.assertIsNone(run.coverage_status({}, []).warning)
        self.assertIsNone(run.coverage_status({"unrelated": 99}, []).warning)
        self.assertIsNone(run.coverage_status({"could not refresh source issue": 4}, []).warning)

        exact = run.coverage_status({"could not refresh source issue": 5}, [])
        self.assertEqual(exact.verification_failures, 5)
        self.assertEqual(exact.failure_count, 5)
        self.assertIsNotNone(exact.warning)

        all_reasons = run.coverage_status(
            {
                "could not refresh source issue": 1,
                "could not refresh issue comments": 2,
                "could not verify open implementation PR timeline": 3,
            },
            [],
        )
        self.assertEqual(all_reasons.verification_failures, 6)

    def test_any_discovery_failure_warns_and_counts_with_verification(self) -> None:
        status = run.coverage_status(
            {"could not refresh issue comments": 2},
            [
                {
                    "url": "https://github.com/issues",
                    "reason": "paid discovery search failed; scan coverage incomplete",
                }
            ],
        )
        self.assertEqual(status.discovery_failures, 1)
        self.assertEqual(status.failure_count, 3)
        self.assertIn("3 discovery/source/comment/competition checks failed", status.warning or "")


class RunLifecycleTests(unittest.TestCase):
    def test_quiet_complete_run_maintains_and_saves_only_when_checked(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        seen = state.SeenState.from_urls([old_url])
        saved: list[state.SeenState] = []
        with (
            patch.object(state, "load_seen_state", return_value=seen),
            patch.object(state, "save_seen_state", side_effect=saved.append),
        ):
            result = run.run_combined_scan(
                run.RunConfig(None, None, None, None, None),
                dependencies(),
                FIXED_TIME,
            )

        self.assertFalse(result.delivery.attempted)
        self.assertTrue(result.state_saved)
        self.assertEqual(len(saved), 1)
        record = saved[0].record(old_url)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.last_checked_at, "2026-10-02T10:00:00Z")

        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig(None, None, None, None, None),
                dependencies(),
                FIXED_TIME,
            )
        self.assertFalse(result.state_saved)
        save.assert_not_called()

    def test_successful_delivery_commits_maintenance_and_new_urls_atomically(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        new = candidate(url="https://github.com/example/project/issues/100", issue_number=100)
        saved: list[state.SeenState] = []

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [new], {}, []

        def telegram(_token: str, _chat_id: str, _message: str) -> bool:
            return True

        def closed_lifecycle(_url: str) -> IssueLifecycleStatus:
            return "closed"

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=telegram,
            send_discord=false_discord,
            send_github_report=false_github,
            issue_lifecycle=closed_lifecycle,
        )
        with (
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([old_url]),
            ),
            patch.object(state, "save_seen_state", side_effect=saved.append),
        ):
            result = run.run_combined_scan(
                run.RunConfig(None, None, "tb", "chat", None),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.delivered)
        self.assertTrue(result.state_saved)
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0].contains(old_url))
        self.assertTrue(saved[0].contains(new["url"]))
        record = saved[0].record(new["url"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.reported_at, "2026-10-02T10:00:00Z")

    def test_failed_or_unconfigured_delivery_never_advances_state(self) -> None:
        item = candidate()

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [item], {}, []

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=false_telegram,
            send_discord=false_discord,
            send_github_report=false_github,
            issue_lifecycle=open_lifecycle,
        )
        for config in (
            run.RunConfig(None, None, "tb", "chat", None),
            run.RunConfig(None, None, None, None, None),
        ):
            with (
                patch.object(state, "load_seen_state", return_value=state.SeenState()),
                patch.object(state, "maintain_seen_state") as maintain,
                patch.object(state, "save_seen_state") as save,
            ):
                result = run.run_combined_scan(config, deps, FIXED_TIME)
            self.assertFalse(result.delivery.delivered)
            maintain.assert_not_called()
            save.assert_not_called()

    def test_incomplete_coverage_can_deliver_but_never_advances_state(self) -> None:
        reports: list[str] = []

        def strategic(
            _token: str | None,
            _seen: set[str],
            _paid_urls: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.StrategicDiscoveryResult:
            return (
                [candidate(paid=False)],
                {"could not refresh source issue": 5},
                [],
                [],
            )

        def github_report(_repo: str, _token: str, _title: str, body: str) -> bool:
            reports.append(body)
            return True

        deps = run.RunDependencies(
            discover_paid=empty_paid,
            discover_strategic=strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=false_telegram,
            send_discord=false_discord,
            send_github_report=github_report,
            issue_lifecycle=open_lifecycle,
        )
        buf = io.StringIO()
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
            redirect_stdout(buf),
        ):
            result = run.run_combined_scan(
                run.RunConfig("tok", "me/repo", None, None, None),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.delivered)
        self.assertIsNotNone(result.coverage.warning)
        maintain.assert_not_called()
        save.assert_not_called()
        self.assertIn("Opportunity discovery/verification coverage is incomplete", reports[0])
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_paid_prefetch_failure_enters_audit_and_blocks_state(self) -> None:
        reports: list[str] = []

        def prefetch(_token: str | None) -> tuple[list[SearchBatch], list[SearchBatch]]:
            return [("paid-q", {})], []

        def strategic(
            _token: str | None,
            _seen: set[str],
            _paid_urls: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            search_results: list[SearchBatch] | None,
        ) -> run.StrategicDiscoveryResult:
            self.assertIsNotNone(search_results)
            return [candidate(paid=False)], {}, [], []

        def github_report(_repo: str, _token: str, _title: str, body: str) -> bool:
            reports.append(body)
            return True

        deps = run.RunDependencies(
            discover_paid=empty_paid,
            discover_strategic=strategic,
            prefetch_discovery_searches=prefetch,
            append_audit=append_audit,
            send_telegram=false_telegram,
            send_discord=false_discord,
            send_github_report=github_report,
            issue_lifecycle=open_lifecycle,
        )
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig("tok", "me/repo", None, None, None),
                deps,
                FIXED_TIME,
            )

        self.assertEqual(result.coverage.discovery_failures, 1)
        self.assertIn("paid discovery search failed for query: paid-q", reports[0])
        save.assert_not_called()

    def test_delivery_success_is_or_across_all_configured_channels(self) -> None:
        calls: list[str] = []

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [candidate()], {}, []

        def telegram(_token: str, _chat_id: str, _message: str) -> bool:
            calls.append("telegram")
            return False

        def discord(_webhook: str, _message: str) -> bool:
            calls.append("discord")
            return True

        def github_report(_repo: str, _token: str, _title: str, _body: str) -> bool:
            calls.append("github")
            return False

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=telegram,
            send_discord=discord,
            send_github_report=github_report,
            issue_lifecycle=open_lifecycle,
        )
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "save_seen_state"),
        ):
            result = run.run_combined_scan(
                run.RunConfig("tok", "me/repo", "tb", "chat", "hook"),
                deps,
                FIXED_TIME,
            )

        self.assertEqual(calls, ["telegram", "discord", "github"])
        self.assertTrue(result.delivery.attempted)
        self.assertTrue(result.delivery.delivered)

    def test_save_failure_is_reported_without_claiming_state_saved(self) -> None:
        item = candidate()

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [item], {}, []

        def telegram(_token: str, _chat_id: str, _message: str) -> bool:
            return True

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=telegram,
            send_discord=false_discord,
            send_github_report=false_github,
            issue_lifecycle=open_lifecycle,
        )
        buf = io.StringIO()
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(
                state,
                "save_seen_state",
                side_effect=state.SeenStateSaveError("save failed"),
            ),
            redirect_stdout(buf),
        ):
            result = run.run_combined_scan(
                run.RunConfig(None, None, "tb", "chat", None),
                deps,
                FIXED_TIME,
            )

        self.assertFalse(result.state_saved)
        self.assertIn("Error saving state file: save failed", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
