"""Bounded strategic deep-verification orchestration.

Discovery supplies deterministic per-repository preview rows. This module owns the
bounded deep checks, source-failure breaker, final thresholding, repo-slot settlement,
and accepted strategic output. App-level adapters provide the mixed paid/strategic
verifier and preflight policy.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
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


class _RepositoryCompletion(Enum):
    EXHAUSTED = "exhausted"
    SLOTS_SETTLED = "slots settled"
    COVERAGE_INCOMPLETE = "source coverage incomplete"


@dataclass(frozen=True)
class _VerificationPolicy:
    keep_per_repo: int
    score_uplift_bound: int
    refresh_failure_limit: int
    min_career_score: int
    min_cash_score: int


@dataclass
class _RepositoryProgress:
    accepted: list[Candidate] = field(default_factory=list)
    outcomes: list[StrategicVerificationOutcome] = field(default_factory=list)
    consecutive_source_failures: int = 0

    def record(self, outcome: StrategicVerificationOutcome) -> None:
        self.outcomes.append(outcome)
        if isinstance(outcome.reason, SourceFailureReason):
            self.consecutive_source_failures += 1
        else:
            self.consecutive_source_failures = 0

        if outcome.candidate is not None and outcome.reason is None:
            self.accepted.append(outcome.candidate)


@dataclass(frozen=True)
class _RepositoryVerificationResult:
    repo: str
    outcomes: tuple[StrategicVerificationOutcome, ...]
    completion: _RepositoryCompletion


@dataclass
class _VerificationDiagnostics:
    audit: list[RejectionRecord]
    rejected: dict[str, int] = field(default_factory=dict)
    examples: list[RejectionRecord] = field(default_factory=list)
    network_checked_rows: int = 0

    def reject(self, item: GitHubIssue, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        if len(self.examples) < 12:
            self.examples.append(
                {
                    "url": item.get("html_url"),
                    "title": item.get("title"),
                    "reason": reason,
                }
            )


def _evaluate_row(
    row: IssueRow,
    deep_verify: DeepVerifier,
    preflight_rejection: PreflightRejection,
    policy: _VerificationPolicy,
) -> StrategicVerificationOutcome:
    item = row[3]
    reason = preflight_rejection(item)
    if reason:
        return StrategicVerificationOutcome(
            row=row,
            candidate=None,
            reason=reason,
            network_checked=False,
        )

    candidate, reason = deep_verify(item)
    if candidate is not None and reason is None:
        reason = selection_policy.score_rejection(
            candidate,
            min_cash_score=policy.min_cash_score,
            min_career_score=policy.min_career_score,
        )

    return StrategicVerificationOutcome(
        row=row,
        candidate=candidate,
        reason=reason,
        network_checked=True,
    )


def _repository_completion(
    progress: _RepositoryProgress,
    remaining: list[IssueRow],
    policy: _VerificationPolicy,
) -> _RepositoryCompletion | None:
    if progress.consecutive_source_failures >= policy.refresh_failure_limit:
        return _RepositoryCompletion.COVERAGE_INCOMPLETE

    if sources.strategic_repo_slots_settled(
        progress.accepted,
        remaining,
        keep_per_repo=policy.keep_per_repo,
        score_uplift_bound=policy.score_uplift_bound,
    ):
        return _RepositoryCompletion.SLOTS_SETTLED

    return None


def _verify_repository(
    entry: tuple[str, list[IssueRow]],
    *,
    deep_verify: DeepVerifier,
    preflight_rejection: PreflightRejection,
    policy: _VerificationPolicy,
) -> _RepositoryVerificationResult:
    repo, ranked = entry
    progress = _RepositoryProgress()

    for index, row in enumerate(ranked):
        progress.record(_evaluate_row(row, deep_verify, preflight_rejection, policy))
        completion = _repository_completion(progress, ranked[index + 1 :], policy)
        if completion is not None:
            return _RepositoryVerificationResult(repo, tuple(progress.outcomes), completion)

    return _RepositoryVerificationResult(
        repo,
        tuple(progress.outcomes),
        _RepositoryCompletion.EXHAUSTED,
    )


def _record_near_miss(
    diagnostics: _VerificationDiagnostics,
    item: GitHubIssue,
    reason: str,
    *,
    audit_limit: int,
) -> None:
    if reason.startswith("career score ") and strategic_discovery.possible_miss_signal(item):
        strategic_discovery.add_audit(
            diagnostics.audit,
            item,
            f"strong-looking near miss: {reason}",
            limit=audit_limit,
        )


def _collect_repository_result(
    result: _RepositoryVerificationResult,
    ranked: list[IssueRow],
    diagnostics: _VerificationDiagnostics,
    accepted_by_repo: dict[str, list[Candidate]],
    *,
    audit_limit: int,
) -> None:
    diagnostics.network_checked_rows += sum(
        1 for outcome in result.outcomes if outcome.network_checked
    )

    for outcome in result.outcomes:
        item = outcome.row[3]
        if outcome.reason:
            diagnostics.reject(item, outcome.reason)
            _record_near_miss(
                diagnostics,
                item,
                outcome.reason,
                audit_limit=audit_limit,
            )
            print(f"Skipping strategic candidate {item.get('html_url')}: {outcome.reason}")
            continue

        assert outcome.candidate is not None
        accepted_by_repo[result.repo].append(outcome.candidate)

    if result.completion is not _RepositoryCompletion.COVERAGE_INCOMPLETE:
        return

    remaining = ranked[len(result.outcomes) :]
    if remaining:
        strategic_discovery.add_audit(
            diagnostics.audit,
            remaining[0][3],
            f"verification coverage incomplete for {result.repo} after repeated source failures",
            limit=audit_limit,
        )
    print(
        "Strategic verification coverage incomplete for "
        f"{result.repo}; stopped after {len(result.outcomes)} deep checks"
    )


def _final_candidates(
    accepted_by_repo: dict[str, list[Candidate]],
    repo_order: list[str],
    *,
    keep_per_repo: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for repo in repo_order:
        ranked = sorted(
            accepted_by_repo[repo],
            key=sources.candidate_rank_key,
            reverse=True,
        )
        candidates.extend(ranked[:keep_per_repo])
    return candidates


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
    policy = _VerificationPolicy(
        keep_per_repo=keep_per_repo,
        score_uplift_bound=score_uplift_bound,
        refresh_failure_limit=refresh_failure_limit,
        min_career_score=min_career_score,
        min_cash_score=min_cash_score,
    )
    diagnostics = _VerificationDiagnostics(audit=list(selection.audit))
    accepted_by_repo: dict[str, list[Candidate]] = {repo: [] for repo in ranked_by_repo}
    verification_inputs = list(ranked_by_repo.items())

    verify_repository = partial(
        _verify_repository,
        deep_verify=deep_verify,
        preflight_rejection=preflight_rejection,
        policy=policy,
    )
    with ThreadPoolExecutor(
        max_workers=min(verify_workers, max(1, len(verification_inputs)))
    ) as executor:
        repository_results = list(executor.map(verify_repository, verification_inputs))

    for result in repository_results:
        _collect_repository_result(
            result,
            ranked_by_repo[result.repo],
            diagnostics,
            accepted_by_repo,
            audit_limit=audit_limit,
        )

    selected_rows = sum(len(rows) for rows in ranked_by_repo.values())
    print(
        "Strategic deep verification: "
        f"{diagnostics.network_checked_rows}/{selected_rows} inspected rows required network checks"
    )

    return StrategicVerificationResult(
        candidates=_final_candidates(
            accepted_by_repo,
            list(ranked_by_repo),
            keep_per_repo=keep_per_repo,
        ),
        rejected=diagnostics.rejected,
        examples=diagnostics.examples,
        audit=diagnostics.audit,
        network_checked_rows=diagnostics.network_checked_rows,
        selected_rows=selected_rows,
    )
