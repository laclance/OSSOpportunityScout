from __future__ import annotations

import json
import time
from collections.abc import Callable
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import patch

from opportunity_scout import app, github, paid_verification, sources
from opportunity_scout.strategic import competition
from tests.helpers import FakeResponse, issue

URL = "https://api.github.com/repos/example/project/issues/42/comments?per_page=100"
NEXT = URL + "&page=2"


def response(payload: object, link: str | None = None) -> FakeResponse:
    result = FakeResponse(json.dumps(payload).encode())
    if link is not None:
        result.headers["Link"] = link
    return result


class PaginationTests(unittest.TestCase):
    def test_single_page_and_relations_without_next(self) -> None:
        for link in (None, f'<{URL}>; rel="last", <{URL}>; rel="first", <{URL}>; rel="prev"'):
            with (
                self.subTest(link=link),
                patch.object(
                    github, "_pagination_open", return_value=response([1], link)
                ) as opened,
            ):
                self.assertEqual(github.github_collection(URL, "tok"), [1])
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.args[0].headers["Authorization"], "Bearer tok")

    def test_multi_page_order_no_deduplication_and_mixed_relations(self) -> None:
        link = f'<{URL}>; rel="first", <{NEXT}>; rel="next", <{NEXT}>; rel="last"'
        with patch.object(
            github, "_pagination_open", side_effect=[response([1, 1], link), response([2])]
        ) as opened:
            self.assertEqual(github.github_collection(URL), [1, 1, 2])
            self.assertEqual([c.args[0].full_url for c in opened.call_args_list], [URL, NEXT])

    def test_unusable_links_fail_without_following(self) -> None:
        links = ["", "bad", f'<{NEXT}>; rel="next", <{NEXT}>; rel="next"']
        for url in (
            "https://evil.example/x?page=2",
            NEXT.replace("https:", "http:"),
            NEXT.replace("api.github.com", "api.github.com:443"),
            NEXT + "#fragment",
            NEXT + "&page=2",
            NEXT.replace("page=2", "page=0"),
            NEXT.replace("page=2", "page=banana"),
            NEXT.replace("page=2", "page=3"),
            NEXT.replace("per_page=100", "per_page=50"),
            NEXT.replace("/comments", "/timeline"),
            NEXT.replace("/repos/example/project", "/repos/other/project"),
            URL,
        ):
            links.append(f'<{url}>; rel="next"')
        for link in links:
            with (
                self.subTest(link=link),
                patch.object(
                    github, "_pagination_open", return_value=response([1], link)
                ) as opened,
            ):
                self.assertIsNone(github.github_collection(URL, "secret"))
                self.assertEqual(opened.call_count, 1)

    def test_cycle_and_repeated_url_discard_partial_data(self) -> None:
        for repeated in (URL, NEXT):
            with (
                self.subTest(repeated=repeated),
                patch.object(
                    github,
                    "_pagination_open",
                    side_effect=[
                        response([1], f'<{NEXT}>; rel="next"'),
                        response([2], f'<{repeated}>; rel="next"'),
                    ],
                ) as opened,
            ):
                self.assertIsNone(github.github_collection(URL))
                self.assertEqual(opened.call_count, 2)

    def test_invalid_initial_url_and_explicit_page(self) -> None:
        with patch.object(github, "_pagination_open") as opened:
            self.assertIsNone(github.github_collection("https://evil.example/x", "secret"))
            opened.assert_not_called()
        with patch.object(github, "_pagination_open", return_value=response([])):
            self.assertEqual(github.github_collection(NEXT), [])

    def test_first_and_later_page_failure(self) -> None:
        payloads: tuple[object, ...] = (None, {}, "bad")
        for payload in payloads:
            for first in (True, False):
                responses = (
                    [response(payload)]
                    if first
                    else [response([1], f'<{NEXT}>; rel="next"'), response(payload)]
                )
                with (
                    self.subTest(payload=payload, first=first),
                    patch.object(github, "_pagination_open", side_effect=responses),
                ):
                    self.assertIsNone(github.github_collection(URL))
        with patch.object(github, "_pagination_open", side_effect=OSError("failed")):
            self.assertIsNone(github.github_collection(URL))

    def test_later_page_retries_use_safe_read_policy(self) -> None:
        for code, headers, delay in (
            (429, {"Retry-After": "2"}, 2.0),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "12"}, 2.0),
            (503, {}, 1.0),
        ):
            error = urllib.error.HTTPError(NEXT, code, "limited", headers, None)  # type: ignore[arg-type]
            with (
                self.subTest(code=code),
                patch.object(
                    github,
                    "_pagination_open",
                    side_effect=[response([1], f'<{NEXT}>; rel="next"'), error, response([2])],
                ) as opened,
                patch.object(time, "sleep") as sleep,
                patch.object(time, "time", return_value=10),
            ):
                self.assertEqual(github.github_collection(URL), [1, 2])
                self.assertEqual(opened.call_count, 3)
                sleep.assert_called_once_with(delay)
        with (
            patch.object(
                github,
                "_pagination_open",
                side_effect=[
                    response([1], f'<{NEXT}>; rel="next"'),
                    TimeoutError(),
                    TimeoutError(),
                    TimeoutError(),
                ],
            ) as opened,
            patch.object(time, "sleep") as sleep,
        ):
            self.assertIsNone(github.github_collection(URL))
            self.assertEqual(opened.call_count, 4)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_page_bound_and_complete_safety_ceiling(self) -> None:
        with patch.object(github, "_pagination_open") as opened:
            self.assertIsNone(github.github_collection(URL, max_pages=0))
            opened.assert_not_called()
        with patch.object(
            github,
            "_pagination_open",
            side_effect=[response([1], f'<{NEXT}>; rel="next"'), response([2], "bad")],
        ) as opened:
            self.assertEqual(github.github_collection(URL, max_pages=2), [1, 2])
            self.assertEqual(opened.call_count, 2)
        with patch.object(github, "_pagination_open", return_value=response([1], "bad")) as opened:
            self.assertEqual(github.github_collection(URL, max_pages=1), [1])
            self.assertEqual(opened.call_count, 1)

        def pages(request: urllib.request.Request, **kwargs: Any) -> FakeResponse:
            _, _, page = github._pagination_identity(request.full_url)
            return response([page], f'<{URL}&page={page + 1}>; rel="next"')

        with patch.object(github, "_pagination_open", side_effect=pages) as opened:
            self.assertIsNone(github.github_collection(URL))
            self.assertEqual(opened.call_count, 1000)

    def test_numeric_repository_alias_requires_verified_identity(self) -> None:
        alias = NEXT.replace("/repos/example/project", "/repositories/123")
        for metadata in ({"id": 123}, {"id": 456}, None):
            with (
                self.subTest(metadata=metadata),
                patch.object(
                    github,
                    "_pagination_open",
                    side_effect=[
                        response([1], f'<{alias}>; rel="next"'),
                        response(metadata),
                        response([2], f'<{alias.replace("page=2", "page=3")}>; rel="next"'),
                        response([3]),
                    ],
                ) as opened,
            ):
                self.assertEqual(
                    github.github_collection(URL), [1, 2, 3] if metadata == {"id": 123} else None
                )
                self.assertEqual(opened.call_count, 4 if metadata == {"id": 123} else 2)
                self.assertEqual(
                    opened.call_args_list[1].args[0].full_url,
                    "https://api.github.com/repos/example/project",
                )
        for url in (
            NEXT.replace("/repos/example/project", "/repositories/123").replace(
                "/comments", "/timeline"
            ),
            NEXT.replace("/repos/example/project", "/repositories/abc"),
        ):
            with patch.object(
                github, "_pagination_open", return_value=response([1], f'<{url}>; rel="next"')
            ):
                self.assertIsNone(github.github_collection(URL))
        with patch.object(
            github,
            "_pagination_open",
            return_value=response([1], '<https://api.github.com/elsewhere?page=2>; rel="next"'),
        ):
            self.assertIsNone(github.github_collection("https://api.github.com/x"))

    def test_redirects_are_refused_before_credentials_forwarded(self) -> None:
        handler = github._NoPaginationRedirect()
        request = urllib.request.Request(NEXT, headers={"Authorization": "Bearer secret"})
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "redirect", {}, "https://evil.example")
        with patch.object(
            urllib.request.OpenerDirector, "open", return_value=response([])
        ) as opened:
            self.assertEqual(github.github_collection(URL), [])
            opened.assert_called_once()
        with patch.object(
            github,
            "_pagination_open",
            side_effect=urllib.error.URLError("pagination redirect refused"),
        ) as opened:
            self.assertIsNone(github.github_collection(URL))
            self.assertEqual(opened.call_count, 1)

    def test_search_ignores_links(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                return_value=response({"items": []}, f'<{NEXT}>; rel="next"'),
            ) as opened,
            patch.object(github, "_pagination_open") as pagination,
        ):
            self.assertEqual(github.search_github("bug"), {"items": []})
            self.assertEqual(opened.call_count, 1)
            pagination.assert_not_called()

    def test_comments_later_claim_and_payment_and_failure(self) -> None:
        comment = {
            "body": "I'm working on this; /bounty $500",
            "user": {"login": "dev"},
            "author_association": "MEMBER",
        }
        with patch.object(
            github,
            "_pagination_open",
            side_effect=[response([], f'<{NEXT}>; rel="next"'), response([comment])],
        ):
            comments, error = github.issue_comments_checked(issue(comments=101), "tok")
            self.assertIsNone(error)
            self.assertEqual(comments, [comment])
            self.assertIsNotNone(app.comment_payment_signal(issue(body=""), "tok", comments))
            self.assertEqual(
                competition.strategic_claim_reason(issue(body=""), comments), "active claim by @dev"
            )
        with patch.object(
            github,
            "_pagination_open",
            side_effect=[response([], f'<{NEXT}>; rel="next"'), response(None)],
        ):
            self.assertEqual(
                github.issue_comments_checked(issue(comments=101), None),
                ([], "could not refresh issue comments"),
            )

    def test_both_timelines_find_later_pr_and_fail_closed(self) -> None:
        timeline_url = URL.replace("/comments", "/timeline")
        event = {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "pull_request": {"url": "pr"},
                    "state": "open",
                    "html_url": "https://github.com/example/project/pull/7",
                    "repository_url": "https://api.github.com/repos/example/project",
                    "title": "Fix project issue",
                    "body": "Fixes #42",
                }
            },
        }
        checkers: tuple[Callable[[], str | None], ...] = (
            lambda: competition.timeline_open_pr_reason(issue(), None),
            lambda: paid_verification.has_existing_implementation_pr("example/project", 42, None),
        )
        for checker in checkers:
            for payload in ([event], None):
                with (
                    self.subTest(payload=payload),
                    patch.object(
                        github,
                        "_pagination_open",
                        side_effect=[
                            response([], f'<{timeline_url}&page=2>; rel="next"'),
                            response(payload),
                        ],
                    ),
                ):
                    self.assertEqual(
                        checker(),
                        "existing open implementation PR: https://github.com/example/project/pull/7"
                        if payload
                        else "could not verify open implementation PR timeline",
                    )

    def test_paid_claim_request_stays_bounded(self) -> None:
        with (
            patch.object(
                urllib.request, "urlopen", return_value=response([], f'<{NEXT}>; rel="next"')
            ) as opened,
            patch.object(github, "_pagination_open") as pagination,
        ):
            self.assertIsNone(
                paid_verification.active_claim_reason("example/project", 42, 101, None)
            )
            self.assertEqual(opened.call_count, 1)
            self.assertIn("per_page=30", opened.call_args.args[0].full_url)
            pagination.assert_not_called()

    def test_curated_discovery_ignores_links_and_keeps_page_and_result_bounds(self) -> None:
        pr = {"pull_request": {"url": "pr"}}
        cases: tuple[tuple[list[object], int, int], ...] = (
            ([pr, pr], 3, 0),
            ([issue(), issue()], 1, 1),
            ([issue()], 1, 1),
        )
        for payload, calls, expected in cases:
            with (
                self.subTest(payload=payload),
                patch.object(
                    urllib.request,
                    "urlopen",
                    return_value=response(payload, f'<{NEXT}>; rel="next"'),
                ) as opened,
                patch.object(github, "_pagination_open") as pagination,
            ):
                items, error = sources.target_repo_issue_pool(
                    "example/project", None, fetch_per_page=2, fetch_pages=3, result_limit=1
                )
                self.assertIsNone(error)
                self.assertEqual(len(items), expected)
                self.assertEqual(opened.call_count, calls)
                pagination.assert_not_called()

    def test_paid_candidate_rejects_incomplete_later_timeline(self) -> None:
        timeline_url = URL.replace("/comments", "/timeline")
        with patch.object(
            github,
            "_pagination_open",
            side_effect=[response([], f'<{timeline_url}&page=2>; rel="next"'), response(None)],
        ):
            reason, signal = paid_verification.candidate_rejection_reason(
                issue(title="Fix bug $500 bounty", body="$500 bounty"), None
            )
            self.assertEqual(reason, "could not verify open implementation PR timeline")
            self.assertIsNotNone(signal)
