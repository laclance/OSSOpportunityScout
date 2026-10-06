"""Combined scan run lifecycle and delivery/state transaction orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from time import monotonic
from typing import Final

from opportunity_scout import github, reporting, selection, sources, state
from opportunity_scout.preferences import ScoutPreferences
from opportunity_scout.types import (
    Candidate,
    DiscoveryFailureReason,
    GitHubIssue,
    IssueLifecycleStatus,
    RejectionRecord,
    RepositoryMetadata,
    SearchBatch,
    SourceFailureReason,
)

REPORT_LIMIT: Final = 8
STRATEGIC_COVERAGE_WARNING_THRESHOLD: Final = 5

PaidDiscoveryResult = tuple[list[Candidate], dict[str, int], list[RejectionRecord]]
StrategicDiscoveryResult = tuple[
    list[Candidate],
    dict[str, int],
    list[RejectionRecord],
    list[RejectionRecord],
]
PaidDiscovery = Callable[
    [
        str | None,
        set[str],
        dict[str, RepositoryMetadata],
        dict[str, str | None],
        list[SearchBatch] | None,
    ],
    PaidDiscoveryResult,
]
StrategicDiscovery = Callable[
    [
        str | None,
        set[str],
        set[str],
        dict[str, RepositoryMetadata],
        dict[str, str | None],
        list[SearchBatch] | None,
    ],
    StrategicDiscoveryResult,
]
DiscoveryPrefetch = Callable[
    [str | None],
    tuple[list[SearchBatch], list[SearchBatch]],
]
AuditAppender = Callable[[list[RejectionRecord], GitHubIssue, str], None]
TelegramSender = Callable[[str, str, str], bool]
DiscordSender = Callable[[str, str], bool]
GitHubReportSender = Callable[[str, str, str, str], bool]
LifecycleChecker = Callable[[str], IssueLifecycleStatus]


@dataclass(frozen=True)
class RunConfig:
    """Normalized application configuration for one combined scanner run."""

    token: str | None
    repo_fullname: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    discord_webhook: str | None
    github_reports_enabled: bool = False
    private_github_reports_repository: str | None = None
    private_github_reports_token: str | None = None
    preferences: ScoutPreferences = ScoutPreferences()
    state_path: str | Path = state.DEFAULT_STATE_FILE


@dataclass(frozen=True)
class RunDependencies:
    """Narrow callbacks supplied by the application assembly layer."""

    discover_paid: PaidDiscovery
    discover_strategic: StrategicDiscovery
    prefetch_discovery_searches: DiscoveryPrefetch
    append_audit: AuditAppender
    send_telegram: TelegramSender
    send_discord: DiscordSender
    send_github_report: GitHubReportSender
    issue_lifecycle: LifecycleChecker
    send_private_github_report: GitHubReportSender | None = None


@dataclass(frozen=True)
class CoverageStatus:
    """Combined discovery/verification coverage result for one run."""

    verification_failures: int
    discovery_failures: int
    failure_count: int
    warning: str | None

    @property
    def complete(self) -> bool:
        """Only zero recognized failures permits state persistence, regardless of warnings."""
        return self.failure_count == 0


@dataclass(frozen=True)
class DeliveryResult:
    """Aggregated configured-channel delivery outcome."""

    attempted: bool
    delivered: bool


@dataclass(frozen=True)
class CombinedRunResult:
    """Observable outcome of one combined scanner run."""

    queue: tuple[Candidate, ...]
    coverage: CoverageStatus
    delivery: DeliveryResult
    state_saved: bool


class PostDeliveryStateSaveError(state.SeenStateSaveError):
    """Raised when delivery succeeded but local seen-state persistence did not."""


@dataclass
class _DiscoveryContext:
    """Shared discovery identity and caches for both scanner lanes."""

    seen_urls: set[str]
    repository_metadata: dict[str, RepositoryMetadata]
    contribution_guides: dict[str, str | None]


@dataclass(frozen=True)
class _PrefetchedSearches:
    """Search batches reused by discovery instead of issuing duplicate searches."""

    paid: list[SearchBatch] | None
    strategic: list[SearchBatch] | None


@dataclass(frozen=True)
class _DiscoveryOutcome:
    """Combined lane output before final preference filtering and queue ranking."""

    paid: list[Candidate]
    paid_rejects: dict[str, int]
    paid_examples: list[RejectionRecord]
    strategic: list[Candidate]
    strategic_rejects: dict[str, int]
    strategic_examples: list[RejectionRecord]
    strategic_audit: list[RejectionRecord]


@dataclass(frozen=True)
class _RunPlan:
    """Reportable result and coverage decision produced by discovery."""

    queue: tuple[Candidate, ...]
    coverage: CoverageStatus
    rejects: dict[str, int]
    paid_examples: tuple[RejectionRecord, ...]
    strategic_examples: tuple[RejectionRecord, ...]
    strategic_audit: tuple[RejectionRecord, ...]

    @property
    def requires_delivery(self) -> bool:
        """A candidate queue or a coverage warning creates reportable output."""
        return bool(self.queue) or self.coverage.warning is not None


class _StateCommitMode(Enum):
    """Allowed persistence transitions after discovery and delivery settle."""

    NONE = auto()
    MAINTENANCE = auto()
    DELIVERED_QUEUE = auto()


def assemble_queue(
    paid: list[Candidate],
    strategic: list[Candidate],
    *,
    limit: int = REPORT_LIMIT,
) -> list[Candidate]:
    """Deduplicate by URL, keep only strictly higher priority, and rank the final queue."""
    by_url: dict[str, Candidate] = {}
    for candidates in (paid, strategic):
        for candidate in candidates:
            previous = by_url.get(candidate["url"])
            if previous is None or candidate["priority_score"] > previous["priority_score"]:
                by_url[candidate["url"]] = candidate

    return sorted(
        by_url.values(),
        key=sources.candidate_rank_key,
        reverse=True,
    )[:limit]


def _semantic_failure_count(rejects: dict[str, int], failure_type: type[str]) -> int:
    return sum(count for reason, count in rejects.items() if isinstance(reason, failure_type))


def coverage_status(
    strategic_rejects: dict[str, int],
    strategic_audit: list[RejectionRecord],
    *,
    paid_rejects: dict[str, int] | None = None,
    warning_threshold: int = STRATEGIC_COVERAGE_WARNING_THRESHOLD,
) -> CoverageStatus:
    """Count semantically classified failures and apply independent warning thresholds."""
    paid_rejects = paid_rejects or {}
    strategic_verification_failures = _semantic_failure_count(
        strategic_rejects,
        SourceFailureReason,
    )
    paid_verification_failures = _semantic_failure_count(paid_rejects, SourceFailureReason)
    verification_failures = strategic_verification_failures + paid_verification_failures
    discovery_failures = _semantic_failure_count(
        paid_rejects,
        DiscoveryFailureReason,
    ) + sum(1 for item in strategic_audit if isinstance(item.get("reason"), DiscoveryFailureReason))
    failure_count = verification_failures + discovery_failures

    warning = None
    if (
        discovery_failures
        or paid_verification_failures
        or strategic_verification_failures >= warning_threshold
    ):
        warning = (
            "Opportunity discovery/verification coverage is incomplete: "
            f"{failure_count} discovery/source/comment/competition checks failed, "
            "so this ranking may omit stronger candidates. "
            "Seen-state will not be advanced for this run."
        )

    return CoverageStatus(
        verification_failures=verification_failures,
        discovery_failures=discovery_failures,
        failure_count=failure_count,
        warning=warning,
    )


def _save_state(next_state: state.SeenState, path: str | Path) -> bool:
    state.save_seen_state(next_state, path)
    return True


def _maintain_seen_state(
    seen_state: state.SeenState,
    scan_time: datetime,
    issue_lifecycle: LifecycleChecker,
) -> state.SeenStateMaintenanceResult:
    return state.maintain_seen_state(seen_state, scan_time, issue_lifecycle)


def _deliver(
    config: RunConfig,
    dependencies: RunDependencies,
    queue: list[Candidate],
    now: str,
    *,
    paid_examples: list[RejectionRecord],
    strategic_examples: list[RejectionRecord],
    strategic_audit: list[RejectionRecord],
    rejects: dict[str, int],
    coverage_warning: str | None,
) -> DeliveryResult:
    message = reporting.notification_message(queue, now, warning=coverage_warning)
    attempted = False
    successful_attempts: list[bool] = []

    if config.telegram_token and config.telegram_chat_id:
        attempted = True
        successful_attempts.append(
            dependencies.send_telegram(
                config.telegram_token,
                config.telegram_chat_id,
                message,
            )
        )

    if config.discord_webhook:
        attempted = True
        successful_attempts.append(
            dependencies.send_discord(
                config.discord_webhook,
                message.replace("•", "-"),
            )
        )

    public_report_configured = bool(
        config.github_reports_enabled and config.token and config.repo_fullname
    )
    private_report_configured = bool(
        config.private_github_reports_repository and config.private_github_reports_token
    )
    github_body: str | None = None
    if public_report_configured or (
        private_report_configured and dependencies.send_private_github_report is not None
    ):
        github_body = reporting.github_report_body(
            queue,
            now,
            verification_examples=paid_examples + strategic_examples,
            strategic_audit=strategic_audit,
            reject_counts=rejects,
            coverage_warning=coverage_warning,
        )

    if public_report_configured:
        attempted = True
        assert config.repo_fullname is not None
        assert config.token is not None
        assert github_body is not None
        successful_attempts.append(
            dependencies.send_github_report(
                config.repo_fullname,
                config.token,
                reporting.github_report_title(len(queue)),
                github_body,
            )
        )

    if private_report_configured:
        attempted = True
        if dependencies.send_private_github_report is not None:
            assert config.private_github_reports_repository is not None
            assert config.private_github_reports_token is not None
            assert github_body is not None
            successful_attempts.append(
                dependencies.send_private_github_report(
                    config.private_github_reports_repository,
                    config.private_github_reports_token,
                    reporting.github_report_title(len(queue)),
                    github_body,
                )
            )

    return DeliveryResult(
        attempted=attempted,
        delivered=any(successful_attempts),
    )


def _should_prefetch(config: RunConfig) -> bool:
    return bool(
        config.token
        and config.repo_fullname
        and (
            config.preferences.paid
            or (config.preferences.strategic and config.preferences.global_search)
        )
    )


def _prefetch_discovery(
    config: RunConfig,
    dependencies: RunDependencies,
) -> _PrefetchedSearches:
    if not _should_prefetch(config):
        return _PrefetchedSearches(paid=None, strategic=None)
    paid, strategic = dependencies.prefetch_discovery_searches(config.token)
    return _PrefetchedSearches(paid=paid, strategic=strategic)


def _discover(
    config: RunConfig,
    dependencies: RunDependencies,
    context: _DiscoveryContext,
) -> _DiscoveryOutcome:
    started = monotonic()
    scan_request_start = github.request_stats_snapshot()

    prefetch_started = monotonic()
    prefetch_request_start = github.request_stats_snapshot()
    prefetched = _prefetch_discovery(config, dependencies)
    prefetch_seconds = monotonic() - prefetch_started
    prefetch_request_stats = github.request_stats_delta(
        prefetch_request_start,
        github.request_stats_snapshot(),
    )
    print(
        "Scout performance: phase=discovery_prefetch "
        f"seconds={prefetch_seconds:.1f} {github.format_request_stats(prefetch_request_stats)}"
    )

    paid_started = monotonic()
    if config.preferences.paid:
        paid, paid_rejects, paid_examples = dependencies.discover_paid(
            config.token,
            context.seen_urls,
            context.repository_metadata,
            context.contribution_guides,
            prefetched.paid,
        )
    else:
        paid, paid_rejects, paid_examples = [], {}, []
    paid_seconds = monotonic() - paid_started

    strategic_started = monotonic()
    if config.preferences.strategic:
        (
            strategic,
            strategic_rejects,
            strategic_examples,
            strategic_audit,
        ) = dependencies.discover_strategic(
            config.token,
            context.seen_urls,
            {candidate["url"] for candidate in paid},
            context.repository_metadata,
            context.contribution_guides,
            prefetched.strategic,
        )
    else:
        strategic, strategic_rejects, strategic_examples, strategic_audit = [], {}, [], []
    strategic_seconds = monotonic() - strategic_started

    scan_request_stats = github.request_stats_delta(
        scan_request_start,
        github.request_stats_snapshot(),
    )
    print(
        "Scout performance: phase=scan_total "
        f"prefetch={prefetch_seconds:.1f}s paid={paid_seconds:.1f}s "
        f"strategic={strategic_seconds:.1f}s seconds={monotonic() - started:.1f} "
        f"{github.format_request_stats(scan_request_stats)}"
    )

    return _DiscoveryOutcome(
        paid=paid,
        paid_rejects=paid_rejects,
        paid_examples=paid_examples,
        strategic=strategic,
        strategic_rejects=strategic_rejects,
        strategic_examples=strategic_examples,
        strategic_audit=strategic_audit,
    )


def _final_candidates(candidates: list[Candidate], config: RunConfig) -> list[Candidate]:
    return [
        candidate
        for candidate in candidates
        if selection.candidate_rejection(candidate, config.preferences) is None
    ]


def _build_run_plan(
    outcome: _DiscoveryOutcome,
    config: RunConfig,
    *,
    report_limit: int,
    coverage_warning_threshold: int,
) -> _RunPlan:
    queue = assemble_queue(
        _final_candidates(outcome.paid, config),
        _final_candidates(outcome.strategic, config),
        limit=min(report_limit, config.preferences.max_results),
    )
    rejects = reporting.rejection_summary(
        {
            reason: count
            for reason, count in outcome.paid_rejects.items()
            if not isinstance(reason, DiscoveryFailureReason)
        },
        outcome.strategic_rejects,
    )
    coverage = coverage_status(
        outcome.strategic_rejects,
        outcome.strategic_audit,
        paid_rejects=outcome.paid_rejects,
        warning_threshold=coverage_warning_threshold,
    )
    return _RunPlan(
        queue=tuple(queue),
        coverage=coverage,
        rejects=rejects,
        paid_examples=tuple(outcome.paid_examples),
        strategic_examples=tuple(outcome.strategic_examples),
        strategic_audit=tuple(outcome.strategic_audit),
    )


def _print_discovery_diagnostics(plan: _RunPlan) -> None:
    if plan.strategic_audit:
        print("=== POTENTIAL SCANNER MISSES ===")
        for item in plan.strategic_audit:
            print(f"- {item.get('url')}: {item['reason']}")
    if plan.coverage.warning is not None:
        print(f"WARNING: {plan.coverage.warning}")


def _state_commit_mode(plan: _RunPlan, delivery: DeliveryResult) -> _StateCommitMode:
    if not plan.coverage.complete:
        return _StateCommitMode.NONE
    if not plan.requires_delivery:
        return _StateCommitMode.MAINTENANCE
    if delivery.attempted and delivery.delivered:
        return _StateCommitMode.DELIVERED_QUEUE
    return _StateCommitMode.NONE


def _commit_state(
    mode: _StateCommitMode,
    seen_state: state.SeenState,
    plan: _RunPlan,
    config: RunConfig,
    dependencies: RunDependencies,
    scan_time: datetime,
) -> bool:
    if mode is _StateCommitMode.NONE:
        return False

    maintenance = _maintain_seen_state(
        seen_state,
        scan_time,
        dependencies.issue_lifecycle,
    )
    if mode is _StateCommitMode.MAINTENANCE:
        if not maintenance.checked_urls:
            return False
        return _save_state(maintenance.state, config.state_path)

    next_state = maintenance.state
    next_state.mark_reported_many(
        (candidate["url"] for candidate in plan.queue),
        reported_at=scan_time.isoformat().replace("+00:00", "Z"),
    )
    try:
        return _save_state(next_state, config.state_path)
    except state.SeenStateSaveError as error:
        raise PostDeliveryStateSaveError(str(error)) from error


def _print_delivery_diagnostics(plan: _RunPlan, delivery: DeliveryResult) -> None:
    if plan.rejects:
        print(
            "Filtered verified candidates: "
            + ", ".join(f"{reason}={count}" for reason, count in sorted(plan.rejects.items()))
        )

    if delivery.attempted and delivery.delivered:
        if not plan.coverage.complete:
            print("Verification coverage incomplete; state was not updated.")
    else:
        print("No notification was delivered; state was not updated.")


def run_combined_scan(
    config: RunConfig,
    dependencies: RunDependencies,
    scan_time: datetime,
    *,
    report_limit: int = REPORT_LIMIT,
    coverage_warning_threshold: int = STRATEGIC_COVERAGE_WARNING_THRESHOLD,
) -> CombinedRunResult:
    """Run discovery, delivery, and the transactional seen-state commit."""
    seen_state = state.load_seen_state(config.state_path)
    context = _DiscoveryContext(
        seen_urls=seen_state.urls(),
        repository_metadata={},
        contribution_guides={},
    )
    discovery = _discover(config, dependencies, context)
    plan = _build_run_plan(
        discovery,
        config,
        report_limit=report_limit,
        coverage_warning_threshold=coverage_warning_threshold,
    )
    _print_discovery_diagnostics(plan)

    if not plan.requires_delivery:
        if plan.coverage.complete:
            print("No new verified OSS opportunities found.")
        else:
            print("Verification coverage incomplete; state was not updated.")
        delivery = DeliveryResult(attempted=False, delivered=False)
        state_saved = _commit_state(
            _state_commit_mode(plan, delivery),
            seen_state,
            plan,
            config,
            dependencies,
            scan_time,
        )
        return CombinedRunResult(
            queue=plan.queue,
            coverage=plan.coverage,
            delivery=delivery,
            state_saved=state_saved,
        )

    now = scan_time.strftime("%Y-%m-%d %H:%M UTC")
    delivery = _deliver(
        config,
        dependencies,
        list(plan.queue),
        now,
        paid_examples=list(plan.paid_examples),
        strategic_examples=list(plan.strategic_examples),
        strategic_audit=list(plan.strategic_audit),
        rejects=plan.rejects,
        coverage_warning=plan.coverage.warning,
    )
    _print_delivery_diagnostics(plan, delivery)
    state_saved = _commit_state(
        _state_commit_mode(plan, delivery),
        seen_state,
        plan,
        config,
        dependencies,
        scan_time,
    )
    return CombinedRunResult(
        queue=plan.queue,
        coverage=plan.coverage,
        delivery=delivery,
        state_saved=state_saved,
    )
