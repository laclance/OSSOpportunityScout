"""Outbound notification and report delivery transports.

Owns Telegram, Discord, host GitHub reports, and private GitHub report delivery.
It does not render reports, rank candidates, or manage seen-state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opportunity_scout import github

NOTIFICATION_TIMEOUT_SECONDS = 10
GITHUB_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class _JsonRequest:
    endpoint: str
    method: str
    headers: Mapping[str, str]
    payload: Mapping[str, object]
    timeout: int

    def materialize(self) -> urllib.request.Request:
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        body = json.dumps(self.payload).encode("utf-8")
        return urllib.request.Request(
            self.endpoint,
            data=body,
            headers=headers,
            method=self.method,
        )


@dataclass(frozen=True)
class _TransportResult:
    succeeded: bool
    body: bytes = b""


class _NoGitHubMutationRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """Fail closed on every GitHub mutation redirect before credentials can move."""
        raise urllib.error.URLError("GitHub mutation redirect refused")


def _github_mutation_open(request: urllib.request.Request, *, timeout: int) -> Any:
    return urllib.request.build_opener(_NoGitHubMutationRedirect()).open(request, timeout=timeout)


def _perform(request_spec: _JsonRequest) -> _TransportResult:
    try:
        request = request_spec.materialize()
        with urllib.request.urlopen(request, timeout=request_spec.timeout) as response:
            return _TransportResult(True, bytes(response.read()))
    except Exception:
        return _TransportResult(False)


def _perform_github_mutation(request_spec: _JsonRequest) -> _TransportResult:
    """Execute one authenticated GitHub mutation only on the trusted API origin."""
    if not github._trusted_github_api_url(request_spec.endpoint):
        return _TransportResult(False)

    try:
        request = request_spec.materialize()
        with _github_mutation_open(request, timeout=request_spec.timeout) as response:
            return _TransportResult(True, bytes(response.read()))
    except Exception:
        return _TransportResult(False)


def _notification_request(endpoint: str, payload: Mapping[str, object]) -> _JsonRequest:
    return _JsonRequest(
        endpoint=endpoint,
        method="POST",
        headers={},
        payload=payload,
        timeout=NOTIFICATION_TIMEOUT_SECONDS,
    )


def _deliver_notification(provider: str, request_spec: _JsonRequest) -> bool:
    if not _perform(request_spec).succeeded:
        print(f"Failed to send {provider} notification.")
        return False
    print(f"{provider} notification sent successfully.")
    return True


def send_telegram_notification(token: str, chat_id: str, message: str) -> bool:
    """Send a notification message via Telegram Bot API."""
    return _deliver_notification(
        "Telegram",
        _notification_request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {
                "chat_id": chat_id,
                "text": message.replace("@", "@\u200b"),
                "disable_web_page_preview": False,
            },
        ),
    )


def send_discord_notification(webhook_url: str, message: str) -> bool:
    """Send a notification message via Discord Webhook."""
    return _deliver_notification(
        "Discord",
        _notification_request(
            webhook_url,
            {"content": message, "allowed_mentions": {"parse": []}},
        ),
    )


def _github_headers(token: str) -> dict[str, str]:
    return {
        **github.GITHUB_API_HEADERS,
        "Authorization": f"Bearer {token}",
    }


def _github_request(
    endpoint: str,
    token: str,
    *,
    method: str,
    payload: Mapping[str, object],
) -> _JsonRequest:
    return _JsonRequest(
        endpoint=endpoint,
        method=method,
        headers=_github_headers(token),
        payload=payload,
        timeout=GITHUB_TIMEOUT_SECONDS,
    )


def _decode_object(raw: bytes) -> dict[str, object] | None:
    try:
        decoded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _created_issue_url(raw: bytes) -> str | None:
    payload = _decode_object(raw)
    if payload is None:
        return None
    issue_url = payload.get("url")
    return issue_url if isinstance(issue_url, str) and issue_url else None


def create_github_issue(repo_fullname: str, token: str, title: str, body: str) -> bool:
    """Create a native GitHub scan report and immediately close it as not planned."""
    created = _perform_github_mutation(
        _github_request(
            f"https://api.github.com/repos/{repo_fullname}/issues",
            token,
            method="POST",
            payload={"title": title, "body": body, "labels": ["bounty-alert"]},
        )
    )
    if not created.succeeded:
        print("Failed to create GitHub Issue notification.")
        return False

    issue_url = _created_issue_url(created.body)
    if issue_url is None:
        print("Failed to auto-close GitHub Issue notification: created issue URL missing.")
        return False

    closed = _perform_github_mutation(
        _github_request(
            issue_url,
            token,
            method="PATCH",
            payload={"state": "closed", "state_reason": "not_planned"},
        )
    )
    if not closed.succeeded:
        print("Failed to auto-close GitHub Issue notification.")
        return False

    print("GitHub Issue notification created and auto-closed successfully.")
    return True


def _private_repository_verified(repo_fullname: str, token: str) -> bool:
    repository = github.github_get(
        f"https://api.github.com/repos/{repo_fullname}",
        token,
        timeout=GITHUB_TIMEOUT_SECONDS,
        log_errors=False,
    )
    if not isinstance(repository, dict):
        print("Failed to verify private GitHub report destination.")
        return False
    if repository.get("private") is not True:
        print("Private GitHub report destination is not verified private.")
        return False
    return True


def create_private_github_issue(repo_fullname: str, token: str, title: str, body: str) -> bool:
    """Create a report only after GitHub confirms the destination is private."""
    if not _private_repository_verified(repo_fullname, token):
        return False

    created = _perform_github_mutation(
        _github_request(
            f"https://api.github.com/repos/{repo_fullname}/issues",
            token,
            method="POST",
            payload={"title": title, "body": body},
        )
    )
    if not created.succeeded:
        print("Failed to create private GitHub report.")
        return False

    print("Private GitHub report created successfully.")
    return True
