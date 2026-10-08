from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from typing import Any, cast
from unittest.mock import patch

from opportunity_scout import sources
from opportunity_scout import github
from opportunity_scout.types import DiscoveryFailureReason, GitHubIssue
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


def github_open_via_urlopen(
    request: urllib.request.Request,
    *,
    timeout: int,
) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


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
            [item.get("html_url") for item in items],
            [i1.get("html_url"), i2.get("html_url"), i3.get("html_url")],
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
        self.assertEqual(
            [item.get("html_url") for item in items], [i1.get("html_url"), i1.get("html_url")]
        )
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

    def test_target_repo_pool_stops_after_short_page(self) -> None:
        i1 = issue(1)
        with patch.object(github, "github_get", return_value=[i1]) as getter:
            items, error = sources.target_repo_issue_pool(
                "example/project",
                "t",
                fetch_per_page=2,
                fetch_pages=4,
                result_limit=5,
            )
        self.assertEqual(items, [i1])
        self.assertIsNone(error)
        getter.assert_called_once()


class GenericSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(github, "_github_open", side_effect=github_open_via_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        with patch.object(
            sources._PLATFORM_OPENER,
            "open",
            return_value=FakeResponse(b"hello"),
        ) as opened:
            success = sources.fetch_text("https://example.test")
        self.assertEqual(success.text, "hello")
        self.assertIsNone(success.failure)
        request = opened.call_args.args[0]
        self.assertEqual(request.headers["User-agent"], "OSSOpportunityScout")
        self.assertEqual(opened.call_args.kwargs["timeout"], 12)

        with patch.object(sources._PLATFORM_OPENER, "open", side_effect=OSError("boom")):
            failure = sources.fetch_text("https://example.test")
        self.assertEqual(failure.text, "")
        self.assertEqual(failure.failure, "boom")

    def test_public_text_fetch_counts_success_and_failure_attempts(self) -> None:
        before = sources.platform_request_count_snapshot()
        with patch.object(
            sources._PLATFORM_OPENER,
            "open",
            return_value=FakeResponse(b"hello"),
        ):
            sources.fetch_text("https://example.test/success")
        with patch.object(sources._PLATFORM_OPENER, "open", side_effect=OSError("boom")):
            sources.fetch_text("https://example.test/failure")
        self.assertEqual(sources.platform_request_count_snapshot(), before + 2)

    def test_platform_redirect_handler_allows_same_origin_https(self) -> None:
        handler = sources._SameOriginHTTPSRedirectHandler()
        request = urllib.request.Request(
            "https://app.opire.dev/home",
            headers={"User-Agent": "OSSOpportunityScout"},
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://APP.OPIRE.DEV:443/home/",
        )

        self.assertIsNotNone(redirected)
        assert redirected is not None
        self.assertEqual(redirected.full_url, "https://APP.OPIRE.DEV:443/home/")
        self.assertEqual(redirected.headers["User-agent"], "OSSOpportunityScout")

    def test_platform_redirect_handler_rejects_untrusted_origins(self) -> None:
        handler = sources._SameOriginHTTPSRedirectHandler()
        request = urllib.request.Request("https://app.opire.dev/home")
        blocked_targets = (
            "https://evil.example/capture",
            "http://app.opire.dev/home",
            "https://app.opire.dev:444/home",
            "https://app.opire.dev:not-a-port/home",
            "https://app.opire.dev.evil.test/home",
            "https://user@app.opire.dev/home",
        )

        for target in blocked_targets:
            with self.subTest(target=target):
                with self.assertRaises(urllib.error.HTTPError):
                    handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_public_text_fetch_returns_failure_when_redirect_policy_rejects(self) -> None:
        handler = sources._SameOriginHTTPSRedirectHandler()

        def redirecting_open(
            request: urllib.request.Request,
            *,
            timeout: int,
        ) -> Any:
            self.assertEqual(timeout, 12)
            return handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://evil.example/capture",
            )

        with patch.object(sources._PLATFORM_OPENER, "open", side_effect=redirecting_open):
            result = sources.fetch_text("https://app.opire.dev/home")

        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.failure)
        self.assertIn("blocked platform redirect", str(result.failure))

    def test_issuehunt_fetch_failure_records_discovery_failure(self) -> None:
        with patch.object(sources._PLATFORM_OPENER, "open", side_effect=OSError("boom")):
            result = sources.issuehunt_platform_refs(pages=1)

        self.assertEqual(result.refs, {})
        self.assertEqual(len(result.failures), 1)
        self.assertIsInstance(result.failures[0], DiscoveryFailureReason)
        self.assertIn("IssueHunt", result.failures[0])

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
        result = sources.issuehunt_platform_refs(
            lambda url: sources.TextFetchResult(pages.get(url, "")),
            pages=2,
        )
        self.assertEqual(
            result.refs["https://github.com/apache/superset/issues/3821"],
            "confirmed bounty platform feed (IssueHunt): $17.00",
        )
        self.assertEqual(
            result.refs["https://github.com/acme/widget/issues/9"],
            "confirmed bounty platform feed (IssueHunt)",
        )
        self.assertEqual(result.failures, ())

        empty = sources.issuehunt_platform_refs(
            lambda _: sources.TextFetchResult(""),
            pages=2,
        )
        self.assertEqual(empty.refs, {})
        self.assertEqual(empty.failures, ())

    def test_issuehunt_retains_good_page_when_later_page_fetch_fails(self) -> None:
        def fetcher(url: str) -> sources.TextFetchResult:
            if url.endswith("?page=2"):
                return sources.TextFetchResult("", failure="boom")
            return sources.TextFetchResult(
                '<a href="/r/acme/widget/issues/9">x</a><span>$50</span>'
            )

        result = sources.issuehunt_platform_refs(fetcher, pages=2)

        self.assertIn("https://github.com/acme/widget/issues/9", result.refs)
        self.assertEqual(len(result.failures), 1)
        self.assertIsInstance(result.failures[0], DiscoveryFailureReason)

    def test_opire_direct_and_detail_sources(self) -> None:
        pages = {
            "https://app.opire.dev/home": (
                r"https:\/\/github.com\/direct\/repo\/issues\/1 "
                '<a href="/issues/A">a</a><a href="/issues/B">b</a>'
                '<a href="/issues/C">c</a><a href="/issues/D">d</a>'
            ),
            "https://app.opire.dev/issues/A": "",
            "https://app.opire.dev/issues/B": (
                "$50 bounty https://github.com/acme/widget/issues/2"
            ),
            "https://app.opire.dev/issues/C": ("funded https://github.com/acme/widget/issues/3"),
            "https://app.opire.dev/issues/D": "no github source here",
        }
        result = sources.opire_platform_refs(
            lambda url: sources.TextFetchResult(pages.get(url, "")),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertIn("https://github.com/direct/repo/issues/1", result.refs)
        self.assertEqual(
            result.refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (Opire): $50",
        )
        self.assertEqual(
            result.refs["https://github.com/acme/widget/issues/3"],
            "confirmed bounty platform feed (Opire)",
        )
        self.assertEqual(result.failures, ())

        empty = sources.opire_platform_refs(
            lambda _: sources.TextFetchResult(""),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertEqual(empty.refs, {})
        self.assertEqual(empty.failures, ())

    def test_opire_deduplicates_and_bounds_detail_requests(self) -> None:
        calls: list[str] = []
        pages = {
            "https://app.opire.dev/home": (
                '<a href="/issues/A">a</a><a href="/issues/A">dup</a>'
                '<a href="/issues/B">b</a><a href="/issues/C">c</a>'
            ),
            "https://app.opire.dev/issues/A": "https://github.com/acme/widget/issues/1",
            "https://app.opire.dev/issues/B": "https://github.com/acme/widget/issues/2",
            "https://app.opire.dev/issues/C": "https://github.com/acme/widget/issues/3",
        }

        def fetcher(url: str) -> sources.TextFetchResult:
            calls.append(url)
            return sources.TextFetchResult(pages[url])

        result = sources.opire_platform_refs(fetcher, fetch_limit=2, network_workers=2)

        self.assertEqual(
            set(result.refs),
            {
                "https://github.com/acme/widget/issues/1",
                "https://github.com/acme/widget/issues/2",
            },
        )
        self.assertEqual(calls.count("https://app.opire.dev/issues/A"), 1)
        self.assertEqual(calls.count("https://app.opire.dev/issues/B"), 1)
        self.assertNotIn("https://app.opire.dev/issues/C", calls)

    def test_opire_worker_count_does_not_change_results(self) -> None:
        pages = {
            "https://app.opire.dev/home": '<a href="/issues/A">a</a><a href="/issues/B">b</a>',
            "https://app.opire.dev/issues/A": "$50 bounty https://github.com/acme/a/issues/1",
            "https://app.opire.dev/issues/B": "https://github.com/acme/b/issues/2",
        }

        def discover(workers: int) -> sources.PlatformDiscoveryResult:
            return sources.opire_platform_refs(
                lambda url: sources.TextFetchResult(pages[url]),
                fetch_limit=2,
                network_workers=workers,
            )

        self.assertEqual(discover(1), discover(3))

    def test_opire_listing_and_detail_failures_are_semantic(self) -> None:
        listing_failure = sources.opire_platform_refs(
            lambda _: sources.TextFetchResult("", failure="listing failed"),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertEqual(listing_failure.refs, {})
        self.assertEqual(len(listing_failure.failures), 1)

        pages = {
            "https://app.opire.dev/home": (
                r"https:\/\/github.com\/direct\/repo\/issues\/1 "
                '<a href="/issues/A">a</a><a href="/issues/B">b</a>'
            ),
            "https://app.opire.dev/issues/A": "https://github.com/acme/widget/issues/2",
        }

        def fetcher(url: str) -> sources.TextFetchResult:
            if url.endswith("/issues/B"):
                return sources.TextFetchResult("", failure="detail failed")
            return sources.TextFetchResult(pages.get(url, ""))

        partial = sources.opire_platform_refs(
            fetcher,
            fetch_limit=20,
            network_workers=2,
        )
        self.assertEqual(
            set(partial.refs),
            {
                "https://github.com/direct/repo/issues/1",
                "https://github.com/acme/widget/issues/2",
            },
        )
        self.assertEqual(len(partial.failures), 1)
        self.assertIsInstance(partial.failures[0], DiscoveryFailureReason)

    def test_bountyhub_direct_detail_and_amount(self) -> None:
        pages = {
            "https://www.bountyhub.dev/en/bounties": (
                r"https:\/\/github.com\/direct\/repo\/issues\/1 "
                '<a href="/en/bounty/view/A">a</a><a href="/en/bounty/view/B">b</a>'
                '<a href="/en/bounty/view/C">c</a><a href="/en/bounty/view/D">d</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": "",
            "https://www.bountyhub.dev/en/bounty/view/B": (
                "Reward $125 https://github.com/acme/widget/issues/2"
            ),
            "https://www.bountyhub.dev/en/bounty/view/C": (
                "funded https://github.com/acme/widget/issues/3"
            ),
            "https://www.bountyhub.dev/en/bounty/view/D": "no github source here",
        }
        amount_pattern = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"
        result = sources.bountyhub_platform_refs(
            amount_pattern,
            lambda url: sources.TextFetchResult(pages.get(url, "")),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertIn("https://github.com/direct/repo/issues/1", result.refs)
        self.assertEqual(
            result.refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (BountyHub): $125",
        )
        self.assertEqual(
            result.refs["https://github.com/acme/widget/issues/3"],
            "confirmed bounty platform feed (BountyHub)",
        )
        self.assertEqual(result.failures, ())

    def test_bountyhub_direct_only_and_detail_only_listings(self) -> None:
        amount_pattern = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"
        direct = sources.bountyhub_platform_refs(
            amount_pattern,
            lambda _: sources.TextFetchResult(r"https:\/\/github.com\/direct\/repo\/issues\/1"),
        )
        self.assertEqual(
            direct.refs,
            {
                "https://github.com/direct/repo/issues/1": (
                    "confirmed bounty platform feed (BountyHub)"
                )
            },
        )
        self.assertEqual(direct.failures, ())

        pages = {
            "https://www.bountyhub.dev/en/bounties": ('<a href="/en/bounty/view/A">bounty</a>'),
            "https://www.bountyhub.dev/en/bounty/view/A": (
                "Reward $125 https://github.com/acme/widget/issues/2"
            ),
        }
        detail = sources.bountyhub_platform_refs(
            amount_pattern,
            lambda url: sources.TextFetchResult(pages[url]),
        )
        self.assertEqual(
            detail.refs,
            {
                "https://github.com/acme/widget/issues/2": (
                    "confirmed bounty platform feed (BountyHub): $125"
                )
            },
        )
        self.assertEqual(detail.failures, ())

    def test_bountyhub_slugged_detail_links_are_fetched_once(self) -> None:
        # Current public BountyHub links include both a UUID and a readable slug.
        detail_path = (
            "/en/bounty/view/521afa31-6c6f-4d2c-becc-c6b14318d2b4/bounty-rcs-support-14999dollar"
        )
        detail_url = "https://www.bountyhub.dev" + detail_path
        requests: list[str] = []

        def fetcher(url: str) -> sources.TextFetchResult:
            requests.append(url)
            if url == "https://www.bountyhub.dev/en/bounties":
                return sources.TextFetchResult(
                    f'<a href="{detail_path}">View</a>'
                    f'<a href="{detail_path}">Duplicate</a>'
                    '<a href="/en/bounty/claim/new/other">Claim</a>'
                )
            if url == detail_url:
                return sources.TextFetchResult(
                    "Reward $14,999.00 https://github.com/microg/GmsCore/issues/2994"
                )
            return sources.TextFetchResult("", failure="unexpected detail URL")

        result = sources.bountyhub_platform_refs(r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?", fetcher)

        self.assertEqual(
            result.refs,
            {
                "https://github.com/microg/GmsCore/issues/2994": (
                    "confirmed bounty platform feed (BountyHub): $14,999.00"
                )
            },
        )
        self.assertEqual(result.failures, ())
        self.assertEqual(requests.count(detail_url), 1)

    def test_bountyhub_api_paginates_and_filters_inactive_rows(self) -> None:
        listing_url = "https://www.bountyhub.dev/en/bounties"
        api_url = "https://api.bountyhub.dev/api/bounties"
        calls: list[str] = []

        def row(number: int, **overrides: Any) -> dict[str, Any]:
            item: dict[str, Any] = {
                "htmlURL": f"https://github.com/example/repo/issues/{number}",
                "issueState": "open",
                "solved": False,
                "retracted": False,
                "totalAmount": "125.00",
            }
            item.update(overrides)
            return item

        pages = {
            f"{api_url}?page=1&limit=6": {
                "data": [row(1), row(2, totalAmount="250.25")],
                "hasNextPage": True,
            },
            f"{api_url}?page=2&limit=6": {
                "data": [
                    row(3, totalAmount="30"),
                    row(4, issueState="closed"),
                    row(5, solved=True),
                    row(6, retracted=True),
                ],
                "hasNextPage": False,
            },
        }

        def fetcher(url: str) -> sources.TextFetchResult:
            calls.append(url)
            if url == listing_url:
                return sources.TextFetchResult("<main>Available Bounties</main>")
            return sources.TextFetchResult(json.dumps(pages[url]))

        result = sources.bountyhub_platform_refs(
            r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?",
            fetcher,
            fetch_limit=6,
        )
        self.assertEqual(
            result.refs,
            {
                "https://github.com/example/repo/issues/1": (
                    "confirmed bounty platform feed (BountyHub): $125.00"
                ),
                "https://github.com/example/repo/issues/2": (
                    "confirmed bounty platform feed (BountyHub): $250.25"
                ),
                "https://github.com/example/repo/issues/3": (
                    "confirmed bounty platform feed (BountyHub): $30"
                ),
            },
        )
        self.assertEqual(result.failures, ())
        self.assertEqual(calls, [listing_url, *pages])

    def test_bountyhub_api_survives_html_transport_failure(self) -> None:
        def fetcher(url: str) -> sources.TextFetchResult:
            if url.endswith("/en/bounties"):
                return sources.TextFetchResult("", failure="HTML fetch unavailable")
            return sources.TextFetchResult(
                json.dumps(
                    {
                        "data": [
                            {
                                "htmlURL": "https://github.com/example/repo/issues/1",
                                "issueState": "open",
                                "solved": False,
                                "retracted": False,
                                "totalAmount": "10.00",
                            }
                        ],
                        "hasNextPage": False,
                    }
                )
            )

        result = sources.bountyhub_platform_refs("amount", fetcher)
        self.assertEqual(len(result.refs), 1)
        self.assertEqual(result.failures, ())

    def test_bountyhub_api_invalid_envelopes_fail_closed(self) -> None:
        bad_responses = [
            sources.TextFetchResult("", failure="upstream error"),
            sources.TextFetchResult("not JSON"),
            sources.TextFetchResult(cast(str, None)),
            sources.TextFetchResult(json.dumps([])),
            sources.TextFetchResult(json.dumps({"data": None, "hasNextPage": False})),
            sources.TextFetchResult(json.dumps({"data": [], "hasNextPage": "false"})),
            sources.TextFetchResult(
                json.dumps({"data": [{}, {}], "hasNextPage": False})
            ),
            sources.TextFetchResult(json.dumps({"data": [], "hasNextPage": True})),
        ]
        for response in bad_responses:
            with self.subTest(response=response):
                result = sources.bountyhub_platform_refs(
                    "amount",
                    lambda url: (
                        sources.TextFetchResult("<main>JS shell</main>")
                        if url.endswith("/en/bounties")
                        else response
                    ),
                    fetch_limit=1,
                )
                self.assertEqual(result.refs, {})
                self.assertEqual(len(result.failures), 1)

    def test_bountyhub_api_partial_bad_rows_preserve_failure(self) -> None:
        valid = {
            "htmlURL": "https://github.com/example/repo/issues/1",
            "issueState": "open",
            "solved": False,
            "retracted": False,
            "totalAmount": "75.00",
        }
        variants: list[Any] = [
            None,
            {},
            {**valid, "issueState": "unknown"},
            {**valid, "solved": "false"},
            {**valid, "retracted": None},
            {**valid, "htmlURL": None},
            {**valid, "htmlURL": "https://untrusted.example/issues/9"},
            {**valid, "totalAmount": None},
            {**valid, "totalAmount": "1.234"},
        ]
        response = json.dumps({"data": [valid, *variants], "hasNextPage": False})
        result = sources.bountyhub_platform_refs(
            "amount",
            lambda url: sources.TextFetchResult(
                "<main>JS shell</main>" if url.endswith("/en/bounties") else response
            ),
            fetch_limit=10,
        )
        self.assertEqual(
            result.refs,
            {
                "https://github.com/example/repo/issues/1": (
                    "confirmed bounty platform feed (BountyHub): $75.00"
                )
            },
        )
        self.assertEqual(len(result.failures), 1)

    def test_bountyhub_api_partial_second_page_failure(self) -> None:
        calls: list[str] = []
        first = json.dumps(
            {
                "data": [
                    {
                        "htmlURL": "https://github.com/example/repo/issues/1",
                        "issueState": "open",
                        "solved": False,
                        "retracted": False,
                        "totalAmount": "55.00",
                    }
                ],
                "hasNextPage": True,
            }
        )

        def fetcher(url: str) -> sources.TextFetchResult:
            calls.append(url)
            if url.endswith("/en/bounties"):
                return sources.TextFetchResult("")
            if "page=1&" in url:
                return sources.TextFetchResult(first)
            return sources.TextFetchResult("", failure="page 2 failed")

        result = sources.bountyhub_platform_refs("amount", fetcher)
        self.assertEqual(len(result.refs), 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(calls), 3)

    def test_bountyhub_api_page_cap_is_incomplete(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> sources.TextFetchResult:
            calls.append(url)
            if url.endswith("/en/bounties"):
                return sources.TextFetchResult("")
            page_number = len(calls) - 1
            return sources.TextFetchResult(
                json.dumps(
                    {
                        "data": [
                            {
                                "htmlURL": (
                                    f"https://github.com/example/repo/issues/{page_number}"
                                ),
                                "issueState": "open",
                                "solved": False,
                                "retracted": False,
                                "totalAmount": "10.00",
                            }
                        ],
                        "hasNextPage": True,
                    }
                )
            )

        result = sources.bountyhub_platform_refs("amount", fetcher)
        self.assertEqual(len(result.refs), 5)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(calls), 6)

    def test_bountyhub_empty_or_unrecognized_listing_is_incomplete(self) -> None:
        amount_pattern = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"
        # No verified BountyHub empty-state contract exists for any of these responses.
        for html in (
            "",
            "<!doctype html><html><div id='app'></div></html>",
            "<main>No bounties found</main>",
        ):
            with self.subTest(html=html):
                result = sources.bountyhub_platform_refs(
                    amount_pattern,
                    lambda _: sources.TextFetchResult(html),
                )
                self.assertEqual(result.refs, {})
                self.assertEqual(len(result.failures), 1)
                self.assertIsInstance(result.failures[0], DiscoveryFailureReason)
                self.assertIn("BountyHub", result.failures[0])
                self.assertIn("scan coverage incomplete", result.failures[0])

    def test_bountyhub_listing_and_detail_failures_are_semantic(self) -> None:
        amount_pattern = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"
        listing_failure = sources.bountyhub_platform_refs(
            amount_pattern,
            lambda _: sources.TextFetchResult("", failure="listing failed"),
            fetch_limit=20,
            network_workers=2,
        )
        self.assertEqual(listing_failure.refs, {})
        self.assertEqual(len(listing_failure.failures), 1)

        pages = {
            "https://www.bountyhub.dev/en/bounties": (
                r"https:\/\/github.com\/direct\/repo\/issues\/1 "
                '<a href="/en/bounty/view/A">a</a><a href="/en/bounty/view/B">b</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": (
                "Reward $125 https://github.com/acme/widget/issues/2"
            ),
        }

        def fetcher(url: str) -> sources.TextFetchResult:
            if url.endswith("/en/bounty/view/B"):
                return sources.TextFetchResult("", failure="detail failed")
            return sources.TextFetchResult(pages.get(url, ""))

        partial = sources.bountyhub_platform_refs(
            amount_pattern,
            fetcher,
            fetch_limit=20,
            network_workers=2,
        )
        self.assertEqual(
            set(partial.refs),
            {
                "https://github.com/direct/repo/issues/1",
                "https://github.com/acme/widget/issues/2",
            },
        )
        self.assertEqual(len(partial.failures), 1)
        self.assertIsInstance(partial.failures[0], DiscoveryFailureReason)

    def test_platform_merge_isolates_one_loader_failure(self) -> None:
        def broken() -> sources.PlatformDiscoveryResult:
            raise RuntimeError("parser broke")

        result = sources.platform_paid_refs(
            (
                (
                    "IssueHunt",
                    lambda: sources.PlatformDiscoveryResult(
                        refs={"a": "issuehunt"},
                        failures=(),
                    ),
                ),
                ("Opire", broken),
                (
                    "BountyHub",
                    lambda: sources.PlatformDiscoveryResult(
                        refs={"b": "bountyhub"},
                        failures=(),
                    ),
                ),
            ),
            network_workers=3,
        )
        self.assertEqual(result.refs, {"a": "issuehunt", "b": "bountyhub"})
        self.assertEqual(len(result.failures), 1)
        self.assertIsInstance(result.failures[0], DiscoveryFailureReason)
        self.assertIn("Opire", result.failures[0])

    def test_platform_merge_is_deduplicated_with_later_loader_precedence(self) -> None:
        result = sources.platform_paid_refs(
            (
                (
                    "IssueHunt",
                    lambda: sources.PlatformDiscoveryResult(
                        refs={"u": "issuehunt"},
                        failures=(),
                    ),
                ),
                (
                    "Opire",
                    lambda: sources.PlatformDiscoveryResult(
                        refs={"v": "opire"},
                        failures=(),
                    ),
                ),
                (
                    "BountyHub",
                    lambda: sources.PlatformDiscoveryResult(
                        refs={"u": "bountyhub"},
                        failures=(),
                    ),
                ),
            ),
            network_workers=3,
        )
        self.assertEqual(result.refs, {"u": "bountyhub", "v": "opire"})
        self.assertEqual(result.failures, ())
        self.assertEqual(
            sources.platform_paid_refs((), network_workers=3),
            sources.PlatformDiscoveryResult(refs={}, failures=()),
        )


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
        selected_urls = {item.get("html_url") for rows in selected.values() for item in rows}
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
            [item.get("html_url") for item in selected["example/project"]],
            [issue(1).get("html_url")],
        )

    def test_equal_preview_scores_preserve_stable_inspection_order(self) -> None:
        selected = sources.strategic_inspection_items(
            [
                (90, 90, 0, issue(1)),
                (90, 90, 0, issue(2)),
                (90, 90, 0, issue(3)),
            ],
            base_per_repo=1,
            adaptive_budget=1,
            should_expand=lambda _: True,
        )
        self.assertEqual(
            [item.get("html_url") for item in selected["example/project"]],
            [issue(1).get("html_url"), issue(2).get("html_url")],
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
    def test_candidate_rank_key_orders_priority_career_cash_then_fewer_comments(self) -> None:
        ranked = [
            candidate(
                title="priority",
                priority_score=91,
                career_score=1,
                cash_score=1,
                comments=99,
            ),
            candidate(
                title="career",
                priority_score=90,
                career_score=91,
                cash_score=1,
                comments=99,
            ),
            candidate(
                title="cash",
                priority_score=90,
                career_score=90,
                cash_score=91,
                comments=99,
            ),
            candidate(
                title="comments",
                priority_score=90,
                career_score=90,
                cash_score=90,
                comments=1,
            ),
            candidate(
                title="later equal",
                priority_score=90,
                career_score=90,
                cash_score=90,
                comments=1,
            ),
        ]

        ordered = sorted(ranked, key=sources.candidate_rank_key, reverse=True)

        self.assertEqual(
            [item["title"] for item in ordered],
            ["priority", "career", "cash", "comments", "later equal"],
        )
        self.assertEqual(sources.candidate_rank_key(ranked[3]), (90, 90, 90, -1))
        self.assertEqual(
            sources.candidate_rank_key(ranked[3]),
            sources.candidate_rank_key(ranked[4]),
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
