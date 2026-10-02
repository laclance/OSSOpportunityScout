"""External discovery and source adapters for OSS Opportunity Scout.

This module owns source fetching/parsing and bounded inspection-pool selection.
It does not rank final candidates or decide implementation readiness.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence, cast

from bountyscout import github
from bountyscout.types import Candidate, GitHubIssue, IssueRow as IssueRow

FetchText = Callable[[str], str]
IssuePredicate = Callable[[GitHubIssue], bool]


def target_repo_issue_pool(
    repo: str,
    token: str | None,
    *,
    fetch_per_page: int,
    fetch_pages: int,
    result_limit: int,
) -> tuple[list[GitHubIssue], str | None]:
    """Fetch a bounded pool of real issues even though GitHub mixes PRs into /issues."""
    issues: list[GitHubIssue] = []

    for page in range(1, fetch_pages + 1):
        params = urllib.parse.urlencode(
            {
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "per_page": fetch_per_page,
                "page": page,
            }
        )
        data = github.github_get(
            f"https://api.github.com/repos/{repo}/issues?{params}",
            token,
        )
        if not isinstance(data, list):
            error = f"target repo discovery failed for {repo}; scan coverage incomplete"
            return issues[:result_limit], error

        for item in data:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            issues.append(cast(GitHubIssue, item))
            if len(issues) >= result_limit:
                return issues[:result_limit], None

        if len(data) < fetch_per_page:
            break

    return issues[:result_limit], None


def github_get_optional(url: str, token: str | None) -> Any:
    """Fetch optional GitHub JSON without turning source absence into a hard failure."""
    return github.github_get(url, token, timeout=10, log_errors=False)


def fetch_text(url: str, timeout: int = 12) -> str:
    """Fetch public HTML for official bounty-platform discovery pages."""
    headers = {"User-Agent": "OSSOpportunityScout"}
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return cast(bytes, response.read()).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Platform fetch failed for {url}: {exc}")
        return ""


def issue_from_github_url(url: str, token: str | None) -> GitHubIssue | None:
    """Fetch a GitHub source issue from a platform-discovered URL."""
    return github.issue_from_github_url(url, token)


def issuehunt_platform_refs(
    fetcher: FetchText = fetch_text,
    *,
    pages: int = 2,
) -> dict[str, str]:
    """Read the official IssueHunt funded-issues pages."""
    refs: dict[str, str] = {}
    for page in range(1, pages + 1):
        url = "https://oss.issuehunt.io/issues"
        if page > 1:
            url += f"?page={page}"
        page_html = fetcher(url)
        if not page_html:
            continue

        pattern = re.compile(
            r'href=["\'](/r/([^/"\']+)/([^/"\']+)/issues/(\d+))["\']',
            re.IGNORECASE,
        )
        for match in pattern.finditer(page_html):
            owner, repo, number = match.group(2), match.group(3), match.group(4)
            source_url = f"https://github.com/{owner}/{repo}/issues/{number}"
            nearby = page_html[match.end() : match.end() + 1200]
            amount = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", nearby)
            signal = "confirmed bounty platform feed (IssueHunt)"
            if amount:
                signal += f": {amount.group(0).strip()}"
            refs[source_url] = signal
    return refs


def opire_platform_refs(
    fetcher: FetchText = fetch_text,
    *,
    fetch_limit: int = 20,
    network_workers: int = 6,
) -> dict[str, str]:
    """Read visible Opire bounty cards and map them back to GitHub issues."""
    refs: dict[str, str] = {}
    home = fetcher("https://app.opire.dev/home")
    if not home:
        return refs

    normalized = home.replace("\\/", "/")
    direct = re.findall(
        r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
        normalized,
    )
    for source_url in direct[:fetch_limit]:
        refs[source_url] = "confirmed bounty platform feed (Opire)"

    detail_paths = list(
        dict.fromkeys(
            re.findall(
                r'href=["\'](/issues/[A-Za-z0-9_-]+)["\']',
                normalized,
            )
        )
    )[:fetch_limit]

    with ThreadPoolExecutor(
        max_workers=min(network_workers, max(1, len(detail_paths)))
    ) as executor:
        details = executor.map(
            lambda path: fetcher("https://app.opire.dev" + path),
            detail_paths,
        )
        for detail in details:
            if not detail:
                continue
            detail = detail.replace("\\/", "/")
            source = re.search(
                r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
                detail,
            )
            if not source:
                continue
            amount = re.search(
                r"\$\s*\d[\d,]*(?:\.\d+)?\s+bounty\b",
                detail,
                re.IGNORECASE,
            )
            signal = "confirmed bounty platform feed (Opire)"
            if amount:
                signal += f": {amount.group(0).split()[0]}"
            refs[source.group(0)] = signal

    return refs


def bountyhub_platform_refs(
    amount_pattern: str,
    fetcher: FetchText = fetch_text,
    *,
    fetch_limit: int = 20,
    network_workers: int = 6,
) -> dict[str, str]:
    """Read public BountyHub listings when the site exposes them in HTML."""
    refs: dict[str, str] = {}
    listing = fetcher("https://www.bountyhub.dev/en/bounties")
    if not listing:
        return refs

    normalized = listing.replace("\\/", "/")
    direct = re.findall(
        r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
        normalized,
    )
    for source_url in direct[:fetch_limit]:
        refs[source_url] = "confirmed bounty platform feed (BountyHub)"

    detail_paths = list(
        dict.fromkeys(
            re.findall(
                r'href=["\'](/en/bounty/view/[A-Za-z0-9_-]+)["\']',
                normalized,
            )
        )
    )[:fetch_limit]

    with ThreadPoolExecutor(
        max_workers=min(network_workers, max(1, len(detail_paths)))
    ) as executor:
        details = executor.map(
            lambda path: fetcher("https://www.bountyhub.dev" + path),
            detail_paths,
        )
        for detail in details:
            if not detail:
                continue
            detail = detail.replace("\\/", "/")
            source = re.search(
                r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
                detail,
            )
            if not source:
                continue
            amount = re.search(amount_pattern, detail, re.IGNORECASE)
            signal = "confirmed bounty platform feed (BountyHub)"
            if amount:
                signal += f": {amount.group(0).strip()}"
            refs[source.group(0)] = signal

    return refs


def platform_paid_refs(
    loaders: Sequence[Callable[[], dict[str, str]]],
    *,
    network_workers: int = 6,
) -> dict[str, str]:
    """Collect official-platform discoveries, deduped by source GitHub issue URL."""
    refs: dict[str, str] = {}
    if not loaders:
        return refs

    def load_source(loader: Callable[[], dict[str, str]]) -> dict[str, str]:
        try:
            return loader()
        except Exception as exc:
            print(f"Platform source loader failed: {exc}")
            return {}

    with ThreadPoolExecutor(max_workers=min(network_workers, len(loaders))) as executor:
        for source in executor.map(load_source, loaders):
            refs.update(source)
    return refs


def contribution_guide(
    repo: str,
    token: str | None,
    getter: Callable[[str, str | None], Any] = github_get_optional,
) -> str | None:
    """Compatibility wrapper for contribution-guide GitHub access."""
    return github.contribution_guide(repo, token, getter)


def strategic_inspection_items(
    provisional: list[IssueRow],
    *,
    base_per_repo: int,
    adaptive_budget: int,
    should_expand: IssuePredicate,
) -> dict[str, list[GitHubIssue]]:
    """Select base per-repo rows plus a globally bounded set of strong overflow rows.

    The adaptive budget prevents busy repositories from permanently hiding good
    contributor-ready or recent bug candidates without scaling network work per repo.
    """
    by_repo: dict[str, list[IssueRow]] = {}
    for row in provisional:
        repo, _ = github.issue_repo_and_number(row[3])
        if repo:
            by_repo.setdefault(repo, []).append(row)

    selected_rows: dict[str, list[IssueRow]] = {}
    overflow: list[tuple[IssueRow, str]] = []
    for repo, rows in by_repo.items():
        rows.sort(key=lambda row: row[:3], reverse=True)
        selected_rows[repo] = list(rows[:base_per_repo])
        for row in rows[base_per_repo:]:
            if should_expand(row[3]):
                overflow.append((row, repo))

    overflow.sort(key=lambda entry: entry[0][:3], reverse=True)
    for row, repo in overflow[:adaptive_budget]:
        selected_rows[repo].append(row)

    return {repo: [row[3] for row in rows] for repo, rows in selected_rows.items()}


def candidate_rank_key(candidate: Candidate) -> tuple[int, int, int, int]:
    """Return the final strategic queue ordering key for a verified candidate."""
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
    item = row[3]
    return (
        min(100, row[0] + score_uplift_bound),
        min(100, row[1] + score_uplift_bound),
        row[2],
        -int(item.get("comments") or 0),
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

    ranked_verified = sorted(
        verified,
        key=candidate_rank_key,
        reverse=True,
    )
    cutoff = candidate_rank_key(ranked_verified[keep_per_repo - 1])
    best_remaining = max(
        strategic_verification_upper_bound(
            row,
            score_uplift_bound=score_uplift_bound,
        )
        for row in remaining
    )
    return best_remaining <= cutoff
