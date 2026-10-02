"""Strategic discovery selection and audit orchestration.

This module owns bounded source-pool collection, cheap candidate selection, adaptive
inspection, and discovery audit diagnostics. Application-layer adapters supply the
paid-lane predicates and signals used by strategic discovery.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from bountyscout import github, scoring, sources
from bountyscout.strategic.readiness import (
    automated_tracking_issue_reason,
    manual_tracking_issue_reason,
    release_tracking_reason,
    reward_history_reason,
    security_disclosure_reason,
)
from bountyscout.types import (
    Candidate,
    CandidateLane,
    GitHubIssue,
    GitHubSearchResult,
    RejectionRecord,
    RepositoryMetadata,
    SearchBatch,
)

TARGET_REPOS = [
    "aws/amazon-vpc-cni-k8s",
    "kubernetes/kubernetes",
    "kubernetes-sigs/controller-runtime",
    "kubernetes-sigs/external-dns",
    "kubernetes-sigs/aws-load-balancer-controller",
    "tailscale/tailscale",
    "cilium/cilium",
    "prometheus/prometheus",
    "prometheus/client_golang",
    "grafana/loki",
    "open-telemetry/opentelemetry-go",
    "hashicorp/terraform",
    "fluxcd/flux2",
    "argoproj/argo-cd",
    "golangci/golangci-lint",
    "containerd/containerd",
    "moby/moby",
    "grpc/grpc-go",
    "etcd-io/etcd",
    "cloudflare/cloudflared",
    "traefik/traefik",
    "open-telemetry/opentelemetry-js",
    "nodejs/undici",
]
STRATEGIC_GLOBAL_QUERIES = [
    'is:issue is:open no:assignee label:"help wanted" (label:"bug" OR regression) sort:updated-desc',
    'is:issue is:open no:assignee label:"good first issue" label:"bug" sort:updated-desc',
]
STRATEGIC_SEARCH_PER_PAGE = 20
STRATEGIC_GLOBAL_SEARCH_PER_PAGE = 30
TARGET_REPO_FETCH_PER_PAGE = 50
TARGET_REPO_FETCH_PAGES = 3
STRATEGIC_INSPECT_PER_REPO = 15
STRATEGIC_ADAPTIVE_INSPECT_BUDGET = 24
STRATEGIC_AUDIT_LIMIT = 20

BasicCandidate = Callable[[GitHubIssue], bool]
BuildCandidate = Callable[
    [GitHubIssue, CandidateLane, str | None, RepositoryMetadata, str | None],
    Candidate,
]
FetchRepoMetadata = Callable[[str, str | None], RepositoryMetadata]
PaymentSignal = Callable[[GitHubIssue], str | None]
SearchGitHub = Callable[[str, str | None, int], GitHubSearchResult]
TargetRepoIssuePool = Callable[[str, str | None], tuple[list[GitHubIssue], str | None]]


@dataclass(frozen=True)
class StrategicDiscoverySelection:
    """Pre-verification strategic rows and bounded discovery diagnostics."""

    ranked_by_repo: dict[str, list[sources.IssueRow]]
    audit: list[RejectionRecord]


def target_repo_issue_pool(
    repo: str,
    token: str | None,
    *,
    fetch_per_page: int = TARGET_REPO_FETCH_PER_PAGE,
    fetch_pages: int = TARGET_REPO_FETCH_PAGES,
    result_limit: int = STRATEGIC_SEARCH_PER_PAGE,
) -> tuple[list[GitHubIssue], str | None]:
    """Fetch the configured bounded source pool for a curated repository."""
    return sources.target_repo_issue_pool(
        repo,
        token,
        fetch_per_page=fetch_per_page,
        fetch_pages=fetch_pages,
        result_limit=result_limit,
    )


def possible_miss_signal(item: GitHubIssue) -> bool:
    """Flag strong raw results that deserve scrutiny when filters discard them."""
    if (
        security_disclosure_reason(item)
        or reward_history_reason(item)
        or manual_tracking_issue_reason(item)
        or automated_tracking_issue_reason(item)
        or release_tracking_reason(item)
    ):
        return False

    _, _, labels, text = scoring.issue_text(item)
    updated = github.parse_github_datetime(item.get("updated_at"))
    recent = bool(updated and (datetime.now(timezone.utc) - updated).days <= 60)
    contributor_signal = any(
        marker in labels
        for marker in (
            "help wanted",
            "help-wanted",
            "good first issue",
            "triage/accepted",
            "refined",
            "contributor/wanted",
            "contributor wanted",
        )
    )
    bug_signal = "bug" in labels or bool(
        re.search(r"\b(?:bug|regression|panic|deadlock|leak)\b", text)
    )
    return recent and (contributor_signal or bug_signal)


def basic_rejection_audit_reason(item: GitHubIssue) -> str | None:
    """Return only tunable/unknown basic-filter reasons worth auditing."""
    if "pull_request" in item:
        return None
    if item.get("assignees"):
        return None

    url = str(item.get("html_url", "")).lower()
    title = str(item.get("title", "")).lower()
    body = str(item.get("body", "")).lower()
    if "/bountyscout/issues/" in url:
        return None
    if any(
        marker in title or marker in body
        for marker in (
            "bounty alert:",
            "active bounty scan results",
            "new opportunities found",
            "new opportunityies found",
        )
    ):
        return None
    if any(
        term in title or term in body
        for term in (
            "airdrop",
            "referral",
            "casino",
            "gambling",
            "trading bot",
            "blog post",
            "article writing",
            "tutorial proposal",
            "content creator",
        )
    ):
        return None
    return "strong-looking result rejected by an unrecognized basic eligibility filter rule"


def add_audit(
    audit: list[RejectionRecord],
    item: GitHubIssue,
    reason: str,
    *,
    limit: int = STRATEGIC_AUDIT_LIMIT,
) -> None:
    """Append one bounded discovery audit record."""
    if len(audit) >= limit:
        return
    audit.append(
        {
            "url": item.get("html_url"),
            "title": item.get("title"),
            "reason": reason,
        }
    )


def strategic_inspection_items(
    provisional: list[sources.IssueRow],
    *,
    base_per_repo: int = STRATEGIC_INSPECT_PER_REPO,
    adaptive_budget: int = STRATEGIC_ADAPTIVE_INSPECT_BUDGET,
) -> dict[str, list[GitHubIssue]]:
    """Select base per-repo candidates plus a globally bounded strong overflow."""
    return sources.strategic_inspection_items(
        provisional,
        base_per_repo=base_per_repo,
        adaptive_budget=adaptive_budget,
        should_expand=possible_miss_signal,
    )


def strategic_global_search_results(
    token: str | None,
    search_github: SearchGitHub,
    *,
    queries: list[str] = STRATEGIC_GLOBAL_QUERIES,
    per_page: int = STRATEGIC_GLOBAL_SEARCH_PER_PAGE,
) -> list[SearchBatch]:
    """Reserve the strategic global Search calls before heavier API work begins."""
    return [(query, search_github(query, token, per_page)) for query in queries]


def select_strategic_candidates(
    token: str | None,
    seen: set[str],
    paid_urls: set[str],
    repo_cache: dict[str, RepositoryMetadata],
    global_search_results: list[SearchBatch],
    *,
    target_repos: list[str],
    network_workers: int,
    target_repo_pool: TargetRepoIssuePool,
    basic_candidate: BasicCandidate,
    fetch_repo_metadata: FetchRepoMetadata,
    payment_signal: PaymentSignal,
    build_candidate: BuildCandidate,
    cache_locks: github.KeyedLockPool,
    inspect_per_repo: int = STRATEGIC_INSPECT_PER_REPO,
    adaptive_budget: int = STRATEGIC_ADAPTIVE_INSPECT_BUDGET,
    audit_limit: int = STRATEGIC_AUDIT_LIMIT,
) -> StrategicDiscoverySelection:
    """Build deterministic pre-verification rows from bounded strategic sources."""
    provisional: list[sources.IssueRow] = []
    touched: set[str] = set()
    audit: list[RejectionRecord] = []

    source_batches: list[list[GitHubIssue]] = []
    with ThreadPoolExecutor(
        max_workers=min(network_workers, max(1, len(target_repos)))
    ) as executor:
        repo_results = executor.map(
            lambda target_repo: (
                target_repo,
                target_repo_pool(target_repo, token),
            ),
            target_repos,
        )
        for target_repo, (items, source_error) in repo_results:
            if source_error:
                add_audit(
                    audit,
                    GitHubIssue(
                        html_url=f"https://github.com/{target_repo}/issues",
                        title=target_repo,
                    ),
                    source_error,
                    limit=audit_limit,
                )
            source_batches.append(items)

    for query, result in global_search_results:
        global_items = result.get("items")
        if not isinstance(global_items, list):
            add_audit(
                audit,
                GitHubIssue(
                    html_url="https://github.com/issues",
                    title=f"Global GitHub Search: {query}",
                ),
                f"global strategic discovery search failed for query: {query}; "
                "scan coverage incomplete",
                limit=audit_limit,
            )
            continue
        source_batches.append(global_items)

    for items in source_batches:
        for item in items:
            url = item.get("html_url")
            if not url or url in seen or url in paid_urls or url in touched:
                continue
            touched.add(url)
            if not basic_candidate(item):
                audit_reason = (
                    basic_rejection_audit_reason(item) if possible_miss_signal(item) else None
                )
                if audit_reason:
                    add_audit(audit, item, audit_reason, limit=audit_limit)
                continue
            repo, _ = github.issue_repo_and_number(item)
            if not repo:
                continue
            repo_key = repo
            meta = github.cached_value(
                repo_cache,
                repo_key,
                lambda: fetch_repo_metadata(repo_key, token),
                cache_locks,
                namespace="repo",
            )
            if not meta or meta.get("archived"):
                if possible_miss_signal(item):
                    add_audit(
                        audit,
                        item,
                        "strong-looking result skipped because repository metadata is "
                        "unavailable or archived",
                        limit=audit_limit,
                    )
                continue
            signal = payment_signal(item)
            lane: CandidateLane = "paid" if signal else "strategic"
            preview = build_candidate(item, lane, signal, meta, None)
            provisional.append(
                (
                    preview["priority_score"],
                    preview["career_score"],
                    preview["cash_score"],
                    item,
                )
            )

    inspected = strategic_inspection_items(
        provisional,
        base_per_repo=inspect_per_repo,
        adaptive_budget=adaptive_budget,
    )
    inspected_urls = {
        str(item.get("html_url"))
        for items in inspected.values()
        for item in items
        if item.get("html_url")
    }
    for _, _, _, preview_item in provisional:
        url = str(preview_item.get("html_url") or "")
        if url and url not in inspected_urls and possible_miss_signal(preview_item):
            add_audit(
                audit,
                preview_item,
                "strong-looking result fell outside the adaptive repo inspection pool",
                limit=audit_limit,
            )

    ranked_by_repo: dict[str, list[sources.IssueRow]] = {}
    for repo, items in inspected.items():
        ranked: list[sources.IssueRow] = []
        for item in items:
            signal = payment_signal(item)
            lane = "paid" if signal else "strategic"
            preview = build_candidate(item, lane, signal, repo_cache[repo], None)
            ranked.append(
                (
                    preview["priority_score"],
                    preview["career_score"],
                    preview["cash_score"],
                    item,
                )
            )
        ranked.sort(key=lambda row: row[:3], reverse=True)
        ranked_by_repo[repo] = ranked

    return StrategicDiscoverySelection(ranked_by_repo=ranked_by_repo, audit=audit)
