"""Bounded strategic deep-verification orchestration.

Discovery supplies deterministic per-repository preview rows. This module owns the
bounded deep checks, source-failure breaker, final thresholding, repo-slot settlement,
and accepted strategic output. App-level adapters provide the mixed paid/strategic
verifier and preflight policy.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from opportunity_scout import selection as selection_policy
from opportunity_scout import sources
from opportunity_scout.strategic import discovery as strategic_discovery
from opportunity_scout.types import (
    Candidate,
    GitHubIssue,
    IssueRow,
    RejectionRecord,
    SourceFailureReason,
)

STRATEGIC_KEEP_PER_REPO = 3
STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND = 11
STRATEGIC_REFRESH_FAILURE_LIMIT = 2
STRATEGIC_MIN_CAREER_SCORE = 55
STRATEGIC_VERIFY_WORKERS = 8

DeepVerifier = Callable[[GitHubIssue], tuple[Candidate | None, str | None]]
PreflightRejection = Callable[[GitHubIssue], str | None]


@dataclass(frozen=True)
class StrategicVerificationOutcome:
    """One ranked row after preflight, deep verification and final thresholding."""

    row: IssueRow
    candidate: Candidate | None
    reason: str | None
    network_checked: bool


@dataclass(frozen=True)
class StrategicVerificationResult:
    """Accepted strategic candidates plus coordinated rejection/audit diagnostics."""

    candidates: list[Candidate]
    rejected: dict[str, int]
    examples: list[RejectionRecord]
    audit: list[RejectionRecord]
    network_checked_rows: int
    selected_rows: int


@dataclass(frozen=True)
class _RepoVerificationResult:
    repo: str
    outcomes: list[StrategicVerificationOutcome]
    coverage_incomplete: bool


@dataclass(frozen=True)
class _VerificationConfig:
    keep_per_repo: int
    score_uplift_bound: int
    refresh_failure_limit: int
    min_career_score: int
    min_cash_score: int


def _add_reject(
    counts: dict[str, int],
    examples: list[RejectionRecord],
    item: GitHubIssue,
    reason: str,
) -> None:
    counts[reason] = counts.get(reason, 0) + 1
    if len(examples) < 12:
        examples.append(
            {
                "url": item.get("html_url"),
                "title": item.get("title"),
                "reason": reason,
            }
        )


def _verify_row(
    row: IssueRow,
    deep_verify: DeepVerifier,
    preflight_rejection: PreflightRejection,
    config: _VerificationConfig,
) -> StrategicVerificationOutcome:
    item = row[3]
    preflight_reason = preflight_rejection(item)
    if preflight_reason:
        return StrategicVerificationOutcome(
            row=row,
            candidate=None,
            reason=preflight_reason,
            network_checked=False,
        )

    candidate, reason = deep_verify(item)
    # Preview classification/scores cannot prove threshold rejection:
    # refreshed payment and issue evidence may change either lane.
    if candidate is not None and reason is None:
        reason = selection_policy.score_rejection(
            candidate,
            min_cash_score=config.min_cash_score,
            min_career_score=config.min_career_score,
        )

    return StrategicVerificationOutcome(
        row=row,
        candidate=candidate,
        reason=reason,
        network_checked=True,
    )


def _verify_repo(
    entry: tuple[str, list[IssueRow]],
    *,
    deep_verify: DeepVerifier,
    preflight_rejection: PreflightRejection,
    config: _VerificationConfig,
) -> _RepoVerificationResult:
    repo, ranked = entry
    outcomes: list[StrategicVerificationOutcome] = []
    accepted: list[Candidate] = []
    consecutive_source_failures = 0

    for index, row in enumerate(ranked):
        outcome = _verify_row(row, deep_verify, preflight_rejection, config)
        outcomes.append(outcome)

        if isinstance(outcome.reason, SourceFailureReason):
            consecutive_source_failures += 1
        else:
            consecutive_source_failures = 0

        if outcome.candidate is not None and outcome.reason is None:
            accepted.append(outcome.candidate)

        if consecutive_source_failures >= config.refresh_failure_limit:
            return _RepoVerificationResult(repo, outcomes, True)

        remaining = ranked[index + 1 :]
        if sources.strategic_repo_slots_settled(
            accepted,
            remaining,
            keep_per_repo=config.keep_per_repo,
            score_uplift_bound=config.score_uplift_bound,
        ):
            return _RepoVerificationResult(repo, outcomes, False)

    return _RepoVerificationResult(repo, outcomes, False)


def _select_final_candidates(
    verified_by_repo: dict[str, list[Candidate]],
    repo_order: list[str],
    *,
    keep_per_repo: int,
) -> list[Candidate]:
    found: list[Candidate] = []
    for repo in repo_order:
        verified_repo = verified_by_repo[repo]
        verified_repo.sort(
            key=sources.candidate_rank_key,
            reverse=True,
        )
        found.extend(verified_repo[:keep_per_repo])
    return found


def verify_strategic_selection(
    selection: strategic_discovery.StrategicDiscoverySelection,
    deep_verify: DeepVerifier,
    preflight_rejection: PreflightRejection,
    *,
    keep_per_repo: int = STRATEGIC_KEEP_PER_REPO,
    score_uplift_bound: int = STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND,
    refresh_failure_limit: int = STRATEGIC_REFRESH_FAILURE_LIMIT,
    min_career_score: int = STRATEGIC_MIN_CAREER_SCORE,
    min_cash_score: int = 55,
    verify_workers: int = STRATEGIC_VERIFY_WORKERS,
    audit_limit: int = strategic_discovery.STRATEGIC_AUDIT_LIMIT,
) -> StrategicVerificationResult:
    """Verify ranked rows within the existing inspection, worker and settlement bounds."""
    ranked_by_repo = selection.ranked_by_repo
    config = _VerificationConfig(
        keep_per_repo=keep_per_repo,
        score_uplift_bound=score_uplift_bound,
        refresh_failure_limit=refresh_failure_limit,
        min_career_score=min_career_score,
        min_cash_score=min_cash_score,
    )
    audit = list(selection.audit)
    rejected: dict[str, int] = {}
    examples: list[RejectionRecord] = []

    verification_inputs = list(ranked_by_repo.items())
    verify_repo = partial(
        _verify_repo,
        deep_verify=deep_verify,
        preflight_rejection=preflight_rejection,
        config=config,
    )
    with ThreadPoolExecutor(
        max_workers=min(verify_workers, max(1, len(verification_inputs)))
    ) as executor:
        repo_verification_results = list(executor.map(verify_repo, verification_inputs))

    verified_by_repo: dict[str, list[Candidate]] = {repo: [] for repo in ranked_by_repo}
    network_checked_rows = 0
    selected_rows = sum(len(rows) for rows in ranked_by_repo.values())
    for repo_result in repo_verification_results:
        repo = repo_result.repo
        outcomes = repo_result.outcomes
        network_checked_rows += sum(1 for outcome in outcomes if outcome.network_checked)
        for outcome in outcomes:
            verified_item = outcome.row[3]
            candidate = outcome.candidate
            reason = outcome.reason
            if reason:
                _add_reject(rejected, examples, verified_item, reason)
                if reason.startswith("career score ") and strategic_discovery.possible_miss_signal(
                    verified_item
                ):
                    strategic_discovery.add_audit(
                        audit,
                        verified_item,
                        f"strong-looking near miss: {reason}",
                        limit=audit_limit,
                    )
                print(f"Skipping strategic candidate {verified_item.get('html_url')}: {reason}")
                continue

            assert candidate is not None
            verified_by_repo[repo].append(candidate)

        if repo_result.coverage_incomplete:
            remaining = ranked_by_repo[repo][len(outcomes) :]
            if remaining:
                strategic_discovery.add_audit(
                    audit,
                    remaining[0][3],
                    f"verification coverage incomplete for {repo} after repeated source failures",
                    limit=audit_limit,
                )
            print(
                "Strategic verification coverage incomplete for "
                f"{repo}; stopped after {len(outcomes)} deep checks"
            )

    print(
        "Strategic deep verification: "
        f"{network_checked_rows}/{selected_rows} inspected rows required network checks"
    )

    found = _select_final_candidates(
        verified_by_repo,
        list(ranked_by_repo),
        keep_per_repo=keep_per_repo,
    )
    return StrategicVerificationResult(
        candidates=found,
        rejected=rejected,
        examples=examples,
        audit=audit,
        network_checked_rows=network_checked_rows,
        selected_rows=selected_rows,
    )
