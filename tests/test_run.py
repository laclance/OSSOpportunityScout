from __future__ import annotations

import io
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import patch

import opportunity_scout.app as scout
from opportunity_scout import github, reporting, run, sources, state
from opportunity_scout.types import (
    DiscoveryFailureReason,
    GitHubIssue,
    IssueLifecycleStatus,
    RejectionRecord,
    RepositoryMetadata,
    SearchBatch,
    SourceFailureReason,
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

    def test_queue_ranking_uses_canonical_candidate_rank_key(self) -> None:
        first = candidate(
            url="https://github.com/example/project/issues/41",
            issue_number=41,
            priority_score=70,
        )
        second = candidate(
            url="https://github.com/example/project/issues/42",
            issue_number=42,
            priority_score=80,
        )
        original_rank_key = sources.candidate_rank_key
        with patch.object(
            sources,
            "candidate_rank_key",
            wraps=original_rank_key,
        ) as rank_key:
            queue = run.assemble_queue([first, second], [])

        self.assertEqual(
            [item["url"] for item in queue],
            [second["url"], first["url"]],
        )
        self.assertEqual(
            [call.args[0] for call in rank_key.call_args_list],
            [first, second],
        )

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
    def test_semantic_source_failure_completeness_is_independent_of_warning_threshold(self) -> None:
        reason = SourceFailureReason("source evidence transport failed")
        for count in (0, 1, 2, 3, 4, 5, 6):
            for threshold in (1, 5, 99):
                with self.subTest(count=count, threshold=threshold):
                    status = run.coverage_status(
                        {reason: count},
                        [],
                        warning_threshold=threshold,
                    )
                    self.assertEqual(status.complete, count == 0)
                    self.assertEqual(status.failure_count, count)
                    self.assertEqual(status.warning is not None, count >= threshold)

    def test_display_reason_text_is_not_the_coverage_protocol(self) -> None:
        status = run.coverage_status(
            {
                "could not refresh source issue": 99,
                "repository is archived": 99,
            },
            [],
            paid_rejects={"could not verify active claim comments": 99},
        )
        self.assertTrue(status.complete)
        self.assertEqual(status.failure_count, 0)
        self.assertIsNone(status.warning)

    def test_semantic_discovery_and_paid_source_failures_always_warn(self) -> None:
        status = run.coverage_status(
            {},
            [
                {
                    "url": "https://github.com/issues",
                    "reason": DiscoveryFailureReason("strategic discovery transport failed"),
                }
            ],
            paid_rejects={
                SourceFailureReason("paid comment evidence unavailable"): 2,
                DiscoveryFailureReason("paid Search unavailable"): 3,
            },
        )
        self.assertEqual(status.verification_failures, 2)
        self.assertEqual(status.discovery_failures, 4)
        self.assertEqual(status.failure_count, 6)
        self.assertFalse(status.complete)
        self.assertIn("6 discovery/source/comment/competition checks failed", status.warning or "")


class DeliveryRenderingTests(unittest.TestCase):
    def test_github_report_body_render_count_by_destination(self) -> None:
        configs = (
            (
                "public",
                run.RunConfig("tok", "me/repo", None, None, None, github_reports_enabled=True),
                [("public", "shared report")],
                False,
            ),
            (
                "private",
                run.RunConfig(
                    None,
                    None,
                    None,
                    None,
                    None,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                [("private", "shared report")],
                True,
            ),
            (
                "both",
                run.RunConfig(
                    "tok",
                    "me/repo",
                    None,
                    None,
                    None,
                    github_reports_enabled=True,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                [("public", "shared report"), ("private", "shared report")],
                True,
            ),
        )

        for name, config, expected_calls, expected_delivered in configs:
            with self.subTest(name=name):
                calls: list[tuple[str, str]] = []

                def public_report(_repo: str, _token: str, _title: str, body: str) -> bool:
                    calls.append(("public", body))
                    return False

                def private_report(_repo: str, _token: str, _title: str, body: str) -> bool:
                    calls.append(("private", body))
                    return True

                deps = replace(
                    dependencies(),
                    send_github_report=public_report,
                    send_private_github_report=private_report,
                )
                with patch.object(
                    reporting,
                    "github_report_body",
                    return_value="shared report",
                ) as render:
                    result = run._deliver(
                        config,
                        deps,
                        [candidate()],
                        "2026-10-02 10:00 UTC",
                        paid_examples=[],
                        strategic_examples=[],
                        strategic_audit=[],
                        rejects={},
                        coverage_warning=None,
                    )

                render.assert_called_once()
                self.assertEqual(calls, expected_calls)
                self.assertTrue(result.attempted)
                self.assertEqual(result.delivered, expected_delivered)

    def test_non_github_delivery_and_missing_private_sender_skip_report_rendering(self) -> None:
        configs = (
            ("none", run.RunConfig(None, None, None, None, None), False),
            (
                "notifications",
                run.RunConfig(None, None, "tb", "chat", "hook"),
                True,
            ),
            (
                "missing-private-sender",
                run.RunConfig(
                    None,
                    None,
                    None,
                    None,
                    None,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                True,
            ),
        )

        for name, config, expected_attempted in configs:
            with self.subTest(name=name):
                with patch.object(reporting, "github_report_body") as render:
                    result = run._deliver(
                        config,
                        dependencies(),
                        [candidate()],
                        "2026-10-02 10:00 UTC",
                        paid_examples=[],
                        strategic_examples=[],
                        strategic_audit=[],
                        rejects={},
                        coverage_warning=None,
                    )

                render.assert_not_called()
                self.assertEqual(result.attempted, expected_attempted)
                self.assertFalse(result.delivered)


class RunLifecycleTests(unittest.TestCase):
    def test_any_recognized_failure_blocks_new_urls_and_quiet_maintenance(self) -> None:
        failures: list[tuple[dict[str, int], dict[str, int], list[RejectionRecord]]] = [
            ({}, {SourceFailureReason(reason): count}, [])
            for reason in (
                "source refresh failed",
                "comment refresh failed",
                "competition timeline failed",
            )
            for count in range(1, 7)
        ]
        failures.append(({SourceFailureReason("paid claim evidence failed"): 1}, {}, []))
        for reason in (
            "paid discovery failed",
            "target repository discovery failed",
            "global strategic discovery failed",
        ):
            failures.append(({}, {}, [{"reason": DiscoveryFailureReason(reason)}]))

        for paid_rejects, strategic_rejects, audit in failures:
            for has_candidates in (False, True):
                for threshold in (5, 99):
                    with self.subTest(
                        paid=paid_rejects,
                        strategic=strategic_rejects,
                        audit=audit,
                        has_candidates=has_candidates,
                        threshold=threshold,
                    ):
                        items = [candidate()] if has_candidates else []

                        def paid(
                            _token: str | None,
                            _seen: set[str],
                            _repo_cache: dict[str, RepositoryMetadata],
                            _guide_cache: dict[str, str | None],
                            _search_results: list[SearchBatch] | None,
                        ) -> run.PaidDiscoveryResult:
                            return items, paid_rejects, []

                        def strategic(
                            _token: str | None,
                            _seen: set[str],
                            _paid_urls: set[str],
                            _repo_cache: dict[str, RepositoryMetadata],
                            _guide_cache: dict[str, str | None],
                            _search_results: list[SearchBatch] | None,
                        ) -> run.StrategicDiscoveryResult:
                            return [], strategic_rejects, [], audit

                        def telegram(_token: str, _chat_id: str, _message: str) -> bool:
                            return True

                        deps = replace(
                            dependencies(),
                            discover_paid=paid,
                            discover_strategic=strategic,
                            send_telegram=telegram,
                        )
                        seen = state.SeenState.from_urls(
                            ["https://github.com/example/project/issues/99"]
                        )
                        output = io.StringIO()
                        with (
                            patch.object(state, "load_seen_state", return_value=seen),
                            patch.object(state, "maintain_seen_state") as maintain,
                            patch.object(state, "save_seen_state") as save,
                            redirect_stdout(output),
                        ):
                            result = run.run_combined_scan(
                                run.RunConfig(None, None, "tb", "chat", None),
                                deps,
                                FIXED_TIME,
                                coverage_warning_threshold=threshold,
                            )

                        self.assertFalse(result.coverage.complete)
                        self.assertEqual(result.queue, tuple(items))
                        self.assertEqual(
                            result.delivery.delivered,
                            has_candidates or result.coverage.warning is not None,
                        )
                        self.assertFalse(result.state_saved)
                        maintain.assert_not_called()
                        save.assert_not_called()
                        self.assertIn(
                            "Verification coverage incomplete; state was not updated.",
                            output.getvalue(),
                        )

    def test_quiet_complete_run_maintains_and_saves_only_when_checked(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        seen = state.SeenState.from_urls([old_url])
        saved: list[state.SeenState] = []
        with (
            patch.object(state, "load_seen_state", return_value=seen),
            patch.object(
                state,
                "save_seen_state",
                side_effect=lambda next_state, _path: saved.append(next_state),
            ),
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

    def test_telegram_only_successful_delivery_commits_maintenance_and_new_urls_atomically(
        self,
    ) -> None:
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
            patch.object(
                state,
                "save_seen_state",
                side_effect=lambda next_state, _path: saved.append(next_state),
            ),
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

    def test_github_credentials_alone_do_not_attempt_public_delivery(self) -> None:
        item = candidate()
        github_calls: list[str] = []

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [item], {}, []

        def github_report(_repo: str, _token: str, _title: str, _body: str) -> bool:
            github_calls.append("github")
            return True

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=false_telegram,
            send_discord=false_discord,
            send_github_report=github_report,
            issue_lifecycle=open_lifecycle,
        )
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig("tok", "me/repo", None, None, None),
                deps,
                FIXED_TIME,
            )

        self.assertEqual(github_calls, [])
        self.assertFalse(result.delivery.attempted)
        self.assertFalse(result.delivery.delivered)
        maintain.assert_not_called()
        save.assert_not_called()

    def test_explicit_github_report_enablement_preserves_delivery(self) -> None:
        item = candidate()
        github_calls: list[str] = []

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [item], {}, []

        def github_report(_repo: str, _token: str, _title: str, _body: str) -> bool:
            github_calls.append("github")
            return True

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
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
                run.RunConfig(
                    "tok",
                    "me/repo",
                    None,
                    None,
                    None,
                    github_reports_enabled=True,
                ),
                deps,
                FIXED_TIME,
            )

        self.assertEqual(github_calls, ["github"])
        self.assertTrue(result.delivery.attempted)
        self.assertTrue(result.delivery.delivered)
        self.assertTrue(result.state_saved)
        save.assert_called_once()

    def test_discord_only_successful_delivery_advances_state(self) -> None:
        item = candidate()

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [item], {}, []

        def discord(_webhook: str, _message: str) -> bool:
            return True

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=false_telegram,
            send_discord=discord,
            send_github_report=false_github,
            issue_lifecycle=open_lifecycle,
        )
        saved: list[state.SeenState] = []
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(
                state,
                "save_seen_state",
                side_effect=lambda next_state, _path: saved.append(next_state),
            ),
        ):
            result = run.run_combined_scan(
                run.RunConfig(None, None, None, None, "hook"),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.attempted)
        self.assertTrue(result.delivery.delivered)
        self.assertTrue(result.state_saved)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].contains(item["url"]))

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
                {SourceFailureReason("source refresh failed"): 5},
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
                run.RunConfig(
                    "tok",
                    "me/repo",
                    None,
                    None,
                    None,
                    github_reports_enabled=True,
                ),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.delivered)
        self.assertIsNotNone(result.coverage.warning)
        maintain.assert_not_called()
        save.assert_not_called()
        self.assertIn("Opportunity discovery/verification coverage is incomplete", reports[0])
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_platform_source_hydration_failure_can_deliver_but_never_advances_state(
        self,
    ) -> None:
        source_url = "https://github.com/platform/project/issues/7"
        delivered = candidate(paid=False)

        def prefetch(_token: str | None) -> tuple[list[SearchBatch], list[SearchBatch]]:
            return [("paid-q", {"items": []})], []

        def strategic(
            _token: str | None,
            _seen: set[str],
            _paid_urls: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.StrategicDiscoveryResult:
            return [delivered], {}, [], []

        def telegram(_token: str, _chat_id: str, _message: str) -> bool:
            return True

        deps = replace(
            dependencies(),
            discover_paid=scout.discover_paid,
            discover_strategic=strategic,
            prefetch_discovery_searches=prefetch,
            send_telegram=telegram,
        )
        source_error = urllib.error.HTTPError(
            "https://api.github.com/repos/platform/project/issues/7",
            401,
            "unauthorized",
            Message(),
            None,
        )
        with (
            patch.object(
                scout,
                "platform_paid_refs",
                return_value=sources.PlatformDiscoveryResult(
                    refs={source_url: "official platform signal"},
                    failures=(),
                ),
            ),
            patch.object(urllib.request, "urlopen", side_effect=source_error),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig("tok", "me/repo", "tb", "chat", None),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.delivered)
        self.assertEqual(result.queue, (delivered,))
        self.assertEqual(
            (
                result.coverage.verification_failures,
                result.coverage.complete,
                result.state_saved,
                save.call_count,
            ),
            (1, False, False, 0),
        )
        maintain.assert_not_called()

    def test_platform_discovery_failure_can_deliver_but_never_advances_state(
        self,
    ) -> None:
        messages: list[str] = []
        item = candidate()
        failure = DiscoveryFailureReason(
            "official bounty-platform discovery failed for Opire; scan coverage incomplete"
        )

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return (
                [item],
                {failure: 1},
                [
                    {
                        "url": "https://github.com/issues",
                        "title": "Official bounty-platform discovery",
                        "reason": failure,
                    }
                ],
            )

        def telegram(_token: str, _chat_id: str, message: str) -> bool:
            messages.append(message)
            return True

        deps = replace(
            dependencies(),
            discover_paid=paid,
            send_telegram=telegram,
        )
        output = io.StringIO()
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
            redirect_stdout(output),
        ):
            result = run.run_combined_scan(
                run.RunConfig(None, None, "tb", "chat", None),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.delivered)
        self.assertEqual(result.queue, (item,))
        self.assertEqual(result.coverage.discovery_failures, 1)
        self.assertFalse(result.coverage.complete)
        self.assertFalse(result.state_saved)
        maintain.assert_not_called()
        save.assert_not_called()
        self.assertIn(
            "Opportunity discovery/verification coverage is incomplete",
            messages[0],
        )
        self.assertIn(
            "Verification coverage incomplete; state was not updated.",
            output.getvalue(),
        )

    def test_paid_claim_verification_failure_can_deliver_but_never_advances_state(self) -> None:
        reports: list[str] = []

        def paid(
            _token: str | None,
            _seen: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.PaidDiscoveryResult:
            return [], {SourceFailureReason("paid claim evidence failed"): 1}, []

        def github_report(_repo: str, _token: str, _title: str, body: str) -> bool:
            reports.append(body)
            return True

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
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
                run.RunConfig(
                    "tok",
                    "me/repo",
                    None,
                    None,
                    None,
                    github_reports_enabled=True,
                ),
                deps,
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.delivered)
        self.assertEqual(result.coverage.verification_failures, 1)
        self.assertIsNotNone(result.coverage.warning)
        maintain.assert_not_called()
        save.assert_not_called()
        self.assertIn("Opportunity discovery/verification coverage is incomplete", reports[0])
        self.assertIn("Verification coverage incomplete; state was not updated.", buf.getvalue())

    def test_paid_prefetch_failure_blocks_state_without_duplicate_search(self) -> None:
        reports: list[str] = []

        def prefetch(_token: str | None) -> tuple[list[SearchBatch], list[SearchBatch]]:
            return [("paid-q", {})], []

        def strategic(
            _token: str | None,
            _seen: set[str],
            _paid_urls: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.StrategicDiscoveryResult:
            return [candidate(paid=False)], {}, [], []

        def github_report(_repo: str, _token: str, _title: str, body: str) -> bool:
            reports.append(body)
            return True

        deps = run.RunDependencies(
            discover_paid=scout.discover_paid,
            discover_strategic=strategic,
            prefetch_discovery_searches=prefetch,
            append_audit=append_audit,
            send_telegram=false_telegram,
            send_discord=false_discord,
            send_github_report=github_report,
            issue_lifecycle=open_lifecycle,
        )
        with (
            patch.object(
                scout,
                "platform_paid_refs",
                return_value=sources.PlatformDiscoveryResult(refs={}, failures=()),
            ),
            patch.object(github, "search_github") as search,
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig(
                    "tok",
                    "me/repo",
                    None,
                    None,
                    None,
                    github_reports_enabled=True,
                ),
                deps,
                FIXED_TIME,
            )

        search.assert_not_called()
        self.assertEqual(result.coverage.discovery_failures, 1)
        self.assertFalse(result.coverage.complete)
        self.assertTrue(result.delivery.delivered)
        self.assertFalse(result.state_saved)
        self.assertIn("paid discovery search failed for query: paid-q", reports[0])
        save.assert_not_called()

    def test_paid_direct_search_failure_without_repo_blocks_state_after_delivery(self) -> None:
        def strategic(
            _token: str | None,
            _seen: set[str],
            _paid_urls: set[str],
            _repo_cache: dict[str, RepositoryMetadata],
            _guide_cache: dict[str, str | None],
            _search_results: list[SearchBatch] | None,
        ) -> run.StrategicDiscoveryResult:
            return [candidate(paid=False)], {}, [], []

        def telegram(_token: str, _chat_id: str, _message: str) -> bool:
            return True

        deps = run.RunDependencies(
            discover_paid=scout.discover_paid,
            discover_strategic=strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=telegram,
            send_discord=false_discord,
            send_github_report=false_github,
            issue_lifecycle=open_lifecycle,
        )
        with (
            patch.object(scout, "PAID_DISCOVERY_QUERIES", ["paid-q"]),
            patch.object(github, "search_github", return_value={}) as search,
            patch.object(
                scout,
                "platform_paid_refs",
                return_value=sources.PlatformDiscoveryResult(refs={}, failures=()),
            ),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig("tok", None, "tb", "chat", None),
                deps,
                FIXED_TIME,
            )

        search.assert_called_once()
        self.assertTrue(result.delivery.delivered)
        self.assertEqual(result.coverage.discovery_failures, 1)
        self.assertFalse(result.coverage.complete)
        self.assertFalse(result.state_saved)
        maintain.assert_not_called()
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

        def private_report(_repo: str, _token: str, _title: str, _body: str) -> bool:
            calls.append("private")
            return True

        deps = run.RunDependencies(
            discover_paid=paid,
            discover_strategic=empty_strategic,
            prefetch_discovery_searches=empty_prefetch,
            append_audit=append_audit,
            send_telegram=telegram,
            send_discord=discord,
            send_github_report=github_report,
            issue_lifecycle=open_lifecycle,
            send_private_github_report=private_report,
        )
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "save_seen_state"),
        ):
            result = run.run_combined_scan(
                run.RunConfig(
                    "tok",
                    "me/repo",
                    "tb",
                    "chat",
                    "hook",
                    github_reports_enabled=True,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                deps,
                FIXED_TIME,
            )

        self.assertEqual(calls, ["telegram", "discord", "github", "private"])
        self.assertTrue(result.delivery.attempted)
        self.assertTrue(result.delivery.delivered)

    def test_successful_delivery_save_failure_propagates(self) -> None:
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
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(
                state,
                "save_seen_state",
                side_effect=state.SeenStateSaveError("save failed"),
            ),
        ):
            with self.assertRaisesRegex(run.PostDeliveryStateSaveError, "save failed") as error:
                run.run_combined_scan(
                    run.RunConfig(None, None, "tb", "chat", None),
                    deps,
                    FIXED_TIME,
                )

        self.assertIsInstance(error.exception, state.SeenStateSaveError)

    def test_quiet_maintenance_save_failure_propagates(self) -> None:
        old_url = "https://github.com/example/project/issues/99"
        with (
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([old_url]),
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
            ) as error:
                run.run_combined_scan(
                    run.RunConfig(None, None, None, None, None),
                    dependencies(),
                    FIXED_TIME,
                )

        self.assertNotIsInstance(error.exception, run.PostDeliveryStateSaveError)


if __name__ == "__main__":
    unittest.main()
