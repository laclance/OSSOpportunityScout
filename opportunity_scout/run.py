"""Combined scan run lifecycle and delivery/state transaction orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Final

from opportunity_scout import reporting, selection, sources, state
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


def assemble_queue(
    paid: list[Candidate],
    strategic: list[Candidate],
    *,
    limit: int = REPORT_LIMIT,
) -> list[Candidate]:
    """Deduplicate by URL, keep only strictly higher priority, and rank the final queue."""
    by_url: dict[str, Candidate] = {}
    for candidate in paid + strategic:
        old = by_url.get(candidate["url"])
        if not old or candidate["priority_score"] > old["priority_score"]:
            by_url[candidate["url"]] = candidate

    return sorted(
        by_url.values(),
        key=sources.candidate_rank_key,
        reverse=True,
    )[:limit]


def coverage_status(
    strategic_rejects: dict[str, int],
    strategic_audit: list[RejectionRecord],
    *,
    paid_rejects: dict[str, int] | None = None,
    warning_threshold: int = STRATEGIC_COVERAGE_WARNING_THRESHOLD,
) -> CoverageStatus:
    """Count semantically classified failures and apply independent warning thresholds."""
    paid_rejects = paid_rejects or {}
    strategic_verification_failures = sum(
        count
        for reason, count in strategic_rejects.items()
        if isinstance(reason, SourceFailureReason)
    )
    paid_verification_failures = sum(
        count for reason, count in paid_rejects.items() if isinstance(reason, SourceFailureReason)
    )
    verification_failures = strategic_verification_failures + paid_verification_failures
    discovery_failures = sum(
        count
        for reason, count in paid_rejects.items()
        if isinstance(reason, DiscoveryFailureReason)
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
    delivered = False

    if config.telegram_token and config.telegram_chat_id:
        attempted = True
        delivered = (
            dependencies.send_telegram(
                config.telegram_token,
                config.telegram_chat_id,
                message,
            )
            or delivered
        )

    if config.discord_webhook:
        attempted = True
        delivered = (
            dependencies.send_discord(
                config.discord_webhook,
                message.replace("•", "-"),
            )
            or delivered
        )

    github_body: str | None = None
    if (config.github_reports_enabled and config.token and config.repo_fullname) or (
        config.private_github_reports_repository
        and config.private_github_reports_token
        and dependencies.send_private_github_report is not None
    ):
        github_body = reporting.github_report_body(
            queue,
            now,
            verification_examples=paid_examples + strategic_examples,
            strategic_audit=strategic_audit,
            reject_counts=rejects,
            coverage_warning=coverage_warning,
        )

    if config.github_reports_enabled and config.token and config.repo_fullname:
        attempted = True
        assert github_body is not None
        delivered = (
            dependencies.send_github_report(
                config.repo_fullname,
                config.token,
                reporting.github_report_title(len(queue)),
                github_body,
            )
            or delivered
        )

    if config.private_github_reports_repository and config.private_github_reports_token:
        attempted = True
        if dependencies.send_private_github_report is not None:
            assert github_body is not None
            delivered = (
                dependencies.send_private_github_report(
                    config.private_github_reports_repository,
                    config.private_github_reports_token,
                    reporting.github_report_title(len(queue)),
                    github_body,
                )
                or delivered
            )

    return DeliveryResult(attempted=attempted, delivered=delivered)


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
    seen = seen_state.urls()
    repo_cache: dict[str, RepositoryMetadata] = {}
    guide_cache: dict[str, str | None] = {}

    started = monotonic()
    prefetched_paid_searches: list[SearchBatch] | None = None
    prefetched_global_searches: list[SearchBatch] | None = None
    if (
        config.token
        and config.repo_fullname
        and (
            config.preferences.paid
            or (config.preferences.strategic and config.preferences.global_search)
        )
    ):
        (
            prefetched_paid_searches,
            prefetched_global_searches,
        ) = dependencies.prefetch_discovery_searches(config.token)

    paid_started = monotonic()
    paid, paid_rejects, paid_examples = (
        dependencies.discover_paid(
            config.token, seen, repo_cache, guide_cache, prefetched_paid_searches
        )
        if config.preferences.paid
        else ([], {}, [])
    )
    paid_seconds = monotonic() - paid_started

    strategic_started = monotonic()
    (
        strategic,
        strategic_rejects,
        strategic_examples,
        strategic_audit,
    ) = (
        dependencies.discover_strategic(
            config.token,
            seen,
            {candidate["url"] for candidate in paid},
            repo_cache,
            guide_cache,
            prefetched_global_searches,
        )
        if config.preferences.strategic
        else ([], {}, [], [])
    )

    strategic_seconds = monotonic() - strategic_started
    print(
        "Scout performance: "
        f"paid={paid_seconds:.1f}s, strategic={strategic_seconds:.1f}s, "
        f"total={monotonic() - started:.1f}s"
    )

    queue = assemble_queue(
        [item for item in paid if selection.candidate_rejection(item, config.preferences) is None],
        [
            item
            for item in strategic
            if selection.candidate_rejection(item, config.preferences) is None
        ],
        limit=min(report_limit, config.preferences.max_results),
    )
    if strategic_audit:
        print("=== POTENTIAL SCANNER MISSES ===")
        for item in strategic_audit:
            print(f"- {item.get('url')}: {item['reason']}")

    rejects = reporting.rejection_summary(
        {
            reason: count
            for reason, count in paid_rejects.items()
            if not isinstance(reason, DiscoveryFailureReason)
        },
        strategic_rejects,
    )
    coverage = coverage_status(
        strategic_rejects,
        strategic_audit,
        paid_rejects=paid_rejects,
        warning_threshold=coverage_warning_threshold,
    )
    if coverage.warning is not None:
        print(f"WARNING: {coverage.warning}")

    if not queue and coverage.warning is None:
        state_saved = False
        if coverage.complete:
            print("No new verified OSS opportunities found.")
            maintenance = _maintain_seen_state(
                seen_state,
                scan_time,
                dependencies.issue_lifecycle,
            )
            if maintenance.checked_urls:
                state_saved = _save_state(maintenance.state, config.state_path)
        else:
            print("Verification coverage incomplete; state was not updated.")
        return CombinedRunResult(
            queue=(),
            coverage=coverage,
            delivery=DeliveryResult(attempted=False, delivered=False),
            state_saved=state_saved,
        )

    now = scan_time.strftime("%Y-%m-%d %H:%M UTC")
    delivery = _deliver(
        config,
        dependencies,
        queue,
        now,
        paid_examples=paid_examples,
        strategic_examples=strategic_examples,
        strategic_audit=strategic_audit,
        rejects=rejects,
        coverage_warning=coverage.warning,
    )

    if rejects:
        print(
            "Filtered verified candidates: "
            + ", ".join(f"{reason}={count}" for reason, count in sorted(rejects.items()))
        )

    state_saved = False
    if delivery.attempted and delivery.delivered and coverage.complete:
        maintenance = _maintain_seen_state(
            seen_state,
            scan_time,
            dependencies.issue_lifecycle,
        )
        next_state = maintenance.state
        next_state.mark_reported_many(
            (candidate["url"] for candidate in queue),
            reported_at=scan_time.isoformat().replace("+00:00", "Z"),
        )
        try:
            state_saved = _save_state(next_state, config.state_path)
        except state.SeenStateSaveError as error:
            raise PostDeliveryStateSaveError(str(error)) from error
    elif delivery.attempted and delivery.delivered:
        print("Verification coverage incomplete; state was not updated.")
    else:
        print("No notification was delivered; state was not updated.")

    return CombinedRunResult(
        queue=tuple(queue),
        coverage=coverage,
        delivery=delivery,
        state_saved=state_saved,
    )
