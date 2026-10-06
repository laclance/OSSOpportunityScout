from __future__ import annotations

import io
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest.mock import patch

from opportunity_scout import app, github, sources
from tests.helpers import FakeResponse


class GitHubRequestStatsTests(unittest.TestCase):
    def test_request_stats_classify_each_endpoint_category(self) -> None:
        urls = (
            "https://api.github.com/search/issues?q=bug",
            "https://api.github.com/repos/example/project/issues?per_page=50",
            "https://api.github.com/repos/example/project/issues/1",
            "https://api.github.com/repos/example/project/issues/1/comments?per_page=100",
            "https://api.github.com/repos/example/project/issues/1/timeline?per_page=100",
            "https://api.github.com/repos/example/project/pulls/2",
            "https://api.github.com/repos/example/project",
            "https://api.github.com/repos/example/project/contents/CONTRIBUTING.md",
            "https://api.github.com/rate_limit",
        )
        before = github.request_stats_snapshot()
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"ok": true}'),
        ):
            for url in urls:
                github.github_get(url)

        stats = github.request_stats_delta(before, github.request_stats_snapshot())
        self.assertEqual(
            stats,
            github.GitHubRequestStats(
                search=1,
                issues_list=1,
                issue=1,
                comments=1,
                timeline=1,
                pull=1,
                repository=1,
                contents=1,
                other=1,
            ),
        )
        self.assertEqual(stats.total, len(urls))
        self.assertEqual(
            github.format_request_stats(stats),
            "github_requests=9 search=1 issues_list=1 issue=1 comments=1 "
            "timeline=1 pull=1 repository=1 contents=1 other=1",
        )

    def test_request_stats_count_retry_attempts(self) -> None:
        before = github.request_stats_snapshot()
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    urllib.error.URLError(TimeoutError()),
                    FakeResponse(b'{"ok": true}'),
                ],
            ),
            patch.object(time, "sleep"),
        ):
            self.assertEqual(
                github.github_get("https://api.github.com/repos/example/project/issues/1"),
                {"ok": True},
            )

        stats = github.request_stats_delta(before, github.request_stats_snapshot())
        self.assertEqual(stats.issue, 2)
        self.assertEqual(stats.total, 2)


class PlatformRequestStatsTests(unittest.TestCase):
    def test_fetch_text_counts_platform_request_attempt(self) -> None:
        before = sources.platform_request_count_snapshot()
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b"<html></html>"),
        ):
            result = sources.fetch_text("https://example.test/platform")

        self.assertIsNone(result.failure)
        self.assertEqual(
            sources.platform_request_count_snapshot() - before,
            1,
        )


class PerformancePhaseLogTests(unittest.TestCase):
    def test_paid_discovery_reports_platform_hydration_and_verification_phases(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                app,
                "platform_paid_refs",
                return_value=sources.PlatformDiscoveryResult(refs={}, failures=()),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                app.discover_paid(
                    None,
                    set(),
                    {},
                    {},
                    search_results=[],
                ),
                ([], {}, []),
            )

        text = output.getvalue()
        self.assertIn("phase=paid_platform_discovery", text)
        self.assertIn("platform_requests=0", text)
        self.assertIn("phase=paid_platform_hydration", text)
        self.assertIn("phase=paid_verification", text)
        self.assertIn("github_requests=0", text)


if __name__ == "__main__":
    unittest.main()
