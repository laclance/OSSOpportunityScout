"""Strategic discovery source selection and bounded candidate inspection.

Discovery has four explicit stages: collect bounded source batches, normalize them into
eligible candidate previews, choose the bounded inspection pool, and rebuild deterministic
per-repository rankings for verification. Application-layer adapters supply the paid-lane
signals and policy predicates used at the boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from opportunity_scout import github, scoring, sources
from opportunity_scout.strategic.readiness import (
    automated_tracking_issue_reason,
    manual_tracking_issue_reason,
    release_tracking_reason,
    reward_history_reason,
    security_disclosure_reason,
)
from opportunity_scout.types import (
    Candidate,
    CandidateLane,
    DiscoveryFailureReason,
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
LanguageEligible = Callable[[GitHubIssue, RepositoryMetadata], bool]
PaymentSignal = Callable[[GitHubIssue], str | None]
SearchGitHub = Callable[[str, str | None, int], GitHubSearchResult]
TargetRepoIssuePool = Callable[[str, str | None], tuple[list[GitHubIssue], str | None]]


@dataclass(frozen=True)
class StrategicDiscoverySelection:
    """Pre-verification strategic rows and bounded discovery diagnostics."""

    ranked_by_repo: dict[str, list[sources.IssueRow]]
    audit: list[RejectionRecord]


@dataclass(frozen=True)
class _SourceBatch:
    """One ordered source contribution after source-specific validation."""

    items: tuple[GitHubIssue, ...]


@dataclass(frozen=True)
class _CandidatePreview:
    """Cheap candidate ranking evidence retained before deep verification."""

    repo: str
    item: GitHubIssue
    rank: tuple[int, int, int]


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
    if "pull_request" in item or item.get("assignees"):
        return None

    url = str(item.get("html_url", "")).lower()
    title = str(item.get("title", "")).lower()
    body = str(item.get("body", "")).lower()
    if "/bountyscout/issues/" in url:
        return None

    excluded_markers = (
        "bounty alert:",
        "active bounty scan results",
        "new opportunities found",
        "new opportunityies found",
    )
    excluded_terms = (
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
    if any(marker in title or marker in body for marker in excluded_markers):
        return None
    if any(term in title or term in body for term in excluded_terms):
        return None
    return "strong-looking result rejected by an unrecognized basic eligibility filter rule"


def add_audit(
    audit: list[RejectionRecord],
    item: GitHubIssue,
    reason: str,
    *,
    limit: int = STRATEGIC_AUDIT_LIMIT,
) -> None:
    """Append an audit record; semantic coverage failures bypass the tuning-only limit."""
    if len(audit) >= limit and not isinstance(reason, DiscoveryFailureReason):
        return
    audit.append(
        {
            "url": item.get("html_url"),
            "title": item.get("title"),
            "reason": reason,
        }
    )


def _inspection_plan(
    previews: list[_CandidatePreview],
    *,
    base_per_repo: int,
    adaptive_budget: int,
) -> dict[str, list[_CandidatePreview]]:
    grouped: dict[str, list[_CandidatePreview]] = {}
    for preview in previews:
        grouped.setdefault(preview.repo, []).append(preview)

    selected: dict[str, list[_CandidatePreview]] = {}
    overflow: list[_CandidatePreview] = []
    for repo, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: row.rank, reverse=True)
        selected[repo] = ordered[:base_per_repo]
        overflow.extend(row for row in ordered[base_per_repo:] if possible_miss_signal(row.item))

    overflow.sort(key=lambda row: row.rank, reverse=True)
    for row in overflow[:adaptive_budget]:
        selected[row.repo].append(row)
    return selected


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


def _target_repository_batches(
    token: str | None,
    target_repos: list[str],
    network_workers: int,
    target_repo_pool: TargetRepoIssuePool,
    audit: list[RejectionRecord],
    *,
    audit_limit: int,
) -> list[_SourceBatch]:
    def fetch(repo: str) -> tuple[str, list[GitHubIssue], str | None]:
        items, failure = target_repo_pool(repo, token)
        return repo, items, failure

    batches: list[_SourceBatch] = []
    with ThreadPoolExecutor(
        max_workers=min(network_workers, max(1, len(target_repos)))
    ) as executor:
        for repo, items, failure in executor.map(fetch, target_repos):
            if failure:
                add_audit(
                    audit,
                    GitHubIssue(
                        html_url=f"https://github.com/{repo}/issues",
                        title=repo,
                    ),
                    DiscoveryFailureReason(failure),
                    limit=audit_limit,
                )
            batches.append(_SourceBatch(tuple(items)))
    return batches


def _global_search_batches(
    global_search_results: list[SearchBatch],
    audit: list[RejectionRecord],
    *,
    audit_limit: int,
) -> list[_SourceBatch]:
    batches: list[_SourceBatch] = []
    for query, result in global_search_results:
        items = result.get("items")
        if isinstance(items, list):
            batches.append(_SourceBatch(tuple(items)))
            continue
        add_audit(
            audit,
            GitHubIssue(
                html_url="https://github.com/issues",
                title=f"Global GitHub Search: {query}",
            ),
            DiscoveryFailureReason(
                f"global strategic discovery search failed for query: {query}; "
                "scan coverage incomplete"
            ),
            limit=audit_limit,
        )
    return batches


def _collect_source_batches(
    token: str | None,
    target_repos: list[str],
    network_workers: int,
    target_repo_pool: TargetRepoIssuePool,
    global_search_results: list[SearchBatch],
    audit: list[RejectionRecord],
    *,
    audit_limit: int,
) -> list[_SourceBatch]:
    return _target_repository_batches(
        token,
        target_repos,
        network_workers,
        target_repo_pool,
        audit,
        audit_limit=audit_limit,
    ) + _global_search_batches(
        global_search_results,
        audit,
        audit_limit=audit_limit,
    )


def _candidate_preview(
    item: GitHubIssue,
    *,
    token: str | None,
    repo_cache: dict[str, RepositoryMetadata],
    audit: list[RejectionRecord],
    basic_candidate: BasicCandidate,
    fetch_repo_metadata: FetchRepoMetadata,
    payment_signal: PaymentSignal,
    build_candidate: BuildCandidate,
    cache_locks: github.KeyedLockPool,
    audit_limit: int,
    repository_excluded: Callable[[str], bool],
    language_eligible: LanguageEligible,
) -> _CandidatePreview | None:
    if not basic_candidate(item):
        if possible_miss_signal(item):
            reason = basic_rejection_audit_reason(item)
            if reason:
                add_audit(audit, item, reason, limit=audit_limit)
        return None

    repo, _ = github.issue_repo_and_number(item)
    if not repo or repository_excluded(repo):
        return None

    metadata = github.cached_value(
        repo_cache,
        repo,
        lambda: fetch_repo_metadata(repo, token),
        cache_locks,
        namespace="repo",
    )
    if not metadata:
        add_audit(
            audit,
            item,
            DiscoveryFailureReason(
                "repository metadata unavailable during strategic discovery; "
                "scan coverage incomplete"
            ),
            limit=audit_limit,
        )
        return None

    if metadata.get("archived"):
        if possible_miss_signal(item):
            add_audit(
                audit,
                item,
                "strong-looking result skipped because repository is archived",
                limit=audit_limit,
            )
        return None
    if not language_eligible(item, metadata):
        return None

    signal = payment_signal(item)
    lane: CandidateLane = "paid" if signal else "strategic"
    candidate = build_candidate(item, lane, signal, metadata, None)
    return _CandidatePreview(
        repo=repo,
        item=item,
        rank=(
            candidate["priority_score"],
            candidate["career_score"],
            candidate["cash_score"],
        ),
    )


def _build_preview_pool(
    batches: list[_SourceBatch],
    *,
    token: str | None,
    seen: set[str],
    paid_urls: set[str],
    repo_cache: dict[str, RepositoryMetadata],
    audit: list[RejectionRecord],
    basic_candidate: BasicCandidate,
    fetch_repo_metadata: FetchRepoMetadata,
    payment_signal: PaymentSignal,
    build_candidate: BuildCandidate,
    cache_locks: github.KeyedLockPool,
    audit_limit: int,
    repository_excluded: Callable[[str], bool],
    language_eligible: LanguageEligible,
) -> list[_CandidatePreview]:
    previews: list[_CandidatePreview] = []
    touched: set[str] = set()
    for batch in batches:
        for item in batch.items:
            url = item.get("html_url")
            if not url or url in seen or url in paid_urls or url in touched:
                continue
            touched.add(url)
            preview = _candidate_preview(
                item,
                token=token,
                repo_cache=repo_cache,
                audit=audit,
                basic_candidate=basic_candidate,
                fetch_repo_metadata=fetch_repo_metadata,
                payment_signal=payment_signal,
                build_candidate=build_candidate,
                cache_locks=cache_locks,
                audit_limit=audit_limit,
                repository_excluded=repository_excluded,
                language_eligible=language_eligible,
            )
            if preview is not None:
                previews.append(preview)
    return previews


def _audit_uninspected_strong_candidates(
    previews: list[_CandidatePreview],
    selected: dict[str, list[_CandidatePreview]],
    audit: list[RejectionRecord],
    *,
    audit_limit: int,
) -> None:
    inspected_urls = {
        str(preview.item.get("html_url"))
        for rows in selected.values()
        for preview in rows
        if preview.item.get("html_url")
    }
    for preview in previews:
        url = str(preview.item.get("html_url") or "")
        if url and url not in inspected_urls and possible_miss_signal(preview.item):
            add_audit(
                audit,
                preview.item,
                "strong-looking result fell outside the adaptive repo inspection pool",
                limit=audit_limit,
            )


def _rank_selected_candidates(
    selected: dict[str, list[_CandidatePreview]],
    repo_cache: dict[str, RepositoryMetadata],
    payment_signal: PaymentSignal,
    build_candidate: BuildCandidate,
) -> dict[str, list[sources.IssueRow]]:
    ranked_by_repo: dict[str, list[sources.IssueRow]] = {}
    for repo, previews in selected.items():
        ranked: list[sources.IssueRow] = []
        for preview in previews:
            signal = payment_signal(preview.item)
            lane: CandidateLane = "paid" if signal else "strategic"
            candidate = build_candidate(
                preview.item,
                lane,
                signal,
                repo_cache[repo],
                None,
            )
            ranked.append(
                (
                    candidate["priority_score"],
                    candidate["career_score"],
                    candidate["cash_score"],
                    preview.item,
                )
            )
        ranked.sort(key=lambda row: row[:3], reverse=True)
        ranked_by_repo[repo] = ranked
    return ranked_by_repo


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
    repository_excluded: Callable[[str], bool] = lambda _repo: False,
    language_eligible: LanguageEligible = lambda _item, _meta: True,
) -> StrategicDiscoverySelection:
    """Build deterministic pre-verification rows from bounded strategic sources."""
    audit: list[RejectionRecord] = []
    batches = _collect_source_batches(
        token,
        target_repos,
        network_workers,
        target_repo_pool,
        global_search_results,
        audit,
        audit_limit=audit_limit,
    )
    previews = _build_preview_pool(
        batches,
        token=token,
        seen=seen,
        paid_urls=paid_urls,
        repo_cache=repo_cache,
        audit=audit,
        basic_candidate=basic_candidate,
        fetch_repo_metadata=fetch_repo_metadata,
        payment_signal=payment_signal,
        build_candidate=build_candidate,
        cache_locks=cache_locks,
        audit_limit=audit_limit,
        repository_excluded=repository_excluded,
        language_eligible=language_eligible,
    )
    selected = _inspection_plan(
        previews,
        base_per_repo=inspect_per_repo,
        adaptive_budget=adaptive_budget,
    )
    _audit_uninspected_strong_candidates(
        previews,
        selected,
        audit,
        audit_limit=audit_limit,
    )
    return StrategicDiscoverySelection(
        ranked_by_repo=_rank_selected_candidates(
            selected,
            repo_cache,
            payment_signal,
            build_candidate,
        ),
        audit=audit,
    )
