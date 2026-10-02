from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from typing import cast
from unittest.mock import patch

from scripts import close_legacy_scan_reports as cleanup
from tests.helpers import FakeResponse


def raw_issue(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "repository_url": "https://api.github.com/repos/laclance/BountyScout",
        "number": 10,
        "title": "🎯 OSS Opportunity Queue: 8 new verified candidates",
        "body": "### Ranked OSS Opportunity Queue\nbody",
        "state": "open",
        "labels": [{"name": "bounty-alert"}],
        "user": {"login": "github-actions[bot]"},
    }
    item.update(overrides)
    return item


def report_issue(**overrides: object) -> cleanup.ReportIssue:
    issue = cleanup.normalize_issue(raw_issue(**overrides))
    assert issue is not None
    return issue


def response(payload: object) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode())


class ReportIdentificationTests(unittest.TestCase):
    def test_legacy_identity_matrix_and_paid_report_regressions(self) -> None:
        self.assertTrue(
            cleanup.is_confirmed_generated_report(report_issue(), "laclance/BountyScout")
        )
        rejected: list[dict[str, object]] = [
            {"repository_url": "https://api.github.com/repos/acme/Other"},
            {"state": "closed"},
            {"pull_request": {}},
            {"labels": [{"name": "other"}]},
            {"user": {"login": "someone"}},
            {"title": "wrong title"},
            {"body": "wrong body"},
            {
                "title": "🎯 Bounty Alert: 5 New Opportunityies found",
                "body": "### Active Bounty Scan Results",
            },
            {
                "title": "🎯 Bounty Alert: 4 New Opportunityies found",
                "body": "### Active Bounty Scan Results",
            },
            {
                "title": "🎯 Bounty Alert: 3 Clean Paid Opportunityies found",
                "body": "### Clean Paid Bounty Scan Results",
            },
            {
                "title": "🎯 Bounty Alert: 1 Clean Paid Opportunity found",
                "body": "### Clean Paid Bounty Scan Results",
            },
        ]
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                self.assertFalse(
                    cleanup.is_confirmed_generated_report(
                        report_issue(**overrides), "laclance/BountyScout"
                    )
                )

    def test_current_format_requires_title_and_marker(self) -> None:
        valid = report_issue(
            title="📊 SCAN REPORT — OSS Opportunity Queue: 3 verified candidates",
            body="<!-- bountyscout-report: automated; actionable: false -->",
        )
        self.assertTrue(cleanup.is_confirmed_generated_report(valid, "LACLANCE/bountyscout"))
        self.assertFalse(
            cleanup.is_confirmed_generated_report(
                report_issue(title=valid.title, body="plain"), "laclance/BountyScout"
            )
        )
        self.assertFalse(
            cleanup.is_confirmed_generated_report(
                report_issue(title="other", body=valid.body), "laclance/BountyScout"
            )
        )

    def test_malformed_payloads_fail_closed(self) -> None:
        payloads: tuple[object, ...] = (
            None,
            [],
            raw_issue(repository_url=None),
            raw_issue(number=True),
            raw_issue(number=0),
            raw_issue(title=None),
            raw_issue(body=None),
            raw_issue(state=None),
            raw_issue(labels=None),
            raw_issue(labels=[1]),
            raw_issue(labels=[{}]),
            raw_issue(user=None),
            raw_issue(user={}),
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(cleanup.normalize_issue(payload))


class TransportTests(unittest.TestCase):
    def test_enumeration_uses_configured_repo_and_paginates(self) -> None:
        first = [raw_issue(number=index + 1) for index in range(100)]
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[response(first), response([raw_issue(number=101)])],
        ) as opened:
            result = cleanup.fetch_open_bounty_alert_issues("laclance/BountyScout", "tok")
        self.assertEqual(len(result.issues), 101)
        first_request = cast(urllib.request.Request, opened.call_args_list[0].args[0])
        second_request = cast(urllib.request.Request, opened.call_args_list[1].args[0])
        self.assertTrue(
            first_request.full_url.startswith(
                "https://api.github.com/repos/laclance/BountyScout/issues?"
            )
        )
        self.assertIn("state=open", first_request.full_url)
        self.assertIn("labels=bounty-alert", first_request.full_url)
        self.assertIn("page=2", second_request.full_url)

    def test_empty_malformed_and_api_failures_are_safe(self) -> None:
        with patch.object(urllib.request, "urlopen", return_value=response([])):
            self.assertEqual(
                cleanup.fetch_open_bounty_alert_issues("laclance/BountyScout", "tok"),
                cleanup.EnumerationResult((), 0),
            )
        with patch.object(
            urllib.request, "urlopen", return_value=response([raw_issue(), {"number": 2}])
        ):
            result = cleanup.fetch_open_bounty_alert_issues("laclance/BountyScout", "tok")
        self.assertEqual((len(result.issues), result.malformed_count), (1, 1))
        with patch.object(urllib.request, "urlopen", return_value=response({"message": "bad"})):
            with self.assertRaises(cleanup.CleanupError):
                cleanup.fetch_open_bounty_alert_issues("laclance/BountyScout", "tok")
        error = urllib.error.HTTPError(
            "https://api.github.com/x", 403, "forbidden", Message(), None
        )
        with patch.object(urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(cleanup.CleanupError):
                cleanup.fetch_open_bounty_alert_issues("laclance/BountyScout", "tok")

    def test_invalid_repository_fails_before_network(self) -> None:
        for repository in ("", "owner", "owner/repo/extra", "owner/repo?x=1"):
            with patch.object(urllib.request, "urlopen") as opened:
                with self.assertRaises(cleanup.CleanupError):
                    cleanup.fetch_open_bounty_alert_issues(repository, "tok")
            opened.assert_not_called()

    def test_close_uses_exact_not_planned_patch(self) -> None:
        with patch.object(
            urllib.request, "urlopen", return_value=response({"state": "closed"})
        ) as opened:
            cleanup.close_report("laclance/BountyScout", "tok", 10)
        request = cast(urllib.request.Request, opened.call_args.args[0])
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(
            json.loads(cast(bytes, request.data).decode()),
            {"state": "closed", "state_reason": "not_planned"},
        )


class CleanupTests(unittest.TestCase):
    def test_dry_run_never_patches_nonmatching_issue(self) -> None:
        enumeration = cleanup.EnumerationResult(
            (report_issue(number=10), report_issue(number=1, title="not a report")), 1
        )
        with (
            patch.object(cleanup, "fetch_open_bounty_alert_issues", return_value=enumeration),
            patch.object(cleanup, "close_report") as close,
        ):
            result = cleanup.cleanup_reports("laclance/BountyScout", "tok", apply=False)
        self.assertEqual([issue.number for issue in result.selected], [10])
        self.assertEqual(result.skipped_count, 2)
        close.assert_not_called()

    def test_partial_failure_is_reported_and_returns_failed_issue(self) -> None:
        enumeration = cleanup.EnumerationResult(
            (report_issue(number=10), report_issue(number=11)), 0
        )
        stderr = io.StringIO()
        with (
            patch.object(cleanup, "fetch_open_bounty_alert_issues", return_value=enumeration),
            patch.object(
                cleanup,
                "close_report",
                side_effect=[None, cleanup.CleanupError("boom")],
            ),
            redirect_stderr(stderr),
        ):
            result = cleanup.cleanup_reports("laclance/BountyScout", "tok", apply=True)
        self.assertEqual(result.closed_numbers, (10,))
        self.assertEqual(result.failed_numbers, (11,))
        self.assertIn("#11", stderr.getvalue())

    def test_second_run_is_idempotent(self) -> None:
        with (
            patch.object(
                cleanup,
                "fetch_open_bounty_alert_issues",
                side_effect=[
                    cleanup.EnumerationResult((report_issue(),), 0),
                    cleanup.EnumerationResult((), 0),
                ],
            ),
            patch.object(cleanup, "close_report") as close,
        ):
            first = cleanup.cleanup_reports("laclance/BountyScout", "tok", apply=True)
            second = cleanup.cleanup_reports("laclance/BountyScout", "tok", apply=True)
        self.assertEqual(first.closed_numbers, (10,))
        self.assertEqual(second.selected, ())
        close.assert_called_once()


class CliTests(unittest.TestCase):
    def test_default_is_dry_run_and_apply_failure_is_nonzero(self) -> None:
        env = {"GITHUB_REPOSITORY": "laclance/BountyScout", "GITHUB_TOKEN": "tok"}
        stdout = io.StringIO()
        dry = cleanup.CleanupResult((report_issue(),), 4, (), ())
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(cleanup, "cleanup_reports", return_value=dry) as run,
            redirect_stdout(stdout),
        ):
            self.assertEqual(cleanup.main([]), 0)
        run.assert_called_once_with("laclance/BountyScout", "tok", apply=False)
        self.assertIn("No mutations made", stdout.getvalue())

        failed = cleanup.CleanupResult((report_issue(),), 4, (), (10,))
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(cleanup, "cleanup_reports", return_value=failed),
        ):
            self.assertEqual(cleanup.main(["--apply"]), 1)

    def test_missing_invalid_or_enumeration_failure_is_nonzero(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            self.assertEqual(cleanup.main([]), 2)
        self.assertIn("required", stderr.getvalue())

        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ, {"GITHUB_REPOSITORY": "invalid", "GITHUB_TOKEN": "tok"}, clear=True
            ),
            patch.object(cleanup, "cleanup_reports") as run,
            redirect_stderr(stderr),
        ):
            self.assertEqual(cleanup.main([]), 1)
        run.assert_not_called()

        stderr = io.StringIO()
        env = {"GITHUB_REPOSITORY": "laclance/BountyScout", "GITHUB_TOKEN": "tok"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                cleanup, "cleanup_reports", side_effect=cleanup.CleanupError("list failed")
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(cleanup.main(["--apply"]), 1)
        self.assertIn("list failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
