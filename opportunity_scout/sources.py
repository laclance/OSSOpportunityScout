"""External discovery and source adapters for OSS Opportunity Scout.

This module owns source fetching/parsing and bounded inspection-pool selection.
It does not rank final candidates or decide implementation readiness.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Iterable, Sequence, cast

from opportunity_scout import github
from opportunity_scout.types import (
    Candidate,
    DiscoveryFailureReason,
    GitHubIssue,
    IssueRow as IssueRow,
)


@dataclass(frozen=True)
class TextFetchResult:
    """Result of an attempted official-platform text fetch."""

    text: str
    failure: str | None = None


@dataclass(frozen=True)
class PlatformDiscoveryResult:
    """Official-platform references plus semantic discovery failures."""

    refs: dict[str, str]
    failures: tuple[DiscoveryFailureReason, ...]


@dataclass(frozen=True)
class _IssuePage:
    """Validated target-repository page contents and pagination state."""

    issues: tuple[GitHubIssue, ...]
    is_short: bool


FetchText = Callable[[str], TextFetchResult]
PlatformLoader = tuple[str, Callable[[], PlatformDiscoveryResult]]
IssuePredicate = Callable[[GitHubIssue], bool]

_ISSUE_URL_RE = re.compile(r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+")
_ISSUEHUNT_LINK_RE = re.compile(
    r'href=["\'](/r/([^/"\']+)/([^/"\']+)/issues/(\d+))["\']',
    re.IGNORECASE,
)
_OPIRE_DETAIL_RE = re.compile(r'href=["\'](/issues/[A-Za-z0-9_-]+)["\']')
_BOUNTYHUB_DETAIL_RE = re.compile(r'href=["\'](/en/bounty/view/[A-Za-z0-9_-]+)["\']')
_DOLLAR_AMOUNT_RE = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?")
_OPIRE_BOUNTY_RE = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?\s+bounty\b", re.IGNORECASE)
_PLATFORM_REQUEST_LOCK = Lock()
_PLATFORM_REQUEST_COUNT = 0


def platform_request_count_snapshot() -> int:
    """Return the number of outbound official-platform HTTP attempts so far."""
    with _PLATFORM_REQUEST_LOCK:
        return _PLATFORM_REQUEST_COUNT


def _record_platform_request() -> None:
    global _PLATFORM_REQUEST_COUNT
    with _PLATFORM_REQUEST_LOCK:
        _PLATFORM_REQUEST_COUNT += 1


def _platform_failure(platform: str) -> DiscoveryFailureReason:
    return DiscoveryFailureReason(
        f"official bounty-platform discovery failed for {platform}; scan coverage incomplete"
    )


def _target_issue_url(repo: str, *, per_page: int, page: int) -> str:
    query = urllib.parse.urlencode(
        {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "per_page": per_page,
            "page": page,
        }
    )
    return f"https://api.github.com/repos/{repo}/issues?{query}"


def _validated_issue_page(payload: object, *, per_page: int) -> _IssuePage | None:
    if not isinstance(payload, list):
        return None
    issues = tuple(
        cast(GitHubIssue, item)
        for item in payload
        if isinstance(item, dict) and "pull_request" not in item
    )
    return _IssuePage(issues=issues, is_short=len(payload) < per_page)


def target_repo_issue_pool(
    repo: str,
    token: str | None,
    *,
    fetch_per_page: int,
    fetch_pages: int,
    result_limit: int,
) -> tuple[list[GitHubIssue], str | None]:
    """Fetch a bounded pool of real issues even though GitHub mixes PRs into /issues."""
    collected: list[GitHubIssue] = []
    for page_number in range(1, fetch_pages + 1):
        payload = github.github_get(
            _target_issue_url(repo, per_page=fetch_per_page, page=page_number),
            token,
        )
        page = _validated_issue_page(payload, per_page=fetch_per_page)
        if page is None:
            return (
                collected[:result_limit],
                f"target repo discovery failed for {repo}; scan coverage incomplete",
            )

        remaining = result_limit - len(collected)
        collected.extend(page.issues[:remaining])
        if len(collected) >= result_limit:
            return collected[:result_limit], None
        if page.is_short:
            break

    return collected[:result_limit], None


def github_get_optional(url: str, token: str | None) -> Any:
    """Fetch optional GitHub JSON without turning source absence into a hard failure."""
    return github.github_get(url, token, timeout=10, log_errors=False)


def fetch_text(url: str, timeout: int = 12) -> TextFetchResult:
    """Fetch public HTML while preserving transport-failure identity."""
    _record_platform_request()
    request = urllib.request.Request(url, headers={"User-Agent": "OSSOpportunityScout"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = cast(bytes, response.read())
        return TextFetchResult(text=body.decode("utf-8", errors="replace"))
    except Exception as exc:
        print(f"Platform fetch failed: {exc}")
        return TextFetchResult(text="", failure=str(exc))


def issue_from_github_url(url: str, token: str | None) -> GitHubIssue | None:
    """Fetch a GitHub source issue from a platform-discovered URL."""
    return github.issue_from_github_url(url, token)


def _normalized_platform_text(text: str) -> str:
    return text.replace("\\/", "/")


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _direct_issue_refs(text: str, *, signal: str, limit: int) -> dict[str, str]:
    return {url: signal for url in _ISSUE_URL_RE.findall(text)[:limit]}


def _detail_urls(text: str, pattern: re.Pattern[str], *, base_url: str, limit: int) -> list[str]:
    paths = _ordered_unique(match.group(1) for match in pattern.finditer(text))[:limit]
    return [base_url + path for path in paths]


def _fetch_detail_pages(
    urls: Sequence[str],
    fetcher: FetchText,
    *,
    network_workers: int,
) -> list[TextFetchResult]:
    with ThreadPoolExecutor(max_workers=min(network_workers, max(1, len(urls)))) as executor:
        return list(executor.map(fetcher, urls))


def _issuehunt_page_refs(page_html: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for match in _ISSUEHUNT_LINK_RE.finditer(page_html):
        owner, repo, number = match.group(2), match.group(3), match.group(4)
        source_url = f"https://github.com/{owner}/{repo}/issues/{number}"
        nearby = page_html[match.end() : match.end() + 1200]
        amount = _DOLLAR_AMOUNT_RE.search(nearby)
        signal = "confirmed bounty platform feed (IssueHunt)"
        if amount:
            signal += f": {amount.group(0).strip()}"
        refs[source_url] = signal
    return refs


def issuehunt_platform_refs(
    fetcher: FetchText = fetch_text,
    *,
    pages: int = 2,
) -> PlatformDiscoveryResult:
    """Read the official IssueHunt funded-issues pages."""
    refs: dict[str, str] = {}
    failed = False
    for page_number in range(1, pages + 1):
        url = "https://oss.issuehunt.io/issues"
        if page_number > 1:
            url += f"?page={page_number}"
        fetched = fetcher(url)
        if fetched.failure is not None:
            failed = True
            continue
        refs.update(_issuehunt_page_refs(fetched.text))

    failures = (_platform_failure("IssueHunt"),) if failed else ()
    return PlatformDiscoveryResult(refs=refs, failures=failures)


def _opire_detail_refs(details: Sequence[TextFetchResult]) -> tuple[dict[str, str], bool]:
    refs: dict[str, str] = {}
    failed = False
    for fetched in details:
        if fetched.failure is not None:
            failed = True
            continue
        detail = _normalized_platform_text(fetched.text)
        source = _ISSUE_URL_RE.search(detail)
        if source is None:
            continue
        amount = _OPIRE_BOUNTY_RE.search(detail)
        signal = "confirmed bounty platform feed (Opire)"
        if amount:
            signal += f": {amount.group(0).split()[0]}"
        refs[source.group(0)] = signal
    return refs, failed


def opire_platform_refs(
    fetcher: FetchText = fetch_text,
    *,
    fetch_limit: int = 20,
    network_workers: int = 6,
) -> PlatformDiscoveryResult:
    """Read visible Opire bounty cards and map them back to GitHub issues."""
    listing = fetcher("https://app.opire.dev/home")
    if listing.failure is not None:
        return PlatformDiscoveryResult(refs={}, failures=(_platform_failure("Opire"),))

    normalized = _normalized_platform_text(listing.text)
    refs = _direct_issue_refs(
        normalized,
        signal="confirmed bounty platform feed (Opire)",
        limit=fetch_limit,
    )
    detail_urls = _detail_urls(
        normalized,
        _OPIRE_DETAIL_RE,
        base_url="https://app.opire.dev",
        limit=fetch_limit,
    )
    detail_refs, detail_failed = _opire_detail_refs(
        _fetch_detail_pages(detail_urls, fetcher, network_workers=network_workers)
    )
    refs.update(detail_refs)
    failures = (_platform_failure("Opire"),) if detail_failed else ()
    return PlatformDiscoveryResult(refs=refs, failures=failures)


def _bountyhub_detail_refs(
    details: Sequence[TextFetchResult],
    *,
    amount_pattern: str,
) -> tuple[dict[str, str], bool]:
    refs: dict[str, str] = {}
    failed = False
    amount_re = re.compile(amount_pattern, re.IGNORECASE)
    for fetched in details:
        if fetched.failure is not None:
            failed = True
            continue
        detail = _normalized_platform_text(fetched.text)
        source = _ISSUE_URL_RE.search(detail)
        if source is None:
            continue
        amount = amount_re.search(detail)
        signal = "confirmed bounty platform feed (BountyHub)"
        if amount:
            signal += f": {amount.group(0).strip()}"
        refs[source.group(0)] = signal
    return refs, failed


def bountyhub_platform_refs(
    amount_pattern: str,
    fetcher: FetchText = fetch_text,
    *,
    fetch_limit: int = 20,
    network_workers: int = 6,
) -> PlatformDiscoveryResult:
    """Read public BountyHub listings when the site exposes them in HTML."""
    listing = fetcher("https://www.bountyhub.dev/en/bounties")
    if listing.failure is not None:
        return PlatformDiscoveryResult(refs={}, failures=(_platform_failure("BountyHub"),))

    normalized = _normalized_platform_text(listing.text)
    refs = _direct_issue_refs(
        normalized,
        signal="confirmed bounty platform feed (BountyHub)",
        limit=fetch_limit,
    )
    detail_urls = _detail_urls(
        normalized,
        _BOUNTYHUB_DETAIL_RE,
        base_url="https://www.bountyhub.dev",
        limit=fetch_limit,
    )
    detail_refs, detail_failed = _bountyhub_detail_refs(
        _fetch_detail_pages(detail_urls, fetcher, network_workers=network_workers),
        amount_pattern=amount_pattern,
    )
    refs.update(detail_refs)
    failures = (_platform_failure("BountyHub"),) if detail_failed else ()
    return PlatformDiscoveryResult(refs=refs, failures=failures)


def _load_platform(row: PlatformLoader) -> PlatformDiscoveryResult:
    name, loader = row
    try:
        return loader()
    except Exception as exc:
        print(f"Platform source loader failed for {name}: {exc}")
        return PlatformDiscoveryResult(refs={}, failures=(_platform_failure(name),))


def platform_paid_refs(
    loaders: Sequence[PlatformLoader],
    *,
    network_workers: int = 6,
) -> PlatformDiscoveryResult:
    """Collect official-platform discoveries while retaining failure evidence."""
    if not loaders:
        return PlatformDiscoveryResult(refs={}, failures=())

    refs: dict[str, str] = {}
    failures: list[DiscoveryFailureReason] = []
    with ThreadPoolExecutor(max_workers=min(network_workers, len(loaders))) as executor:
        for source in executor.map(_load_platform, loaders):
            refs.update(source.refs)
            failures.extend(source.failures)
    return PlatformDiscoveryResult(refs=refs, failures=tuple(failures))


def contribution_guide(
    repo: str,
    token: str | None,
    getter: Callable[[str, str | None], Any] = github_get_optional,
) -> str | None:
    """Compatibility wrapper for contribution-guide GitHub access."""
    return github.contribution_guide(repo, token, getter)


def _preview_score(row: IssueRow) -> tuple[int, int, int]:
    return row[0], row[1], row[2]


def strategic_inspection_items(
    provisional: list[IssueRow],
    *,
    base_per_repo: int,
    adaptive_budget: int,
    should_expand: IssuePredicate,
) -> dict[str, list[GitHubIssue]]:
    """Select base per-repo rows plus a globally bounded set of strong overflow rows."""
    grouped: dict[str, list[IssueRow]] = {}
    for row in provisional:
        repo, _ = github.issue_repo_and_number(row[3])
        if repo is not None:
            grouped.setdefault(repo, []).append(row)

    selected: dict[str, list[IssueRow]] = {}
    overflow: list[tuple[IssueRow, str]] = []
    for repo, rows in grouped.items():
        ordered = sorted(rows, key=_preview_score, reverse=True)
        selected[repo] = ordered[:base_per_repo]
        overflow.extend(
            (row, repo) for row in ordered[base_per_repo:] if should_expand(row[3]) is True
        )

    overflow.sort(key=lambda entry: _preview_score(entry[0]), reverse=True)
    for row, repo in overflow[:adaptive_budget]:
        selected[repo].append(row)

    return {repo: [row[3] for row in rows] for repo, rows in selected.items()}


def candidate_rank_key(candidate: Candidate) -> tuple[int, int, int, int]:
    """Return the canonical final-candidate ordering key."""
    return (
        int(candidate["priority_score"]),
        int(candidate["career_score"]),
        int(candidate["cash_score"]),
        -int(candidate.get("comments") or 0),
    )


def strategic_verification_upper_bound(
    row: IssueRow,
    *,
    score_uplift_bound: int,
) -> tuple[int, int, int, int]:
    """Return the most optimistic final ordering key for an unverified preview row."""
    return (
        min(100, row[0] + score_uplift_bound),
        min(100, row[1] + score_uplift_bound),
        row[2],
        -int(row[3].get("comments") or 0),
    )


def strategic_repo_slots_settled(
    verified: Sequence[Candidate],
    remaining: Sequence[IssueRow],
    *,
    keep_per_repo: int,
    score_uplift_bound: int,
) -> bool:
    """Return whether remaining previews cannot displace the verified keep set."""
    if len(verified) < keep_per_repo:
        return False
    if not remaining:
        return True

    cutoff = candidate_rank_key(
        sorted(verified, key=candidate_rank_key, reverse=True)[keep_per_repo - 1]
    )
    strongest_remaining = max(
        strategic_verification_upper_bound(row, score_uplift_bound=score_uplift_bound)
        for row in remaining
    )
    return strongest_remaining <= cutoff
