from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

from opportunity_scout import (
    delivery,
    github,
    paid,
    paid_verification,
    preferences,
    selection,
    state,
)
from opportunity_scout import reporting as reporting
from opportunity_scout import run
from opportunity_scout import scoring
from opportunity_scout import sources
from opportunity_scout.strategic import competition as competition_policy
from opportunity_scout.strategic import discovery as strategic_discovery
from opportunity_scout.strategic import verification as strategic_verification
from opportunity_scout.strategic.claims import strategic_claim_text as strategic_claim_text
from opportunity_scout.strategic.readiness import (
    TRUSTED_ASSOCIATIONS as TRUSTED_ASSOCIATIONS,
    abandoned_lifecycle_reason as abandoned_lifecycle_reason,
    automated_tracking_issue_reason as automated_tracking_issue_reason,
    issue_label_set as issue_label_set,
    maintainer_comment_authority as maintainer_comment_authority,
    maintainer_issue_decision_reason as maintainer_issue_decision_reason,
    maintainer_submission_hold_reason as maintainer_submission_hold_reason,
    manual_tracking_issue_reason as manual_tracking_issue_reason,
    maintainer_readiness_comment_state as maintainer_readiness_comment_state,
    proposal_stage_signal as proposal_stage_signal,
    reporter_resolution_reason as reporter_resolution_reason,
    reporter_support_triage_reason as reporter_support_triage_reason,
    reward_history_reason as reward_history_reason,
    security_disclosure_reason as security_disclosure_reason,
    readiness_pending_label_reason as readiness_pending_label_reason,
    release_tracking_reason as release_tracking_reason,
    triage_pending_signal as triage_pending_signal,
)
from opportunity_scout.types import (
    Candidate,
    CandidateLane,
    DiscoveryFailureReason,
    GitHubComment,
    GitHubIssue,
    GitHubSearchResult,
    RejectionRecord,
    RepositoryMetadata,
    SearchBatch,
    SourceFailureReason,
)

TARGET_REPOS = strategic_discovery.TARGET_REPOS
STRATEGIC_GLOBAL_QUERIES = strategic_discovery.STRATEGIC_GLOBAL_QUERIES
STRATEGIC_SEARCH_PER_PAGE = strategic_discovery.STRATEGIC_SEARCH_PER_PAGE
STRATEGIC_GLOBAL_SEARCH_PER_PAGE = strategic_discovery.STRATEGIC_GLOBAL_SEARCH_PER_PAGE
TARGET_REPO_FETCH_PER_PAGE = strategic_discovery.TARGET_REPO_FETCH_PER_PAGE
TARGET_REPO_FETCH_PAGES = strategic_discovery.TARGET_REPO_FETCH_PAGES
STRATEGIC_INSPECT_PER_REPO = strategic_discovery.STRATEGIC_INSPECT_PER_REPO
STRATEGIC_ADAPTIVE_INSPECT_BUDGET = strategic_discovery.STRATEGIC_ADAPTIVE_INSPECT_BUDGET
STRATEGIC_KEEP_PER_REPO = strategic_verification.STRATEGIC_KEEP_PER_REPO
STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND = strategic_verification.STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND
STRATEGIC_REFRESH_FAILURE_LIMIT = strategic_verification.STRATEGIC_REFRESH_FAILURE_LIMIT
STRATEGIC_COVERAGE_WARNING_THRESHOLD = run.STRATEGIC_COVERAGE_WARNING_THRESHOLD
STRATEGIC_MIN_CAREER_SCORE = strategic_verification.STRATEGIC_MIN_CAREER_SCORE
STRATEGIC_AUDIT_LIMIT = strategic_discovery.STRATEGIC_AUDIT_LIMIT
PAID_MIN_CASH_SCORE = 55
REPORT_LIMIT = run.REPORT_LIMIT
NETWORK_WORKERS = 6
STRATEGIC_VERIFY_WORKERS = strategic_verification.STRATEGIC_VERIFY_WORKERS
DISCOVERY_SEARCH_INTERVAL_SECONDS = 2.1
CACHE_LOCKS = github.KeyedLockPool()


PAID_DISCOVERY_QUERIES = [
    "is:issue is:open bounty in:title,body sort:updated-desc",
    'is:issue is:open "/reward" in:comments sort:updated-desc',
    "is:issue is:open (opire.dev OR bountyhub.dev OR algora.io) in:comments sort:updated-desc",
]
EXTENDED_AMOUNT_RE = (
    r"(?:[$€£¥₹]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<![A-Za-z])R\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<!ERC-)(?<!\d)\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|cad|aud|nzd|jpy|chf|"
    r"inr|zar|dai|xmr|sol|eth|btc)\b)"
)

PLATFORM_FETCH_LIMIT = 20
ISSUEHUNT_PAGES = 2


def _print_performance_phase(
    phase: str,
    started: float,
    request_start: github.GitHubRequestStats,
    *,
    platform_request_start: int | None = None,
) -> None:
    request_stats = github.request_stats_delta(
        request_start,
        github.request_stats_snapshot(),
    )
    platform_detail = (
        ""
        if platform_request_start is None
        else " platform_requests="
        f"{sources.platform_request_count_snapshot() - platform_request_start}"
    )
    print(
        f"Scout performance: phase={phase} seconds={monotonic() - started:.1f} "
        f"{github.format_request_stats(request_stats)}{platform_detail}"
    )


def target_repo_issue_pool(
    repo: str,
    token: str | None,
) -> tuple[list[GitHubIssue], str | None]:
    """Compatibility wrapper for bounded curated-repository discovery."""
    return strategic_discovery.target_repo_issue_pool(
        repo,
        token,
        fetch_per_page=TARGET_REPO_FETCH_PER_PAGE,
        fetch_pages=TARGET_REPO_FETCH_PAGES,
        result_limit=STRATEGIC_SEARCH_PER_PAGE,
    )


def maintainer_ready_signal(labels_text: str) -> bool:
    """Compatibility wrapper for contributor-ready label detection."""
    return scoring.maintainer_ready_signal(labels_text)


def issue_text(item: GitHubIssue) -> tuple[str, str, str, str]:
    """Compatibility wrapper for normalized issue text."""
    return scoring.issue_text(item)


def code_reference_count(text: str) -> int:
    """Compatibility wrapper for source/config reference counting."""
    return scoring.code_reference_count(text)


def strategic_basic_candidate(item: GitHubIssue) -> bool:
    """Apply strategic eligibility without making comment volume disqualifying."""
    if paid.is_clean_candidate(item):
        return True
    if int(item.get("comments") or 0) <= paid.MAX_COMMENTS:
        return False

    relaxed = cast(GitHubIssue, dict(item))
    relaxed["comments"] = paid.MAX_COMMENTS
    return paid.is_clean_candidate(relaxed)


def github_get_optional(url: str, token: str | None) -> Any:
    """Compatibility wrapper for optional GitHub JSON fetching."""
    return github.github_get(url, token, timeout=10, log_errors=False)


def issue_comments(item: GitHubIssue, token: str | None) -> list[GitHubComment]:
    """Compatibility wrapper for issue-comment GitHub fetching."""
    return github.issue_comments(item, token)


def _strategic_timeline_evidence(
    item: GitHubIssue,
    token: str | None,
) -> tuple[list[GitHubComment] | None, list[Any] | None]:
    """Load one complete timeline and reuse its comment events only when complete."""
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return None, None

    timeline = github.github_collection(
        f"https://api.github.com/repos/{repo}/issues/{number}/timeline?per_page=100",
        token,
    )
    if not isinstance(timeline, list):
        return None, None

    comments = [
        cast(GitHubComment, event)
        for event in timeline
        if isinstance(event, dict) and event.get("event") == "commented"
    ]
    if len(comments) != int(item.get("comments") or 0):
        return None, timeline
    return comments, timeline


TRIAGE_PENDING_LABELS = {"needs-triage"}
TRIAGE_ACCEPTED_LABELS = {"triage/accepted", "good first issue", "help wanted"}
STRATEGIC_CLAIM_MAX_AGE_DAYS = competition_policy.STRATEGIC_CLAIM_MAX_AGE_DAYS


def strategic_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Compatibility wrapper for strategic active-claim detection."""
    return competition_policy.strategic_claim_reason(item, comments)


def linked_open_pr_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Compatibility wrapper for explicitly linked implementation PR detection."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.linked_open_pr_reason(item, token, loaded_comments)


def timeline_open_pr_reason(item: GitHubIssue, token: str | None) -> str | None:
    """Compatibility wrapper for timeline-linked implementation PR detection."""
    return competition_policy.timeline_open_pr_reason(item, token)


def supplemental_claim_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Compatibility wrapper for supplemental active-claim detection."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.supplemental_claim_reason(item, loaded_comments)


def extended_competition_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Apply paid-lane competition checks through the extracted policy module."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.extended_competition_reason(
        item,
        token,
        loaded_comments,
        existing_pr_checker=paid_verification.has_existing_implementation_pr,
        linked_pr_checker=linked_open_pr_reason,
        supplemental_claim_checker=lambda candidate, claim_comments: supplemental_claim_reason(
            candidate,
            token,
            claim_comments,
        ),
    )


def strategic_competition_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
    *,
    timeline_events: list[Any] | None = None,
) -> str | None:
    """Apply strategic-only competition checks through the extracted policy module."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    timeline_pr_checker: competition_policy.TimelinePrChecker = timeline_open_pr_reason
    if timeline_events is not None:

        def prefetched_timeline_pr_checker(
            _item: GitHubIssue,
            _token: str | None,
        ) -> str | None:
            repo, number = github.issue_repo_and_number(_item)
            return paid_verification.existing_implementation_pr_reason(
                timeline_events,
                cast(str, repo),
                cast(int, number),
            )

        timeline_pr_checker = prefetched_timeline_pr_checker
    return competition_policy.strategic_competition_reason(
        item,
        token,
        loaded_comments,
        timeline_pr_checker=timeline_pr_checker,
        linked_pr_checker=linked_open_pr_reason,
        strategic_claim_checker=strategic_claim_reason,
    )


def supplemental_payment_signal(item: GitHubIssue) -> str | None:
    """Recognize explicit paid-work wording outside the upstream vocabulary."""
    title, body, labels, _ = issue_text(item)
    text = f"{title}\n{body}"
    term = r"(?:cash\s+prize|stipend|sponsored(?:\s+work)?|funded\s+task)"
    near_amount = re.search(
        term + r".{0,80}?(" + EXTENDED_AMOUNT_RE + r")",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if near_amount:
        return f"explicit paid-work wording: {near_amount.group(1).strip()}"

    amount_near = re.search(
        r"(" + EXTENDED_AMOUNT_RE + r").{0,80}?" + term,
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if amount_near:
        return f"explicit paid-work wording: {amount_near.group(1).strip()}"

    amount = re.search(EXTENDED_AMOUNT_RE, text, re.IGNORECASE)
    if ("bountyhub.dev" in text.lower() or "bountyhub.dev" in labels) and amount:
        return f"named bounty platform + amount (BountyHub): {amount.group(0).strip()}"

    return None


def comment_payment_signal(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Recognize payment signals while reusing comments already loaded by verification."""
    loaded_comments = issue_comments(item, token) if comments is None else comments

    # Prefer explicit bot/platform confirmations over the command that triggered them.
    for comment in loaded_comments:
        body = str(comment.get("body", ""))
        login = str((comment.get("user") or {}).get("login", "")).lower()
        amount = re.search(EXTENDED_AMOUNT_RE, body, re.IGNORECASE)

        if (
            amount
            and ("algora" in login or "algora.io" in body.lower())
            and re.search(r"\bbounty created\b", body, re.IGNORECASE)
        ):
            return f"confirmed bounty platform comment (Algora): {amount.group(0).strip()}"

        if (
            amount
            and ("opire" in login or "opire.dev" in body.lower())
            and re.search(r"\b(?:reward|bounty)\b", body, re.IGNORECASE)
        ):
            return f"confirmed bounty platform comment (Opire): {amount.group(0).strip()}"

        if (
            amount
            and ("bountyhub" in login or "bountyhub.dev" in body.lower())
            and re.search(
                r"\bbounty\b.*\bcreated\b|\bcreated\b.*\bbounty\b", body, re.IGNORECASE | re.DOTALL
            )
        ):
            return f"confirmed bounty platform comment (BountyHub): {amount.group(0).strip()}"

    # Commands alone are accepted only from a repo owner/member/collaborator.
    for comment in loaded_comments:
        body = str(comment.get("body", ""))
        association = str(comment.get("author_association", "")).upper()
        if association not in TRUSTED_ASSOCIATIONS:
            continue

        reward = re.search(
            r"/reward\s+(\d[\d,]*(?:\.\d+)?)\b",
            body,
            re.IGNORECASE,
        )
        if reward:
            return f"explicit /reward comment: ${reward.group(1)}"

        algora = re.search(
            r"/bounty\s+(" + EXTENDED_AMOUNT_RE + r")",
            body,
            re.IGNORECASE,
        )
        if algora:
            return f"explicit /bounty comment: {algora.group(1).strip()}"

    return None


def issue_from_github_url(url: str, token: str | None) -> GitHubIssue | None:
    """Compatibility wrapper for platform-discovered GitHub issue fetching."""
    return github.issue_from_github_url(url, token)


def issue_from_github_url_checked(
    url: str,
    token: str | None,
) -> tuple[GitHubIssue | None, SourceFailureReason | None]:
    """Compatibility wrapper preserving platform source hydration failures."""
    return github.issue_from_github_url_checked(url, token)


def platform_paid_refs() -> sources.PlatformDiscoveryResult:
    """Merge official bounty-platform source discoveries and failures."""
    return sources.platform_paid_refs(
        (
            (
                "IssueHunt",
                lambda: sources.issuehunt_platform_refs(pages=ISSUEHUNT_PAGES),
            ),
            (
                "Opire",
                lambda: sources.opire_platform_refs(
                    fetch_limit=PLATFORM_FETCH_LIMIT,
                    network_workers=NETWORK_WORKERS,
                ),
            ),
            (
                "BountyHub",
                lambda: sources.bountyhub_platform_refs(
                    EXTENDED_AMOUNT_RE,
                    fetch_limit=PLATFORM_FETCH_LIMIT,
                    network_workers=NETWORK_WORKERS,
                ),
            ),
        ),
        network_workers=NETWORK_WORKERS,
    )


def contribution_guide(repo: str, token: str | None) -> str | None:
    """Compatibility wrapper for contribution-guide GitHub fetching."""
    return github.contribution_guide(repo, token, github_get_optional)


def fetch_repo_metadata(repo: str, token: str | None) -> RepositoryMetadata:
    """Compatibility wrapper for repository metadata GitHub fetching."""
    return github.repo_metadata(repo, token)


def build_candidate(
    item: GitHubIssue,
    lane: CandidateLane,
    signal: str | None,
    repo_meta: RepositoryMetadata,
    guide: str | None,
    activity_comments: list[GitHubComment] | None = None,
) -> Candidate:
    """Build ranked candidate output through the extracted scoring module."""
    return scoring.build_candidate(
        item,
        lane,
        signal,
        repo_meta,
        guide,
        activity_comments,
        target_repos=TARGET_REPOS,
        amount_pattern=EXTENDED_AMOUNT_RE,
    )


def refresh_issue(item: GitHubIssue, token: str | None) -> tuple[GitHubIssue | None, str | None]:
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return None, "could not identify repository/issue number"
    fresh = github.github_get(f"https://api.github.com/repos/{repo}/issues/{number}", token)
    if not isinstance(fresh, dict):
        return None, SourceFailureReason("could not refresh source issue")
    if fresh.get("state") != "open":
        return None, "issue is no longer open"
    if "pull_request" in fresh:
        return None, "source is a pull request, not an issue"
    return cast(GitHubIssue, fresh), None


def upstream_wrapper_issue_url(item: GitHubIssue) -> str | None:
    """Return the real GitHub source issue for explicit aggregator/handoff wrappers."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    if "ORIGINAL_ISSUE_URL" not in body:
        return None
    if "TARGET_REPOSITORY" not in body and "READY FOR ENGINEERING" not in title.upper():
        return None

    match = re.search(
        r"\bORIGINAL_ISSUE_URL\b.{0,240}?"
        r"(https://github\.com/[^/\s]+/[^/\s]+/issues/\d+)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else None


def non_actionable_diagnostic_reason(item: GitHubIssue) -> str | None:
    """Reject machine/OS crash diagnostics that lack an actionable contributor path."""
    _, body, labels, text = issue_text(item)
    maintainer_ready = maintainer_ready_signal(labels)
    if maintainer_ready or code_reference_count(body):
        return None

    system_failure = re.search(
        r"\b(?:hard hang|black screen|kernel panic|force power|force-reset|force reset)\b",
        text,
    )
    hardware_specific = re.search(
        r"\bmacos\b.*\b(?:m[1-9]|apple silicon)\b|"
        r"\b(?:m[1-9]|apple silicon)\b.*\bmacos\b",
        text,
        re.DOTALL,
    )
    diagnostic_only = re.search(
        r"\b(?:no deterministic repro|no .{0,30}(?:app|userspace) crash|"
        r"not in the panic backtrace|sysdiagnose|panic logs?|filed with apple|"
        r"force-reset reports?)\b",
        text,
        re.DOTALL,
    )
    if system_failure and hardware_specific and diagnostic_only:
        return "hardware/kernel diagnostic report without actionable contributor scope"
    return None


def _strategic_common_source_rejection(item: GitHubIssue) -> str | None:
    """Return source-only strategic rejections shared by preflight and full verification."""
    if not strategic_basic_candidate(item):
        return "failed basic eligibility filter"

    _, _, labels, text = issue_text(item)
    if "oss opportunity queue" in text:
        return "generated opportunity-scout report"
    if any(
        label in labels
        for label in (
            "question",
            "support",
            "needs info",
            "needs-info",
            "needs-information",
            "waiting for info",
            "waiting-for-info",
            "invalid",
        )
    ):
        return "support/triage issue rather than a contributor task"

    if "claimed" in issue_label_set(item):
        return "issue is marked claimed by the project"
    return None


def _strategic_classification_rejection(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Return ordered policy rejections that are pure over supplied issue evidence."""
    for reason in (
        security_disclosure_reason(item),
        reporter_support_triage_reason(item),
        manual_tracking_issue_reason(item, comments),
        automated_tracking_issue_reason(item),
        release_tracking_reason(item, comments),
        maintainer_issue_decision_reason(item),
        maintainer_submission_hold_reason(item),
    ):
        if reason:
            return reason
    return None


def strategic_preflight_rejection(item: GitHubIssue) -> str | None:
    """Reject source-visible states that cannot be rescued by comment/timeline checks."""
    common_reason = _strategic_common_source_rejection(item)
    if common_reason:
        return common_reason

    label_set = issue_label_set(item)
    if not int(item.get("comments") or 0):
        labels_text = " ".join(label_set)
        accepted = bool(TRIAGE_ACCEPTED_LABELS & label_set) or maintainer_ready_signal(labels_text)
        abandoned_reason = abandoned_lifecycle_reason(item, False)
        if abandoned_reason:
            return abandoned_reason
        readiness_reason = readiness_pending_label_reason(item, accepted)
        if readiness_reason:
            return readiness_reason
        pending = bool(TRIAGE_PENDING_LABELS & label_set) or triage_pending_signal(labels_text)
        if pending and not accepted:
            return "awaiting maintainer triage"

    classification_reason = _strategic_classification_rejection(item, [])
    if classification_reason:
        return classification_reason

    diagnostic_reason = non_actionable_diagnostic_reason(item)
    if diagnostic_reason:
        return diagnostic_reason

    return strategic_claim_reason(item, [])


def strategic_rejection(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
    *,
    timeline_events: list[Any] | None = None,
) -> str | None:
    common_reason = _strategic_common_source_rejection(item)
    if common_reason:
        return common_reason

    label_set = issue_label_set(item)
    labels_text = " ".join(label_set)
    comment_ready, comment_hold_reason = maintainer_readiness_comment_state(item, comments)

    abandoned_reason = abandoned_lifecycle_reason(item, comment_ready is True)
    if abandoned_reason:
        return abandoned_reason

    accepted = (
        bool(TRIAGE_ACCEPTED_LABELS & label_set)
        or maintainer_ready_signal(labels_text)
        or comment_ready is True
    )

    readiness_reason = readiness_pending_label_reason(item, accepted)
    if readiness_reason:
        return readiness_reason

    pending = bool(TRIAGE_PENDING_LABELS & label_set) or triage_pending_signal(labels_text)
    if pending and not accepted:
        return "awaiting maintainer triage"

    classification_reason = _strategic_classification_rejection(item, comments or [])
    if classification_reason:
        return classification_reason

    reporter_reason = reporter_resolution_reason(item, comments)
    if reporter_reason:
        return reporter_reason

    if comment_hold_reason:
        return comment_hold_reason

    diagnostic_reason = non_actionable_diagnostic_reason(item)
    if diagnostic_reason:
        return diagnostic_reason

    if timeline_events is None:
        return strategic_competition_reason(item, token, comments)
    return strategic_competition_reason(
        item,
        token,
        comments,
        timeline_events=timeline_events,
    )


def verify(
    item: GitHubIssue,
    token: str | None,
    repo_cache: dict[str, RepositoryMetadata],
    guide_cache: dict[str, str | None],
    require_paid: bool = False,
    payment_signal_override: str | None = None,
    activity_comments: list[GitHubComment] | None = None,
    *,
    scout_preferences: preferences.ScoutPreferences = preferences.ScoutPreferences(),
) -> tuple[Candidate | None, str | None]:
    fresh, reason = refresh_issue(item, token)
    if reason:
        return None, reason
    if fresh is None:
        return None, SourceFailureReason("could not refresh source issue")

    upstream_url = upstream_wrapper_issue_url(fresh)
    if upstream_url:
        upstream = issue_from_github_url(upstream_url, token)
        if upstream is None:
            return None, SourceFailureReason(
                "could not refresh upstream issue from aggregator wrapper"
            )
        fresh, reason = refresh_issue(upstream, token)
        if reason:
            if isinstance(reason, SourceFailureReason):
                return None, SourceFailureReason(f"upstream source: {reason}")
            return None, f"upstream source: {reason}"
        if fresh is None:
            return None, SourceFailureReason(
                "could not refresh upstream issue from aggregator wrapper"
            )

    repo, _ = github.issue_repo_and_number(fresh)
    if selection.repository_excluded(repo, scout_preferences):
        return None, "repository excluded by configuration"

    clean = paid.is_clean_candidate(fresh) if require_paid else strategic_basic_candidate(fresh)
    if not clean:
        return None, "failed basic eligibility filter after source refresh"

    reward_history = reward_history_reason(fresh)
    if reward_history:
        return None, reward_history

    comments = activity_comments
    timeline_events: list[Any] | None = None
    issue_signal = paid.payment_signal(fresh) or supplemental_payment_signal(fresh)
    comment_signal = None
    if not issue_signal and int(fresh.get("comments") or 0):
        if comments is None:
            if not require_paid:
                comments, timeline_events = _strategic_timeline_evidence(fresh, token)
            if comments is None:
                comments, comments_reason = github.issue_comments_checked(fresh, token)
                if comments_reason:
                    return None, comments_reason
        comment_signal = comment_payment_signal(fresh, token, comments)
    signal = issue_signal or comment_signal or payment_signal_override

    lane: CandidateLane
    if require_paid or signal:
        issue_author_claim = strategic_claim_reason(fresh, [])
        if issue_author_claim:
            return None, issue_author_claim

        reason, verified_issue_signal = paid_verification.candidate_rejection_reason(
            fresh,
            token,
        )
        if reason and reason != "no explicit payment signal":
            return None, reason

        if verified_issue_signal:
            signal = verified_issue_signal

        if not signal:
            return None, "no explicit payment signal"

        # The upstream verifier stops before competition checks when payment is
        # only present in comments/platform feeds, so finish those checks here.
        if reason == "no explicit payment signal":
            repo, number = github.issue_repo_and_number(fresh)
            competition_reason = extended_competition_reason(fresh, token, comments)
            if competition_reason:
                return None, competition_reason

        lane = "paid"
    else:
        if comments is None:
            comments, comments_reason = github.issue_comments_checked(fresh, token)
            if comments_reason:
                return None, comments_reason
        if timeline_events is None:
            reason = strategic_rejection(fresh, token, comments)
        else:
            reason = strategic_rejection(
                fresh,
                token,
                comments,
                timeline_events=timeline_events,
            )
        if reason:
            return None, reason
        lane = "strategic"

    if not (scout_preferences.paid if lane == "paid" else scout_preferences.strategic):
        return None, "final candidate lane disabled by configuration"

    repo, _ = github.issue_repo_and_number(fresh)
    if repo is None:
        return None, "could not identify repository/issue number"
    repo_meta = github.cached_value(
        repo_cache,
        repo,
        lambda: fetch_repo_metadata(repo, token),
        CACHE_LOCKS,
        namespace="repo",
    )
    if not repo_meta:
        return None, SourceFailureReason("repository metadata unavailable")
    if repo_meta.get("archived"):
        return None, "repository is archived"
    if not selection.language_accepted(repo_meta.get("language"), scout_preferences):
        return None, "repository language excluded by configuration"
    guide = github.cached_value(
        guide_cache,
        repo,
        lambda: contribution_guide(repo, token),
        CACHE_LOCKS,
        namespace="guide",
    )
    return (
        build_candidate(
            fresh,
            lane,
            signal,
            repo_meta,
            guide,
            comments if lane == "strategic" else None,
        ),
        None,
    )


def add_reject(
    counts: dict[str, int], examples: list[RejectionRecord], item: GitHubIssue, reason: str
) -> None:
    counts[reason] = counts.get(reason, 0) + 1
    if len(examples) < 12:
        examples.append({"url": item.get("html_url"), "title": item.get("title"), "reason": reason})


def discover_paid(
    token: str | None,
    seen: set[str],
    repo_cache: dict[str, RepositoryMetadata],
    guide_cache: dict[str, str | None],
    search_results: list[SearchBatch] | None = None,
    *,
    scout_preferences: preferences.ScoutPreferences = preferences.ScoutPreferences(),
) -> tuple[list[Candidate], dict[str, int], list[RejectionRecord]]:
    found: list[Candidate] = []
    touched: set[str] = set()
    rejected: dict[str, int] = {}
    examples: list[RejectionRecord] = []
    pending: list[tuple[GitHubIssue, str | None, bool]] = []

    if search_results is None:
        search_results = [
            (
                query,
                github.search_github(query, token),
            )
            for query in PAID_DISCOVERY_QUERIES
        ]

    for query, result in search_results:
        items = result.get("items")
        if not isinstance(items, list):
            add_reject(
                rejected,
                examples,
                GitHubIssue(
                    html_url="https://github.com/issues",
                    title=f"Paid GitHub Search: {query}",
                ),
                DiscoveryFailureReason(
                    f"paid discovery search failed for query: {query}; scan coverage incomplete"
                ),
            )
            continue
        for item in items:
            url = item.get("html_url")
            if not url or url in seen or url in touched:
                continue
            touched.add(url)
            repo, _ = github.issue_repo_and_number(item)
            if not selection.repository_excluded(
                repo, scout_preferences
            ) and paid.is_clean_candidate(item):
                pending.append((item, None, False))

    # Official platform feeds can expose funded issues that contain no bounty
    # keywords on GitHub at all. Fetch their source issues concurrently, then
    # apply the same source-authoritative verification as direct discoveries.
    platform_discovery_started = monotonic()
    platform_discovery_requests = github.request_stats_snapshot()
    platform_request_start = sources.platform_request_count_snapshot()
    platform_result = platform_paid_refs()
    _print_performance_phase(
        "paid_platform_discovery",
        platform_discovery_started,
        platform_discovery_requests,
        platform_request_start=platform_request_start,
    )
    for failure in platform_result.failures:
        add_reject(
            rejected,
            examples,
            GitHubIssue(
                html_url="https://github.com/issues",
                title="Official bounty-platform discovery",
            ),
            failure,
        )

    platform_sources: list[tuple[str, str]] = []
    for source_url, platform_signal in platform_result.refs.items():
        if source_url in seen or source_url in touched:
            continue
        source_repo, _ = github.issue_repo_and_number(GitHubIssue(html_url=source_url))
        if selection.repository_excluded(source_repo, scout_preferences):
            continue
        touched.add(source_url)
        platform_sources.append((source_url, platform_signal))

    platform_hydration_started = monotonic()
    platform_hydration_requests = github.request_stats_snapshot()
    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(platform_sources)))
    ) as executor:
        platform_items = list(
            executor.map(
                lambda row: issue_from_github_url_checked(row[0], token),
                platform_sources,
            )
        )
    _print_performance_phase(
        "paid_platform_hydration",
        platform_hydration_started,
        platform_hydration_requests,
    )

    for (source_url, platform_signal), (platform_item, source_failure) in zip(
        platform_sources,
        platform_items,
        strict=True,
    ):
        if source_failure:
            add_reject(
                rejected,
                examples,
                GitHubIssue(
                    html_url=source_url,
                    title="Official bounty-platform GitHub source",
                ),
                source_failure,
            )
            continue
        if platform_item and paid.is_clean_candidate(platform_item):
            pending.append((platform_item, platform_signal, True))

    def verify_paid(
        row: tuple[GitHubIssue, str | None, bool],
    ) -> tuple[
        tuple[GitHubIssue, str | None, bool],
        tuple[Candidate | None, str | None],
    ]:
        item, platform_signal, _ = row
        return (
            row,
            verify(
                item,
                token,
                repo_cache,
                guide_cache,
                require_paid=True,
                payment_signal_override=platform_signal,
                scout_preferences=scout_preferences,
            ),
        )

    paid_verification_started = monotonic()
    paid_verification_requests = github.request_stats_snapshot()
    with ThreadPoolExecutor(max_workers=min(NETWORK_WORKERS, max(1, len(pending)))) as executor:
        verification_results = list(executor.map(verify_paid, pending))
    _print_performance_phase(
        "paid_verification",
        paid_verification_started,
        paid_verification_requests,
    )

    for (item, _, is_platform), (candidate, reason) in verification_results:
        url = str(item.get("html_url") or "")
        kind = "platform" if is_platform else "paid"
        if reason:
            add_reject(rejected, examples, item, reason)
            print(f"Skipping {kind} candidate {url}: {reason}")
            continue

        assert candidate is not None
        preference_reason = selection.candidate_rejection(candidate, scout_preferences)
        if preference_reason:
            add_reject(rejected, examples, item, preference_reason)
            print(f"Skipping {kind} candidate {url}: {preference_reason}")
            continue

        found.append(candidate)

    return found, rejected, examples


def possible_miss_signal(item: GitHubIssue) -> bool:
    """Compatibility wrapper for strategic near-miss detection."""
    return strategic_discovery.possible_miss_signal(item)


def basic_rejection_audit_reason(item: GitHubIssue) -> str | None:
    """Compatibility wrapper for strategic basic-filter audit reasons."""
    return strategic_discovery.basic_rejection_audit_reason(item)


def add_audit(
    audit: list[RejectionRecord],
    item: GitHubIssue,
    reason: str,
) -> None:
    """Compatibility wrapper for bounded strategic audit collection."""
    strategic_discovery.add_audit(
        audit,
        item,
        reason,
        limit=STRATEGIC_AUDIT_LIMIT,
    )


def strategic_inspection_items(
    provisional: list[sources.IssueRow],
) -> dict[str, list[GitHubIssue]]:
    """Compatibility wrapper for bounded strategic inspection selection."""
    return strategic_discovery.strategic_inspection_items(
        provisional,
        base_per_repo=STRATEGIC_INSPECT_PER_REPO,
        adaptive_budget=STRATEGIC_ADAPTIVE_INSPECT_BUDGET,
    )


def strategic_global_search_results(
    token: str | None,
) -> list[SearchBatch]:
    """Compatibility wrapper for strategic global Search orchestration."""

    def search_github(query: str, search_token: str | None, per_page: int) -> GitHubSearchResult:
        return github.search_github(
            query,
            search_token,
            per_page=per_page,
        )

    return strategic_discovery.strategic_global_search_results(
        token,
        search_github,
        queries=STRATEGIC_GLOBAL_QUERIES,
        per_page=STRATEGIC_GLOBAL_SEARCH_PER_PAGE,
    )


def prefetch_discovery_searches(
    token: str | None,
    *,
    scout_preferences: preferences.ScoutPreferences = preferences.ScoutPreferences(),
) -> tuple[list[SearchBatch], list[SearchBatch]]:
    """Pace paid and strategic Search calls to avoid burst/secondary rate limits."""
    requests = [
        ("paid", query, 15) for query in PAID_DISCOVERY_QUERIES if scout_preferences.paid
    ] + [
        ("strategic", query, STRATEGIC_GLOBAL_SEARCH_PER_PAGE)
        for query in STRATEGIC_GLOBAL_QUERIES
        if scout_preferences.strategic and scout_preferences.global_search
    ]
    paid_results: list[SearchBatch] = []
    strategic_results: list[SearchBatch] = []
    for index, (lane, query, per_page) in enumerate(requests):
        result = github.search_github(
            query,
            token,
            per_page=per_page,
        )
        target = paid_results if lane == "paid" else strategic_results
        target.append((query, result))
        if index + 1 < len(requests):
            sleep(DISCOVERY_SEARCH_INTERVAL_SECONDS)
    return paid_results, strategic_results


def discover_strategic(
    token: str | None,
    seen: set[str],
    paid_urls: set[str],
    repo_cache: dict[str, RepositoryMetadata],
    guide_cache: dict[str, str | None],
    global_search_results: list[SearchBatch] | None = None,
    *,
    scout_preferences: preferences.ScoutPreferences = preferences.ScoutPreferences(),
) -> tuple[
    list[Candidate],
    dict[str, int],
    list[RejectionRecord],
    list[RejectionRecord],
]:
    if not scout_preferences.global_search:
        global_search_results = []
    elif global_search_results is None:
        global_search_results = strategic_global_search_results(token)

    strategic_discovery_started = monotonic()
    strategic_discovery_requests = github.request_stats_snapshot()
    discovery_selection = strategic_discovery.select_strategic_candidates(
        token,
        seen,
        paid_urls,
        repo_cache,
        global_search_results,
        target_repos=selection.strategic_repositories(TARGET_REPOS, scout_preferences),
        network_workers=NETWORK_WORKERS,
        target_repo_pool=target_repo_issue_pool,
        basic_candidate=strategic_basic_candidate,
        fetch_repo_metadata=fetch_repo_metadata,
        payment_signal=paid.payment_signal,
        build_candidate=build_candidate,
        cache_locks=CACHE_LOCKS,
        inspect_per_repo=STRATEGIC_INSPECT_PER_REPO,
        adaptive_budget=STRATEGIC_ADAPTIVE_INSPECT_BUDGET,
        audit_limit=STRATEGIC_AUDIT_LIMIT,
        repository_excluded=lambda repo: selection.repository_excluded(repo, scout_preferences),
        language_eligible=lambda item, meta: (
            bool(upstream_wrapper_issue_url(item))
            or selection.language_accepted(meta.get("language"), scout_preferences)
        ),
    )
    _print_performance_phase(
        "strategic_discovery",
        strategic_discovery_started,
        strategic_discovery_requests,
    )

    def deep_verify(item: GitHubIssue) -> tuple[Candidate | None, str | None]:
        candidate, reason = verify(
            item, token, repo_cache, guide_cache, scout_preferences=scout_preferences
        )
        if candidate is not None and reason is None:
            reason = selection.candidate_rejection(candidate, scout_preferences)
            if reason is not None:
                candidate = None
        return candidate, reason

    strategic_verification_started = monotonic()
    strategic_verification_requests = github.request_stats_snapshot()
    result = strategic_verification.verify_strategic_selection(
        discovery_selection,
        deep_verify,
        strategic_preflight_rejection,
        keep_per_repo=STRATEGIC_KEEP_PER_REPO,
        score_uplift_bound=STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND,
        refresh_failure_limit=STRATEGIC_REFRESH_FAILURE_LIMIT,
        min_career_score=scout_preferences.min_career_score,
        min_cash_score=scout_preferences.min_cash_score,
        verify_workers=STRATEGIC_VERIFY_WORKERS,
        audit_limit=STRATEGIC_AUDIT_LIMIT,
    )
    _print_performance_phase(
        "strategic_verification",
        strategic_verification_started,
        strategic_verification_requests,
    )
    return result.candidates, result.rejected, result.examples, result.audit


def _run_dependencies(
    token: str | None, scout_preferences: preferences.ScoutPreferences
) -> run.RunDependencies:
    """Bind explicit preferences without mutable globals or widening run callbacks."""
    return run.RunDependencies(
        discover_paid=partial(discover_paid, scout_preferences=scout_preferences),
        discover_strategic=partial(discover_strategic, scout_preferences=scout_preferences),
        prefetch_discovery_searches=partial(
            prefetch_discovery_searches, scout_preferences=scout_preferences
        ),
        append_audit=add_audit,
        send_telegram=delivery.send_telegram_notification,
        send_discord=delivery.send_discord_notification,
        send_github_report=delivery.create_github_issue,
        issue_lifecycle=lambda url: github.issue_lifecycle(url, token).status,
        send_private_github_report=delivery.create_private_github_issue,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Find verified OSS opportunities.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("scout.toml"),
        help="Version-1 scout TOML preferences (default: scout.toml)",
    )
    parser.add_argument(
        "--state", type=Path, default=Path(state.DEFAULT_STATE_FILE), help="Seen-state path"
    )
    arguments = parser.parse_args(argv)
    scout_preferences = preferences.load_scout_preferences(arguments.config)
    token = os.environ.get("GITHUB_TOKEN")
    config = run.RunConfig(
        token=token,
        repo_fullname=os.environ.get("GITHUB_REPOSITORY"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        discord_webhook=os.environ.get("DISCORD_WEBHOOK_URL"),
        github_reports_enabled=(
            os.environ.get("GITHUB_REPORTS_ENABLED", "").strip().casefold() == "true"
        ),
        private_github_reports_repository=os.environ.get("PRIVATE_GITHUB_REPORTS_REPOSITORY"),
        private_github_reports_token=os.environ.get("PRIVATE_GITHUB_REPORTS_TOKEN"),
        preferences=scout_preferences,
        state_path=arguments.state,
    )
    run.run_combined_scan(
        config,
        _run_dependencies(token, scout_preferences),
        datetime.now(timezone.utc),
        report_limit=REPORT_LIMIT,
        coverage_warning_threshold=STRATEGIC_COVERAGE_WARNING_THRESHOLD,
    )


if __name__ == "__main__":
    main()
