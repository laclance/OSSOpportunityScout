from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import opportunity_scout.app as app
from opportunity_scout import delivery, run, state
from opportunity_scout.types import (
    DiscoveryFailureReason,
    GitHubIssue,
    IssueLifecycleStatus,
    RejectionRecord,
    RepositoryMetadata,
    SearchQueryResult,
)
from tests.helpers import candidate

FIXED_TIME = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def paid_candidate(
    _token: str | None,
    _seen: set[str],
    _repo_cache: dict[str, RepositoryMetadata],
    _guide_cache: dict[str, str | None],
    _search_results: list[SearchQueryResult] | None,
) -> run.PaidDiscoveryResult:
    return [candidate()], {}, []


def empty_strategic(
    _token: str | None,
    _seen: set[str],
    _paid_urls: set[str],
    _repo_cache: dict[str, RepositoryMetadata],
    _guide_cache: dict[str, str | None],
    _search_results: list[SearchQueryResult] | None,
) -> run.StrategicDiscoveryResult:
    return [], {}, [], []


def incomplete_strategic(
    _token: str | None,
    _seen: set[str],
    _paid_urls: set[str],
    _repo_cache: dict[str, RepositoryMetadata],
    _guide_cache: dict[str, str | None],
    _search_results: list[SearchQueryResult] | None,
) -> run.StrategicDiscoveryResult:
    return (
        [],
        {},
        [],
        [
            {
                "url": "https://github.com/issues",
                "title": "coverage",
                "reason": DiscoveryFailureReason("scan coverage incomplete"),
            }
        ],
    )


def empty_prefetch(
    _token: str | None,
) -> tuple[list[SearchQueryResult], list[SearchQueryResult]]:
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


def true_github(_repo: str, _token: str, _title: str, _body: str) -> bool:
    return True


def open_lifecycle(_url: str) -> IssueLifecycleStatus:
    return "open"


def dependencies(
    private_sender: run.GitHubReportSender | None,
    *,
    strategic: run.StrategicDiscovery = empty_strategic,
    prefetch: run.DiscoveryPrefetch = empty_prefetch,
) -> run.RunDependencies:
    return run.RunDependencies(
        discover_paid=paid_candidate,
        discover_strategic=strategic,
        prefetch_discovery_searches=prefetch,
        append_audit=append_audit,
        send_telegram=false_telegram,
        send_discord=false_discord,
        send_github_report=false_github,
        issue_lifecycle=open_lifecycle,
        send_private_github_report=private_sender,
    )


class PrivateGitHubRunTests(unittest.TestCase):
    def test_private_delivery_inactive_without_both_private_values(self) -> None:
        calls: list[tuple[str, str]] = []

        def private_report(repo: str, token: str, _title: str, _body: str) -> bool:
            calls.append((repo, token))
            return True

        configs = (
            run.RunConfig("scanner-token", "public/source", None, None, None),
            run.RunConfig(
                "scanner-token",
                "public/source",
                None,
                None,
                None,
                private_github_reports_repository="owner/private-reports",
            ),
        )
        for config in configs:
            with (
                patch.object(state, "load_seen_state", return_value=state.SeenState()),
                patch.object(state, "maintain_seen_state") as maintain,
                patch.object(state, "save_seen_state") as save,
            ):
                result = run.run_combined_scan(
                    config,
                    dependencies(private_report),
                    FIXED_TIME,
                )
            self.assertFalse(result.delivery.attempted)
            self.assertFalse(result.delivery.delivered)
            maintain.assert_not_called()
            save.assert_not_called()

        self.assertEqual(calls, [])

    def test_private_delivery_uses_private_target_and_token_and_advances_state(self) -> None:
        prefetch_tokens: list[str | None] = []
        private_calls: list[tuple[str, str]] = []

        def prefetch(
            token: str | None,
        ) -> tuple[list[SearchQueryResult], list[SearchQueryResult]]:
            prefetch_tokens.append(token)
            return [], []

        def private_report(repo: str, token: str, _title: str, _body: str) -> bool:
            private_calls.append((repo, token))
            return True

        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig(
                    "scanner-token",
                    "public/source",
                    None,
                    None,
                    None,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                dependencies(private_report, prefetch=prefetch),
                FIXED_TIME,
            )

        self.assertEqual(prefetch_tokens, ["scanner-token"])
        self.assertEqual(private_calls, [("owner/private-reports", "report-token")])
        self.assertTrue(result.delivery.attempted)
        self.assertTrue(result.delivery.delivered)
        self.assertTrue(result.state_saved)
        save.assert_called_once()

    def test_failed_private_only_delivery_leaves_state_unchanged(self) -> None:
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig(
                    None,
                    None,
                    None,
                    None,
                    None,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                dependencies(false_github),
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.attempted)
        self.assertFalse(result.delivery.delivered)
        self.assertFalse(result.state_saved)
        maintain.assert_not_called()
        save.assert_not_called()

    def test_missing_private_sender_fails_closed(self) -> None:
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig(
                    None,
                    None,
                    None,
                    None,
                    None,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                dependencies(None),
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.attempted)
        self.assertFalse(result.delivery.delivered)
        maintain.assert_not_called()
        save.assert_not_called()

    def test_incomplete_coverage_blocks_private_delivery_state_commit(self) -> None:
        with (
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(state, "maintain_seen_state") as maintain,
            patch.object(state, "save_seen_state") as save,
        ):
            result = run.run_combined_scan(
                run.RunConfig(
                    None,
                    None,
                    None,
                    None,
                    None,
                    private_github_reports_repository="owner/private-reports",
                    private_github_reports_token="report-token",
                ),
                dependencies(true_github, strategic=incomplete_strategic),
                FIXED_TIME,
            )

        self.assertTrue(result.delivery.attempted)
        self.assertTrue(result.delivery.delivered)
        self.assertIsNotNone(result.coverage.warning)
        self.assertFalse(result.state_saved)
        maintain.assert_not_called()
        save.assert_not_called()

    def test_app_wires_private_reporting_separately_from_scanner_github(self) -> None:
        env = {
            "GITHUB_TOKEN": "scanner-token",
            "GITHUB_REPOSITORY": "public/source",
            "PRIVATE_GITHUB_REPORTS_REPOSITORY": "owner/private-reports",
            "PRIVATE_GITHUB_REPORTS_TOKEN": "report-token",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(run, "run_combined_scan") as combined,
        ):
            app.main(["--config", str(Path(__file__).resolve().parents[1] / "scout.example.toml")])

        config = combined.call_args.args[0]
        deps = combined.call_args.args[1]
        self.assertEqual(config.token, "scanner-token")
        self.assertEqual(config.repo_fullname, "public/source")
        self.assertFalse(config.github_reports_enabled)
        self.assertEqual(config.private_github_reports_repository, "owner/private-reports")
        self.assertEqual(config.private_github_reports_token, "report-token")
        self.assertIs(deps.send_private_github_report, delivery.create_private_github_issue)
