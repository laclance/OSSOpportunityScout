from __future__ import annotations

import io
import ssl
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from email.message import Message
from unittest.mock import patch

from bountyscout import delivery, github
from tests.helpers import FakeResponse


def http_error(
    code: int,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"{}",
) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        code,
        "error",
        message,
        io.BytesIO(body),
    )


class SafeReadTests(unittest.TestCase):
    def test_success_first_attempt(self) -> None:
        with patch.object(
            urllib.request, "urlopen", return_value=FakeResponse(b'{"ok": true}')
        ) as opened:
            self.assertEqual(github.github_get("https://api.github.com/x", "tok"), {"ok": True})
        self.assertEqual(opened.call_count, 1)

    def test_error_log_is_classified_and_token_safe(self) -> None:
        output = io.StringIO()
        with (
            patch.object(urllib.request, "urlopen", side_effect=http_error(401)),
            redirect_stdout(output),
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", "secret-token"))
        self.assertIn("authentication failure", output.getvalue())
        self.assertNotIn("secret-token", output.getvalue())

    def test_429_retry_after(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(429, headers={"Retry-After": "5"}),
                    FakeResponse(b'{"ok": true}'),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(5.0)

    def test_primary_reset(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(
                        403,
                        headers={
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": "1030",
                        },
                    ),
                    FakeResponse(b'{"ok": true}'),
                ],
            ) as opened,
            patch("bountyscout.github.time.time", return_value=1000.0),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(30.0)

    def test_primary_retry_after_takes_precedence(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(
                        403,
                        headers={
                            "Retry-After": "4",
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": "9999",
                        },
                    ),
                    FakeResponse(b'{"ok": true}'),
                ],
            ),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        slept.assert_called_once_with(4.0)

    def test_secondary_message_fallback(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(
                        403,
                        body=b'{"message":"You have exceeded a secondary rate limit."}',
                    ),
                    FakeResponse(b'{"ok": true}'),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(60.0)

    def test_429_without_headers_uses_secondary_fallback(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[http_error(429), FakeResponse(b'{"ok": true}')],
            ),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        slept.assert_called_once_with(60.0)

    def test_abuse_detection_message_uses_secondary_fallback(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(403, body=b'{"message":"abuse detection mechanism"}'),
                    FakeResponse(b'{"ok": true}'),
                ],
            ),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        slept.assert_called_once_with(60.0)

    def test_permanent_http_failures_single_attempt(self) -> None:
        for code in (401, 403, 404, 422):
            with self.subTest(code=code):
                with (
                    patch.object(urllib.request, "urlopen", side_effect=http_error(code)) as opened,
                    patch("bountyscout.github.time.sleep") as slept,
                ):
                    self.assertIsNone(
                        github.github_get("https://api.github.com/x", log_errors=False)
                    )
                self.assertEqual(opened.call_count, 1)
                slept.assert_not_called()

    def test_rate_limit_exhaustion_is_bounded(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(429, headers={"Retry-After": "1"}),
                    http_error(429, headers={"Retry-After": "1"}),
                    http_error(429, headers={"Retry-After": "1"}),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", log_errors=False))
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([call.args[0] for call in slept.call_args_list], [1.0, 1.0])

    def test_unreasonable_delay_fails_without_sleep(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=http_error(429, headers={"Retry-After": "121"}),
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", log_errors=False))
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()

    def test_primary_without_reset_fails_without_sleep(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=http_error(403, headers={"X-RateLimit-Remaining": "0"}),
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", log_errors=False))
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()

    def test_primary_past_reset_retries_immediately(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(
                        403,
                        headers={
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": "900",
                        },
                    ),
                    FakeResponse(b'{"ok": true}'),
                ],
            ),
            patch("bountyscout.github.time.time", return_value=1000.0),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        slept.assert_called_once_with(0.0)

    def test_temporary_transport_retries_then_succeeds(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    urllib.error.URLError(ConnectionError("temporary")),
                    FakeResponse(b'{"ok": true}'),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(1.0)

    def test_non_transient_url_errors_are_not_retried(self) -> None:
        errors = (
            urllib.error.URLError("protocol failure"),
            urllib.error.URLError(ssl.SSLError("TLS failure")),
        )
        for error in errors:
            with self.subTest(reason=type(error.reason).__name__):
                with (
                    patch.object(urllib.request, "urlopen", side_effect=error) as opened,
                    patch("bountyscout.github.time.sleep") as slept,
                ):
                    self.assertIsNone(
                        github.github_get("https://api.github.com/x", log_errors=False)
                    )
                self.assertEqual(opened.call_count, 1)
                slept.assert_not_called()

    def test_timeout_and_connection_error_are_retried(self) -> None:
        for error in (TimeoutError("timeout"), ConnectionError("reset")):
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(
                        urllib.request,
                        "urlopen",
                        side_effect=[error, FakeResponse(b'{"ok": true}')],
                    ),
                    patch("bountyscout.github.time.sleep") as slept,
                ):
                    self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
                slept.assert_called_once_with(1.0)

    def test_503_retries_with_backoff_and_retry_after(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[http_error(503), FakeResponse(b'{"ok": true}')],
            ),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        slept.assert_called_once_with(1.0)

        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(503, headers={"Retry-After": "7"}),
                    FakeResponse(b'{"ok": true}'),
                ],
            ),
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertEqual(github.github_get("https://api.github.com/x"), {"ok": True})
        slept.assert_called_once_with(7.0)

    def test_malformed_success_is_not_retried(self) -> None:
        for body in (b"{", b"\xff"):
            with self.subTest(body=body):
                with (
                    patch.object(
                        urllib.request, "urlopen", return_value=FakeResponse(body)
                    ) as opened,
                    patch("bountyscout.github.time.sleep") as slept,
                ):
                    self.assertIsNone(
                        github.github_get("https://api.github.com/x", log_errors=False)
                    )
                self.assertEqual(opened.call_count, 1)
                slept.assert_not_called()

    def test_non_retryable_exception_is_not_retried(self) -> None:
        with (
            patch.object(urllib.request, "urlopen", side_effect=PermissionError("no")) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", log_errors=False))
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()

    def test_invalid_retry_headers_do_not_crash(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=http_error(
                    403,
                    headers={
                        "Retry-After": "nope",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "-1",
                    },
                ),
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", log_errors=False))
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()

    def test_error_payload_parser_tolerates_bad_error_body(self) -> None:
        for body in (b"{", b"[]", b'{"message": 1}'):
            with self.subTest(body=body):
                with patch.object(
                    urllib.request, "urlopen", side_effect=http_error(403, body=body)
                ) as opened:
                    self.assertIsNone(
                        github.github_get("https://api.github.com/x", log_errors=False)
                    )
                self.assertEqual(opened.call_count, 1)

    def test_error_payload_read_failure_is_non_retryable(self) -> None:
        error = http_error(403)
        with (
            patch.object(error, "read", side_effect=OSError("unreadable")),
            patch.object(urllib.request, "urlopen", side_effect=error) as opened,
        ):
            self.assertIsNone(github.github_get("https://api.github.com/x", log_errors=False))
        self.assertEqual(opened.call_count, 1)

    def test_issue_lifecycle_404_not_found_and_rate_limit_failed(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=http_error(404)):
            self.assertEqual(
                github.issue_lifecycle("https://github.com/example/project/issues/42", None).status,
                "not_found",
            )
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(429, headers={"Retry-After": "1"}),
                    http_error(429, headers={"Retry-After": "1"}),
                    http_error(429, headers={"Retry-After": "1"}),
                ],
            ),
            patch("bountyscout.github.time.sleep"),
        ):
            self.assertEqual(
                github.issue_lifecycle("https://github.com/example/project/issues/42", None).status,
                "failed",
            )

    def test_private_metadata_retry_then_single_post(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(429, headers={"Retry-After": "2"}),
                    FakeResponse(b'{"private": true}'),
                    FakeResponse(b"{}"),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertTrue(
                delivery.create_private_github_issue(
                    "owner/private-reports",
                    "report-token",
                    "title",
                    "body",
                )
            )
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(opened.call_args_list[0].args[0].get_method(), "GET")
        self.assertEqual(opened.call_args_list[1].args[0].get_method(), "GET")
        self.assertEqual(opened.call_args_list[2].args[0].get_method(), "POST")
        slept.assert_called_once_with(2.0)

    def test_private_metadata_retry_exhaustion_fails_closed_without_post(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    http_error(429, headers={"Retry-After": "1"}),
                    http_error(429, headers={"Retry-After": "1"}),
                    http_error(429, headers={"Retry-After": "1"}),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep"),
        ):
            self.assertFalse(
                delivery.create_private_github_issue(
                    "owner/private-reports",
                    "report-token",
                    "title",
                    "body",
                )
            )
        self.assertEqual(opened.call_count, 3)
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in opened.call_args_list))

    def test_private_report_post_failure_is_not_retried(self) -> None:
        with (
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=[
                    FakeResponse(b'{"private": true}'),
                    urllib.error.URLError("post failed"),
                ],
            ) as opened,
            patch("bountyscout.github.time.sleep") as slept,
        ):
            self.assertFalse(
                delivery.create_private_github_issue(
                    "owner/private-reports",
                    "report-token",
                    "title",
                    "body",
                )
            )
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(opened.call_args_list[1].args[0].get_method(), "POST")
        slept.assert_not_called()

    def test_host_report_mutations_remain_single_attempt(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("create failed"),
        ) as opened:
            self.assertFalse(delivery.create_github_issue("owner/repo", "tok", "title", "body"))
        self.assertEqual(opened.call_count, 1)

        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[
                FakeResponse(b'{"url":"https://api.github.com/repos/owner/repo/issues/42"}'),
                urllib.error.URLError("close failed"),
            ],
        ) as opened:
            self.assertFalse(delivery.create_github_issue("owner/repo", "tok", "title", "body"))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(opened.call_args_list[0].args[0].get_method(), "POST")
        self.assertEqual(opened.call_args_list[1].args[0].get_method(), "PATCH")
