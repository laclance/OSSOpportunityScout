from __future__ import annotations

import io
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from threading import Lock
from typing import Any, cast
from unittest.mock import patch

from bountyscout import github
from bountyscout.types import GitHubIssue
from tests.helpers import FakeResponse


def issue(**overrides: Any) -> GitHubIssue:
    item: dict[str, Any] = {
        "html_url": "https://github.com/example/project/issues/42",
        "comments": 1,
    }
    item.update(overrides)
    return cast(GitHubIssue, item)


class GitHubParsingTests(unittest.TestCase):
    def test_issue_repo_and_number_preserves_canonical_url_semantics(self) -> None:
        repo, number = github.issue_repo_and_number(issue())
        self.assertEqual((repo, number), ("example/project", 42))
        self.assertIsInstance(number, int)
        self.assertEqual(
            github.issue_repo_and_number(GitHubIssue(html_url="https://example.com/no")),
            (None, None),
        )
        self.assertEqual(github.issue_repo_and_number(GitHubIssue()), (None, None))

    def test_parse_github_datetime_preserves_github_timestamp_semantics(self) -> None:
        self.assertEqual(
            github.parse_github_datetime("2026-09-30T12:00:00Z"),
            datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            github.parse_github_datetime("2026-09-30T12:00:00+02:00"),
            datetime(2026, 9, 30, 12, 0, tzinfo=timezone(timedelta(hours=2))),
        )
        self.assertIsNone(github.parse_github_datetime(""))
        self.assertIsNone(github.parse_github_datetime(None))
        self.assertIsNone(github.parse_github_datetime("not-a-date"))


class GitHubHttpTests(unittest.TestCase):
    def test_github_get_uses_standard_headers_and_auth(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"ok": true}'),
        ) as opened:
            self.assertEqual(github.github_get("https://api.github.com/x", "tok", 7), {"ok": True})

        request = opened.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer tok")
        self.assertEqual(request.headers["Accept"], "application/vnd.github+json")
        self.assertEqual(request.headers["User-agent"], "OSSOpportunityScout")
        self.assertEqual(request.headers["X-github-api-version"], "2022-11-28")
        self.assertNotIn("Content-type", request.headers)
        self.assertEqual(opened.call_args.kwargs["timeout"], 7)

    def test_github_get_error_logging_can_be_suppressed(self) -> None:
        with (
            patch.object(urllib.request, "urlopen", side_effect=OSError("boom")),
            io.StringIO() as output,
            redirect_stdout(output),
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x"))
            self.assertIn("GitHub API Error", output.getvalue())

        with (
            patch.object(urllib.request, "urlopen", side_effect=OSError("boom")),
            io.StringIO() as output,
            redirect_stdout(output),
        ):
            self.assertIsNone(
                github.github_get(
                    "https://api.github.com/x",
                    timeout=10,
                    log_errors=False,
                )
            )
            self.assertEqual(output.getvalue(), "")

    def test_search_github_encodes_forwards_and_fetches_once(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def fetch_json(url: str, token: str | None) -> Any:
            calls.append((url, token))
            return {"items": [issue()]}

        result = github.search_github(
            "a b",
            "tok",
            per_page=7,
            fetch_json=fetch_json,
        )

        self.assertEqual(result, {"items": [issue()]})
        self.assertEqual(len(calls), 1)
        self.assertIn("q=a+b", calls[0][0])
        self.assertIn("per_page=7", calls[0][0])
        self.assertEqual(calls[0][1], "tok")

    def test_search_github_normalizes_non_dict_and_transport_failure(self) -> None:
        with patch.object(github, "github_get", return_value=[]) as getter:
            self.assertEqual(github.search_github("x"), {})
            getter.assert_called_once()

        with patch.object(github, "github_get", return_value=None) as getter:
            self.assertEqual(github.search_github("x"), {})
            getter.assert_called_once()

    def test_issue_lifecycle_returns_open_and_closed_from_direct_issue_endpoint(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[
                FakeResponse(b'{"state": "open"}'),
                FakeResponse(b'{"state": "closed"}'),
            ],
        ) as opened:
            self.assertEqual(
                github.issue_lifecycle(
                    "https://github.com/example/project/issues/42",
                    "tok",
                ).status,
                "open",
            )
            self.assertEqual(
                github.issue_lifecycle(
                    "https://github.com/example/project/issues/43",
                    "tok",
                ).status,
                "closed",
            )

        request = opened.call_args_list[0].args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/example/project/issues/42",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer tok")

    def test_issue_lifecycle_distinguishes_not_found_from_other_http_failures(self) -> None:
        headers = Message()
        not_found = urllib.error.HTTPError(
            "https://api.github.com/x",
            404,
            "not found",
            headers,
            None,
        )
        forbidden = urllib.error.HTTPError(
            "https://api.github.com/x",
            403,
            "forbidden",
            headers,
            None,
        )
        with patch.object(urllib.request, "urlopen", side_effect=[not_found, forbidden]):
            self.assertEqual(
                github.issue_lifecycle(
                    "https://github.com/example/project/issues/42",
                    None,
                ).status,
                "not_found",
            )
            self.assertEqual(
                github.issue_lifecycle(
                    "https://github.com/example/project/issues/43",
                    None,
                ).status,
                "failed",
            )

    def test_issue_lifecycle_transport_and_malformed_payload_fail_closed(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=TimeoutError("timeout")):
            self.assertEqual(
                github.issue_lifecycle(
                    "https://github.com/example/project/issues/42",
                    None,
                ).status,
                "failed",
            )

        malformed_payloads = [
            b"{",
            b"[]",
            b'{"state": "open", "pull_request": {}}',
            b'{"state": "unknown"}',
        ]
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with patch.object(
                    urllib.request,
                    "urlopen",
                    return_value=FakeResponse(payload),
                ):
                    self.assertEqual(
                        github.issue_lifecycle(
                            "https://github.com/example/project/issues/42",
                            None,
                        ).status,
                        "failed",
                    )

    def test_issue_lifecycle_rejects_noncanonical_urls_without_network(self) -> None:
        with patch.object(urllib.request, "urlopen") as opened:
            self.assertEqual(
                github.issue_lifecycle("https://example.com/tasks/42", None).status,
                "failed",
            )
        opened.assert_not_called()


class CacheTests(unittest.TestCase):
    def test_keyed_lock_pool_reuses_only_matching_keys(self) -> None:
        locks = github.KeyedLockPool()
        self.assertIs(locks.lock_for("repo:a"), locks.lock_for("repo:a"))
        self.assertIsNot(locks.lock_for("repo:a"), locks.lock_for("repo:b"))

    def test_cached_value_uses_existing_value_without_loading(self) -> None:
        cache = {"a": 7}
        calls = 0

        def loader() -> int:
            nonlocal calls
            calls += 1
            return 9

        value = github.cached_value(
            cache,
            "a",
            loader,
            github.KeyedLockPool(),
            namespace="repo",
        )
        self.assertEqual(value, 7)
        self.assertEqual(calls, 0)

    def test_cached_value_serializes_same_key_fill(self) -> None:
        cache: dict[str, int] = {}
        calls = 0
        calls_lock = Lock()
        locks = github.KeyedLockPool()

        def loader() -> int:
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.01)
            return 11

        def load(_: int) -> int:
            return github.cached_value(
                cache,
                "a",
                loader,
                locks,
                namespace="repo",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            values = list(executor.map(load, range(4)))

        self.assertEqual(values, [11, 11, 11, 11])
        self.assertEqual(calls, 1)
        self.assertEqual(cache, {"a": 11})


class GitHubResourceTests(unittest.TestCase):
    def test_repo_metadata_requires_dict_shape(self) -> None:
        with patch.object(github, "github_get", return_value={"stargazers_count": 10}):
            self.assertEqual(github.repo_metadata("a/b", "t"), {"stargazers_count": 10})
        with patch.object(github, "github_get", return_value=[]):
            self.assertEqual(github.repo_metadata("a/b", "t"), {})

    def test_issue_comments_short_circuit_and_shape(self) -> None:
        bad = GitHubIssue(html_url="bad", comments=1)
        self.assertEqual(github.issue_comments(bad, "t"), [])
        self.assertEqual(
            github.issue_comments_checked(bad, "t"),
            ([], "could not identify repository/issue number"),
        )
        self.assertEqual(github.issue_comments_checked(issue(comments=0), "t"), ([], None))
        self.assertEqual(github.issue_comments(issue(comments=0), "t"), [])

        with patch.object(github, "github_get", return_value={"bad": "shape"}):
            self.assertEqual(
                github.issue_comments_checked(issue(), "t"),
                ([], "could not refresh issue comments"),
            )
            self.assertEqual(github.issue_comments(issue(), "t"), [])
        with patch.object(github, "github_get", return_value=[{"body": "x"}]) as getter:
            self.assertEqual(
                github.issue_comments_checked(issue(), "t"),
                ([{"body": "x"}], None),
            )
            self.assertEqual(github.issue_comments(issue(), "t"), [{"body": "x"}])
            self.assertIn("/issues/42/comments?per_page=100", getter.call_args.args[0])

    def test_contribution_guide_checks_common_paths(self) -> None:
        calls: list[str] = []

        def getter(url: str, token: str | None) -> Any:
            calls.append(url)
            if "docs/CONTRIBUTING.md" in url:
                return {"html_url": "guide"}
            return None

        self.assertEqual(github.contribution_guide("a/b", "t", getter), "guide")
        self.assertEqual(len(calls), 3)
        self.assertIsNone(github.contribution_guide("a/b", "t", lambda *_: None))

    def test_contribution_guide_default_getter_uses_optional_http_semantics(self) -> None:
        with patch.object(
            github,
            "github_get",
            side_effect=[None, {"html_url": "guide"}],
        ) as getter:
            self.assertEqual(github.contribution_guide("a/b", "t"), "guide")
        self.assertEqual(getter.call_count, 2)
        self.assertEqual(getter.call_args.kwargs["timeout"], 10)
        self.assertFalse(getter.call_args.kwargs["log_errors"])

    def test_issue_from_github_url_validates_and_fetches(self) -> None:
        self.assertIsNone(github.issue_from_github_url("bad", "t"))

        with patch.object(github, "github_get", return_value=[]):
            self.assertIsNone(github.issue_from_github_url("https://github.com/a/b/issues/12", "t"))

        with patch.object(github, "github_get", return_value={"state": "open"}) as getter:
            self.assertEqual(
                github.issue_from_github_url("https://github.com/a/b/issues/12", "t"),
                {"state": "open"},
            )
            self.assertIn("/repos/a/b/issues/12", getter.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
