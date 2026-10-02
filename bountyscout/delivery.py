"""Outbound notification and report delivery transports.

Owns Telegram, Discord, and GitHub report HTTP delivery.
It does not render reports, rank candidates, or manage seen-state.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping

NOTIFICATION_TIMEOUT_SECONDS = 10
GITHUB_TIMEOUT_SECONDS = 15
GITHUB_USER_AGENT = "OSSOpportunityScout"
GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": GITHUB_USER_AGENT,
    "X-GitHub-Api-Version": "2022-11-28",
}


def _request_json(
    url: str,
    payload: Mapping[str, object],
    *,
    method: str,
    headers: Mapping[str, str],
    timeout: int,
) -> bytes:
    """Serialize and perform one JSON HTTP request."""
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read())


def _send_notification(provider: str, endpoint: str, payload: Mapping[str, object]) -> bool:
    """Deliver one notification through the shared JSON transport."""
    try:
        _request_json(
            endpoint,
            payload,
            method="POST",
            headers={},
            timeout=NOTIFICATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        print(f"Failed to send {provider} notification: {exc}")
        return False

    print(f"{provider} notification sent successfully.")
    return True


def send_telegram_notification(token: str, chat_id: str, message: str) -> bool:
    """Send a notification message via Telegram Bot API."""
    return _send_notification(
        "Telegram",
        f"https://api.telegram.org/bot{token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        },
    )


def send_discord_notification(webhook_url: str, message: str) -> bool:
    """Send a notification message via Discord Webhook."""
    return _send_notification("Discord", webhook_url, {"content": message})


def create_github_issue(repo_fullname: str, token: str, title: str, body: str) -> bool:
    """Create a native GitHub scan report and immediately close it as not planned."""
    headers = {
        **GITHUB_API_HEADERS,
        "Authorization": f"Bearer {token}",
    }
    try:
        created_body = _request_json(
            f"https://api.github.com/repos/{repo_fullname}/issues",
            {"title": title, "body": body, "labels": ["bounty-alert"]},
            method="POST",
            headers=headers,
            timeout=GITHUB_TIMEOUT_SECONDS,
        )
        created: object = json.loads(created_body.decode("utf-8"))
    except Exception as exc:
        print(f"Failed to create GitHub Issue notification: {exc}")
        return False

    issue_url = created.get("url") if isinstance(created, dict) else None
    if not isinstance(issue_url, str) or not issue_url:
        print("Failed to auto-close GitHub Issue notification: created issue URL missing.")
        return False

    try:
        _request_json(
            issue_url,
            {"state": "closed", "state_reason": "not_planned"},
            method="PATCH",
            headers=headers,
            timeout=GITHUB_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        print(f"Failed to auto-close GitHub Issue notification: {exc}")
        return False

    print("GitHub Issue notification created and auto-closed successfully.")
    return True
