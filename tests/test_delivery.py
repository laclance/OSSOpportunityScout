from __future__ import annotations

import json
import urllib.request
from typing import Any, cast
import unittest
from unittest.mock import patch

from opportunity_scout import delivery, github
from tests.helpers import FakeResponse


def request_json(req: urllib.request.Request) -> dict[str, Any]:
    """Decode one JSON request body for transport assertions."""
    assert req.data is not None
    data = cast(bytes, req.data)
    loaded = json.loads(data.decode("utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, Any], loaded)


class DeliveryTests(unittest.TestCase):
    def test_telegram_success_preserves_request(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as opened:
            self.assertTrue(delivery.send_telegram_notification("bot", "chat", "hello"))

        req = cast(urllib.request.Request, opened.call_args.args[0])
        self.assertEqual(req.full_url, "https://api.telegram.org/botbot/sendMessage")
        self.assertEqual(req.method, "POST")
        self.assertEqual(
            request_json(req),
            {
                "chat_id": "chat",
                "text": "hello",
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
        )
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(opened.call_args.kwargs["timeout"], 10)

    def test_telegram_failure_returns_false(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=OSError("telegram failed")):
            self.assertFalse(delivery.send_telegram_notification("bot", "chat", "hello"))

    def test_discord_success_preserves_request(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as opened:
            self.assertTrue(delivery.send_discord_notification("https://hook", "hello"))

        req = cast(urllib.request.Request, opened.call_args.args[0])
        self.assertEqual(req.full_url, "https://hook")
        self.assertEqual(req.method, "POST")
        self.assertEqual(request_json(req), {"content": "hello"})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(opened.call_args.kwargs["timeout"], 10)

    def test_discord_failure_returns_false(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=OSError("discord failed")):
            self.assertFalse(delivery.send_discord_notification("https://hook", "hello"))

    def test_github_report_create_and_close_preserve_requests(self) -> None:
        created_url = "https://api.github.com/repos/me/repo/issues/42"
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[FakeResponse(f'{{"url": "{created_url}"}}'.encode()), FakeResponse()],
        ) as opened:
            self.assertTrue(delivery.create_github_issue("me/repo", "tok", "title", "body"))

        self.assertEqual(opened.call_count, 2)
        create_req = cast(urllib.request.Request, opened.call_args_list[0].args[0])
        close_req = cast(urllib.request.Request, opened.call_args_list[1].args[0])

        self.assertEqual(create_req.full_url, "https://api.github.com/repos/me/repo/issues")
        self.assertEqual(create_req.method, "POST")
        self.assertEqual(
            request_json(create_req),
            {"title": "title", "body": "body", "labels": ["bounty-alert"]},
        )
        self.assertEqual(create_req.get_header("Content-type"), "application/json")
        self.assertEqual(create_req.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(create_req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(create_req.get_header("User-agent"), "OSSOpportunityScout")
        self.assertEqual(create_req.get_header("X-github-api-version"), "2022-11-28")
        self.assertEqual(opened.call_args_list[0].kwargs["timeout"], 15)

        self.assertEqual(close_req.full_url, created_url)
        self.assertEqual(close_req.method, "PATCH")
        self.assertEqual(
            request_json(close_req),
            {"state": "closed", "state_reason": "not_planned"},
        )
        self.assertEqual(close_req.get_header("Content-type"), "application/json")
        self.assertEqual(close_req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(close_req.get_header("User-agent"), "OSSOpportunityScout")
        self.assertEqual(opened.call_args_list[1].kwargs["timeout"], 15)

    def test_github_report_rejects_malformed_create_response(self) -> None:
        for body in (b"not json", b"{}", b"[]", b'{"url": ""}'):
            with self.subTest(body=body):
                with patch.object(
                    urllib.request,
                    "urlopen",
                    return_value=FakeResponse(body),
                ) as opened:
                    self.assertFalse(
                        delivery.create_github_issue("me/repo", "tok", "title", "body")
                    )
                opened.assert_called_once()

    def test_github_report_create_failure_returns_false(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=OSError("create failed"),
        ) as opened:
            self.assertFalse(delivery.create_github_issue("me/repo", "tok", "title", "body"))
        opened.assert_called_once()

    def test_github_report_close_failure_returns_false(self) -> None:
        created = FakeResponse(b'{"url": "https://api.github.com/repos/me/repo/issues/42"}')
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[created, OSError("close failed")],
        ) as opened:
            self.assertFalse(delivery.create_github_issue("me/repo", "tok", "title", "body"))
        self.assertEqual(opened.call_count, 2)

    def test_private_github_report_verifies_privacy_and_leaves_issue_open(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[FakeResponse(b'{"private": true}'), FakeResponse(b"{}")],
        ) as opened:
            self.assertTrue(
                delivery.create_private_github_issue(
                    "owner/private-reports",
                    "report-token",
                    "title",
                    "body",
                )
            )

        self.assertEqual(opened.call_count, 2)
        metadata_req = cast(urllib.request.Request, opened.call_args_list[0].args[0])
        create_req = cast(urllib.request.Request, opened.call_args_list[1].args[0])
        self.assertEqual(
            metadata_req.full_url,
            "https://api.github.com/repos/owner/private-reports",
        )
        self.assertEqual(metadata_req.method, "GET")
        self.assertIsNone(metadata_req.data)
        for request in (metadata_req, create_req):
            self.assertEqual(request.get_header("Accept"), github.GITHUB_ACCEPT)
            self.assertEqual(request.get_header("User-agent"), github.GITHUB_USER_AGENT)
            self.assertEqual(
                request.get_header("X-github-api-version"),
                github.GITHUB_API_VERSION,
            )
            self.assertEqual(request.get_header("Authorization"), "Bearer report-token")
        self.assertEqual(
            create_req.full_url,
            "https://api.github.com/repos/owner/private-reports/issues",
        )
        self.assertEqual(create_req.method, "POST")
        self.assertEqual(request_json(create_req), {"title": "title", "body": "body"})

    def test_private_github_report_fails_closed_for_unverified_privacy(self) -> None:
        for body in (b'{"private": false}', b"{}", b"[]", b"not json"):
            with self.subTest(body=body):
                with patch.object(
                    urllib.request,
                    "urlopen",
                    return_value=FakeResponse(body),
                ) as opened:
                    self.assertFalse(
                        delivery.create_private_github_issue(
                            "owner/reports",
                            "report-token",
                            "title",
                            "body",
                        )
                    )
                opened.assert_called_once()

    def test_private_github_report_metadata_transport_or_auth_failure_fails_closed(
        self,
    ) -> None:
        for error in (OSError("metadata failed"), PermissionError("authentication failed")):
            with self.subTest(error=error):
                with patch.object(urllib.request, "urlopen", side_effect=error) as opened:
                    self.assertFalse(
                        delivery.create_private_github_issue(
                            "owner/reports",
                            "report-token",
                            "title",
                            "body",
                        )
                    )
                opened.assert_called_once()

    def test_private_github_report_issue_create_failure_returns_false(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[FakeResponse(b'{"private": true}'), OSError("create failed")],
        ) as opened:
            self.assertFalse(
                delivery.create_private_github_issue(
                    "owner/reports",
                    "report-token",
                    "title",
                    "body",
                )
            )
        self.assertEqual(opened.call_count, 2)
