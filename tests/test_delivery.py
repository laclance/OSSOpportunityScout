from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import redirect_stdout
from email.message import Message
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


def github_open_via_urlopen(
    request: urllib.request.Request,
    *,
    timeout: int,
) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


REAL_GITHUB_MUTATION_OPEN = delivery._github_mutation_open


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        read_patcher = patch.object(github, "_github_open", side_effect=github_open_via_urlopen)
        read_patcher.start()
        self.addCleanup(read_patcher.stop)

        mutation_patcher = patch.object(
            delivery,
            "_github_mutation_open",
            side_effect=github_open_via_urlopen,
        )
        mutation_patcher.start()
        self.addCleanup(mutation_patcher.stop)

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
        payload = request_json(req)
        self.assertEqual(
            payload,
            {
                "chat_id": "chat",
                "text": "hello",
                "disable_web_page_preview": False,
            },
        )
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(opened.call_args.kwargs["timeout"], 10)

    def test_telegram_neutralizes_source_mentions_only_in_outbound_payload(self) -> None:
        message = (
            "🎯 OSS Opportunity Queue (now)\n\n"
            "1. owner/repo #42 — Fix @alice and @bob in parser_[edge]\n"
            "   • strategic OSS | career: 80/100 | priority: 93/100\n"
            "   • https://github.com/owner/repo/issues/42"
        )
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as opened:
            self.assertTrue(delivery.send_telegram_notification("bot", "chat", message))

        req = cast(urllib.request.Request, opened.call_args.args[0])
        self.assertEqual(req.full_url, "https://api.telegram.org/botbot/sendMessage")
        self.assertEqual(
            request_json(req),
            {
                "chat_id": "chat",
                "text": message.replace("@", "@\u200b"),
                "disable_web_page_preview": False,
            },
        )
        self.assertNotIn("parse_mode", request_json(req))
        outbound_text = request_json(req)["text"]
        self.assertIn("parser_[edge]", outbound_text)
        self.assertIn("   • strategic OSS | career: 80/100", outbound_text)
        self.assertNotIn("@alice", outbound_text)
        self.assertNotIn("@bob", outbound_text)
        self.assertEqual(message.count("@"), 2)

    def test_telegram_mention_neutralization_stays_within_message_limit(self) -> None:
        message = "@" * 1900
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as opened:
            self.assertTrue(delivery.send_telegram_notification("bot", "chat", message))
        req = cast(urllib.request.Request, opened.call_args.args[0])
        outbound_text = request_json(req)["text"]
        self.assertEqual(outbound_text, "@\u200b" * 1900)
        self.assertLessEqual(len(outbound_text), 4096)

    def test_telegram_failure_returns_false(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=OSError("telegram failed")):
            self.assertFalse(delivery.send_telegram_notification("bot", "chat", "hello"))

    def test_discord_success_disables_mentions_and_preserves_request(self) -> None:
        message = "@everyone @here <@123> <@&456>"
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as opened:
            self.assertTrue(delivery.send_discord_notification("https://hook", message))

        req = cast(urllib.request.Request, opened.call_args.args[0])
        self.assertEqual(req.full_url, "https://hook")
        self.assertEqual(req.method, "POST")
        self.assertEqual(
            request_json(req),
            {"content": message, "allowed_mentions": {"parse": []}},
        )
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(opened.call_args.kwargs["timeout"], 10)

    def test_discord_failure_returns_false(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=OSError("discord failed")):
            self.assertFalse(delivery.send_discord_notification("https://hook", "hello"))

    def test_notification_failures_do_not_expose_credentials(self) -> None:
        cases: tuple[tuple[Callable[[], bool], str], ...] = (
            (
                lambda: delivery.send_telegram_notification("secret-token", "chat", "hello"),
                "secret-token",
            ),
            (
                lambda: delivery.send_discord_notification(
                    "https://discord.example/hooks/secret-webhook", "hello"
                ),
                "secret-webhook",
            ),
        )
        for send, secret in cases:
            with self.subTest(secret=secret):
                output = io.StringIO()
                with patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=OSError(f"transport failed for {secret}"),
                ):
                    with redirect_stdout(output):
                        self.assertFalse(send())
                self.assertNotIn(secret, output.getvalue())

    def test_github_mutation_open_installs_no_redirect_handler(self) -> None:
        request = urllib.request.Request(
            "https://api.github.com/repos/me/repo/issues",
            data=b"{}",
            headers={"Authorization": "Bearer secret-token"},
            method="POST",
        )
        with patch.object(urllib.request, "build_opener") as build_opener:
            opener = build_opener.return_value
            opener.open.return_value = FakeResponse()
            self.assertIs(
                REAL_GITHUB_MUTATION_OPEN(request, timeout=9),
                opener.open.return_value,
            )

        self.assertIsInstance(
            build_opener.call_args.args[0],
            delivery._NoGitHubMutationRedirect,
        )
        opener.open.assert_called_once_with(request, timeout=9)

    def test_github_mutations_refuse_all_redirects(self) -> None:
        request = urllib.request.Request(
            "https://api.github.com/repos/me/repo/issues",
            data=b"{}",
            headers={"Authorization": "Bearer secret-token"},
            method="POST",
        )
        handler = delivery._NoGitHubMutationRedirect()

        for target in (
            "https://api.github.com/repos/me/repo/issues/42",
            "https://evil.example/capture",
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    urllib.error.URLError,
                    "GitHub mutation redirect refused",
                ):
                    handler.redirect_request(
                        request,
                        None,
                        302,
                        "redirect",
                        {},
                        target,
                    )

    def test_github_mutation_rejects_untrusted_initial_url_without_transport(self) -> None:
        request_spec = delivery._github_request(
            "https://evil.example/capture",
            "secret-token",
            method="POST",
            payload={"title": "x"},
        )
        with patch.object(delivery, "_github_mutation_open") as opened:
            result = delivery._perform_github_mutation(request_spec)

        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure, "untrusted destination")
        opened.assert_not_called()

    def test_github_report_rejects_untrusted_created_issue_url_without_close(self) -> None:
        with patch.object(
            delivery,
            "_github_mutation_open",
            return_value=FakeResponse(b'{"url": "https://evil.example/issues/42"}'),
        ) as opened:
            self.assertFalse(
                delivery.create_github_issue("me/repo", "secret-token", "title", "body")
            )

        opened.assert_called_once()
        create_request = cast(urllib.request.Request, opened.call_args.args[0])
        self.assertEqual(
            create_request.full_url,
            "https://api.github.com/repos/me/repo/issues",
        )
        self.assertEqual(create_request.get_header("Authorization"), "Bearer secret-token")

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
        for body in (b"not json", b"{}", b"[]", b'{"url": ""}', b'{"url": 42}'):
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

    def test_private_github_report_requires_literal_true(self) -> None:
        for body in (b'{"private": 1}', b'{"private": "true"}'):
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

    def test_http_error_reports_status_without_telegram_token(self) -> None:
        secret = "telegram-url-secret"
        error = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{secret}/sendMessage",
            403,
            f"unauthorized {secret}",
            Message(),
            None,
        )
        output = io.StringIO()
        with (
            patch.object(urllib.request, "urlopen", side_effect=error) as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(delivery.send_telegram_notification(secret, "chat", "payload-secret"))
        opened.assert_called_once()
        self.assertEqual(output.getvalue(), "Failed to send Telegram notification (HTTP 403).\n")
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("payload-secret", output.getvalue())

    def test_http_error_reports_status_without_discord_webhook_secret(self) -> None:
        secret = "discord-webhook-secret"
        error = urllib.error.HTTPError(
            f"https://discord.example/api/webhooks/{secret}",
            429,
            f"rate-limited {secret}",
            Message(),
            None,
        )
        output = io.StringIO()
        with (
            patch.object(urllib.request, "urlopen", side_effect=error),
            redirect_stdout(output),
        ):
            self.assertFalse(
                delivery.send_discord_notification(
                    f"https://discord.example/api/webhooks/{secret}", "private-payload"
                )
            )
        self.assertEqual(output.getvalue(), "Failed to send Discord notification (HTTP 429).\n")
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("private-payload", output.getvalue())

    def test_invalid_http_status_does_not_echo_untrusted_status(self) -> None:
        for status in (0, cast(int, "http-status-secret")):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    "https://api.telegram.org/bottoken-secret/sendMessage",
                    status,
                    "server-text-secret",
                    Message(),
                    None,
                )
                output = io.StringIO()
                with (
                    patch.object(urllib.request, "urlopen", side_effect=error),
                    redirect_stdout(output),
                ):
                    self.assertFalse(
                        delivery.send_telegram_notification("token-secret", "chat", "hi")
                    )
                self.assertEqual(
                    output.getvalue(), "Failed to send Telegram notification (HTTP error).\n"
                )

    def test_network_errors_do_not_echo_urls_or_exception_messages(self) -> None:
        for error in (
            urllib.error.URLError("https://api.telegram.org/botnetwork-secret/sendMessage"),
            OSError("https://api.telegram.org/botnetwork-secret/sendMessage"),
        ):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with (
                    patch.object(urllib.request, "urlopen", side_effect=error),
                    redirect_stdout(output),
                ):
                    self.assertFalse(
                        delivery.send_telegram_notification(
                            "network-secret", "chat", "message-secret"
                        )
                    )
                self.assertEqual(
                    output.getvalue(), "Failed to send Telegram notification (network error).\n"
                )
                self.assertNotIn("network-secret", output.getvalue())
                self.assertNotIn("message-secret", output.getvalue())

    def test_unexpected_transport_exception_does_not_leak_content(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=RuntimeError("unexpected transport-secret with payload-secret"),
            ),
            redirect_stdout(output),
        ):
            self.assertFalse(
                delivery.send_discord_notification(
                    "https://discord.example/transport-secret", "text"
                )
            )
        self.assertEqual(
            output.getvalue(), "Failed to send Discord notification (transport error).\n"
        )
        self.assertNotIn("transport-secret", output.getvalue())
        self.assertNotIn("payload-secret", output.getvalue())

    def test_request_construction_failure_is_safe_and_skips_network(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                delivery._JsonRequest, "materialize", side_effect=ValueError("request-secret")
            ),
            patch.object(urllib.request, "urlopen") as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(delivery.send_telegram_notification("request-secret", "chat", "hi"))
        opened.assert_not_called()
        self.assertEqual(
            output.getvalue(), "Failed to send Telegram notification (request error).\n"
        )

    def test_github_create_http_error_does_not_echo_bearer_token(self) -> None:
        secret = "bearer-token-secret"
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/me/repo/issues",
            401,
            f"Bearer {secret} and source-payload-secret",
            Message(),
            None,
        )
        output = io.StringIO()
        with (
            patch.object(delivery, "_github_mutation_open", side_effect=error) as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(delivery.create_github_issue("me/repo", secret, "title", "body"))
        opened.assert_called_once()
        self.assertEqual(
            output.getvalue(), "Failed to create GitHub Issue notification (HTTP 401).\n"
        )
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("source-payload-secret", output.getvalue())

    def test_github_close_network_error_is_failure_without_retry_or_leaks(self) -> None:
        created = FakeResponse(b'{"url": "https://api.github.com/repos/me/repo/issues/42"}')
        output = io.StringIO()
        with (
            patch.object(
                delivery,
                "_github_mutation_open",
                side_effect=[created, urllib.error.URLError("Bearer close-token-secret")],
            ) as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(
                delivery.create_github_issue("me/repo", "close-token-secret", "title", "body")
            )
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(
            output.getvalue(), "Failed to auto-close GitHub Issue notification (network error).\n"
        )
        self.assertNotIn("close-token-secret", output.getvalue())

    def test_private_report_http_failure_does_not_echo_bearer_token(self) -> None:
        secret = "private-report-token-secret"
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/me/private/issues",
            403,
            f"Bearer {secret}",
            Message(),
            None,
        )
        output = io.StringIO()
        with (
            patch.object(github, "github_get", return_value={"private": True}),
            patch.object(delivery, "_github_mutation_open", side_effect=error) as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(
                delivery.create_private_github_issue("me/private", secret, "title", "secret-body")
            )
        opened.assert_called_once()
        self.assertEqual(output.getvalue(), "Failed to create private GitHub report (HTTP 403).\n")
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("secret-body", output.getvalue())

    def test_github_unexpected_failure_uses_generic_category(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                delivery, "_github_mutation_open", side_effect=RuntimeError("Bearer github-secret")
            ) as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(
                delivery.create_github_issue("me/repo", "github-secret", "title", "body")
            )
        opened.assert_called_once()
        self.assertEqual(
            output.getvalue(), "Failed to create GitHub Issue notification (transport error).\n"
        )
        self.assertNotIn("github-secret", output.getvalue())

    def test_github_request_construction_failure_skips_mutation(self) -> None:
        request = delivery._github_request(
            "https://api.github.com/repos/me/repo/issues",
            "github-secret",
            method="POST",
            payload={"title": "hi"},
        )
        with (
            patch.object(delivery._JsonRequest, "materialize", side_effect=ValueError("secret")),
            patch.object(delivery, "_github_mutation_open") as opened,
        ):
            result = delivery._perform_github_mutation(request)
        opened.assert_not_called()
        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure, "request error")

    def test_github_refused_redirect_stays_single_attempt_and_safe(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                delivery,
                "_github_mutation_open",
                side_effect=urllib.error.URLError("redirect-to-bearer-secret"),
            ) as opened,
            redirect_stdout(output),
        ):
            self.assertFalse(
                delivery.create_github_issue("me/repo", "bearer-secret", "title", "body")
            )
        opened.assert_called_once()
        self.assertEqual(
            output.getvalue(), "Failed to create GitHub Issue notification (network error).\n"
        )
        self.assertNotIn("bearer-secret", output.getvalue())

    def test_missing_failure_detail_uses_safe_fallback(self) -> None:
        self.assertEqual(
            delivery._failure_message(
                "Failed to send notification", delivery._TransportResult(False)
            ),
            "Failed to send notification (transport error).",
        )
