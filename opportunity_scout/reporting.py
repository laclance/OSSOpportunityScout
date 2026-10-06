"""Reporting and notification formatting for OSS Opportunity Scout.

The module turns ranked scanner records into Markdown reports and plain-text
notifications. It does not fetch source data, rank candidates, or deliver output.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Mapping, Sequence

from opportunity_scout.types import Candidate, RejectionRecord

_GITHUB_REPORT_URL = re.compile(
    r"https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)",
    flags=re.IGNORECASE,
)
_REPORT_INTRO = (
    "<!-- opportunity-scout-report: automated; actionable: false -->\n"
    "> [!IMPORTANT]\n"
    "> **Automated OSS Opportunity Scout scan report — not a development task.**\n"
    "> Do not claim this report or open a pull request to resolve it. "
    "The linked source issues are the actual contributor opportunities.\n\n"
)
_REPORT_CONTEXT = (
    "Paid candidates reuse OSS Opportunity Scout's existing payment/competition filters unchanged. "
    "Strategic candidates are pre-ranked, then source-refreshed and checked for assignees, "
    "claim comments, open implementation PRs, repository legitimacy, and contribution guidance. "
    "For strategic work, career score measures long-term value while priority score applies "
    "execution friction from effort and visible competition.\n\n"
)


def github_report_ref(text: object | None) -> str:
    """Render GitHub issue and pull-request URLs through the no-backlink redirector."""
    value = str(text or "")

    def redirected(match: re.Match[str]) -> str:
        owner, repo, kind, number = match.groups()
        return f"https://redirect.github.com/{owner}/{repo}/{kind}/{number}"

    return _GITHUB_REPORT_URL.sub(redirected, value)


def markdown_label(text: object | None) -> str:
    """Escape text that will be used as a Markdown link label."""
    value = github_report_ref(text)
    for source, replacement in (("\\", "\\\\"), ("[", "\\["), ("]", "\\]")):
        value = value.replace(source, replacement)
    return value


def strategic_priority_delta(candidate: Candidate) -> int:
    """Return the execution adjustment applied on top of career score."""
    return int(candidate["priority_score"]) - int(candidate["career_score"])


def _expected_hourly_text(candidate: Candidate) -> str:
    hourly = candidate["expected_hourly"]
    if hourly is None:
        return "unknown / not USD-comparable"
    return f"~${hourly:.0f}/h"


def _contribution_process_text(candidate: Candidate) -> str:
    guide = candidate["contribution_guide"]
    if not guide:
        return "not found at common paths"
    return f"[contribution guide]({guide})"


def _candidate_lane_fields(candidate: Candidate) -> list[tuple[str, str]]:
    if candidate["paid"]:
        return [
            ("Reward", str(candidate["reward"] or "unknown")),
            ("Payment confidence", f"{candidate['payment_confidence']}/100"),
            ("Cash score", f"{candidate['cash_score']}/100"),
            ("Effort", str(candidate["effort"])),
            ("Expected hourly value", _expected_hourly_text(candidate)),
        ]

    delta = strategic_priority_delta(candidate)
    delta_text = f"+{delta}" if delta >= 0 else str(delta)
    return [
        ("Career score", f"{candidate['career_score']}/100"),
        ("Priority score", f"{candidate['priority_score']}/100"),
        (
            "Execution adjustment",
            f"{delta_text} (career {candidate['career_score']} → "
            f"priority {candidate['priority_score']})",
        ),
        ("Effort", str(candidate["effort"])),
    ]


def _candidate_detail_fields(candidate: Candidate) -> list[tuple[str, str]]:
    fields = _candidate_lane_fields(candidate)

    effort_reasons = candidate.get("effort_reasons") or []
    if effort_reasons:
        fields.append(("Effort basis", ", ".join(str(reason) for reason in effort_reasons)))

    priority_reasons = candidate.get("priority_reasons") or []
    if priority_reasons and not candidate["paid"]:
        fields.append(("Priority basis", ", ".join(str(reason) for reason in priority_reasons)))

    labels = candidate.get("labels") or []
    fields.extend(
        [
            ("Competition", str(candidate["competition"])),
            ("Repo stars", str(candidate["stars"])),
            ("Repo recent activity", str(candidate["recent_activity"])),
            ("Language", str(candidate["language"])),
            ("Labels", ", ".join(str(label) for label in labels) or "none"),
            ("Contribution process", _contribution_process_text(candidate)),
        ]
    )

    if candidate["paid"]:
        reason_label = "Cash reasons"
        reasons = candidate["cash_reasons"]
    else:
        reason_label = "Career reasons"
        reasons = candidate["career_reasons"]
    fields.append((reason_label, ", ".join(str(reason) for reason in reasons)))
    return fields


def markdown_candidate(candidate: Candidate, idx: int) -> str:
    """Render one paid or strategic candidate for the GitHub queue issue."""
    heading = (
        f"#### {idx}. [{markdown_label(candidate.get('repo'))} #{candidate['issue_number']}]"
        f"({github_report_ref(candidate['url'])}): {markdown_label(candidate.get('title'))}"
    )
    details = "\n".join(
        f"- **{label}:** {value}" for label, value in _candidate_detail_fields(candidate)
    )
    return f"{heading}\n{details}\n\n"


def notification_candidate(candidate: Candidate, idx: int) -> list[str]:
    """Render one concise plain-text notification entry."""
    title = str(candidate["title"] or "")
    if len(title) > 100:
        title = title[:97] + "..."

    score_line = (
        f"   • paid bounty | reward: {candidate['reward'] or 'unknown'} | "
        f"cash: {candidate['cash_score']}/100"
        if candidate["paid"]
        else f"   • strategic OSS | career: {candidate['career_score']}/100 | "
        f"priority: {candidate['priority_score']}/100"
    )
    return [
        f"{idx}. {candidate['repo']} #{candidate['issue_number']} — {title}",
        score_line,
        f"   • {candidate['effort']} | competition: {candidate['competition']}",
        f"   • {candidate['url']}",
    ]


def notification_message(
    queue: Sequence[Candidate],
    now: str,
    *,
    warning: str | None = None,
    max_chars: int = 1900,
) -> str:
    """Render a notification that stays within the stricter Discord text budget."""
    message = f"🎯 OSS Opportunity Queue ({now})\n\n"
    if warning:
        message += f"⚠️ {warning}\n\n"

    accepted: list[str] = []
    accepted_length = 0
    omitted = 0
    for idx, candidate in enumerate(queue, 1):
        block = "\n".join(notification_candidate(candidate, idx)) + "\n\n"
        if len(message) + accepted_length + len(block) <= max_chars:
            accepted.append(block)
            accepted_length += len(block)
            continue
        omitted = len(queue) - idx + 1
        break

    message += "".join(accepted)
    if omitted:
        suffix = f"… {omitted} more ranked candidate(s) omitted from this notification."
        room = max_chars - len(suffix) - 1
        message = message[: max(0, room)].rstrip() + "\n" + suffix
    return message.rstrip()


def _reason_summary(counts: Mapping[str, int], *, limit: int = 6) -> str:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return "; ".join(f"{reason} ×{count}" for reason, count in ranked[:limit])


def rejection_summary(
    paid_rejects: Mapping[str, int],
    strategic_rejects: Mapping[str, int],
) -> dict[str, int]:
    """Combine paid and strategic rejection counts for report assembly."""
    combined = Counter(paid_rejects)
    combined.update(strategic_rejects)
    return dict(combined)


def _linked_record(item: RejectionRecord) -> str:
    source = github_report_ref(item.get("url"))
    title = markdown_label(item.get("title") or source)
    reason = github_report_ref(item.get("reason"))
    return f"- [{title}]({source}): {reason}"


def markdown_examples(
    heading: str,
    examples: Sequence[RejectionRecord],
    *,
    limit: int = 12,
) -> str:
    """Render linked rejection or audit examples without creating source backlinks."""
    if not examples:
        return ""
    rows = [_linked_record(item) for item in examples[:limit]]
    return "\n".join([f"### {heading}", "", *rows]) + "\n"


def audit_summary(audit: Sequence[RejectionRecord]) -> str:
    """Summarize recurring tuning signals before concrete examples."""
    if not audit:
        return ""
    counts = Counter(str(item.get("reason") or "unknown audit reason") for item in audit)
    return _reason_summary(counts)


def _report_header(now: str) -> str:
    return (
        _REPORT_INTRO
        + f"### Ranked OSS Opportunity Queue\n\n**Scan Time:** {now}\n\n"
        + _REPORT_CONTEXT
    )


def github_report_body(
    queue: Sequence[Candidate],
    now: str,
    *,
    verification_examples: Sequence[RejectionRecord] = (),
    strategic_audit: Sequence[RejectionRecord] = (),
    reject_counts: Mapping[str, int] | None = None,
    coverage_warning: str | None = None,
) -> str:
    """Render the complete GitHub queue report."""
    sections = [_report_header(now)]

    if coverage_warning:
        sections.append(f"> [!WARNING]\n> {coverage_warning}\n\n")

    sections.extend(markdown_candidate(candidate, idx) for idx, candidate in enumerate(queue, 1))

    if reject_counts:
        sections.append(
            "### Verification summary\n\n"
            f"**Filtered candidates:** {sum(reject_counts.values())}\n\n"
            f"**Top rejection reasons:** {_reason_summary(reject_counts)}\n\n"
        )

    sections.append(markdown_examples("Verification rejects", verification_examples))

    if strategic_audit:
        sections.append("\n### Potential scanner misses / tuning candidates\n\n")
        sections.append(f"**Audit summary:** {audit_summary(strategic_audit)}\n\n")
        sections.extend(f"{_linked_record(item)}\n" for item in strategic_audit[:12])

    return "".join(sections)


def github_report_title(queue_size: int) -> str:
    """Return the GitHub issue title for a queue run."""
    candidate_word = "candidate" if queue_size == 1 else "candidates"
    return f"📊 SCAN REPORT — OSS Opportunity Queue: {queue_size} new verified {candidate_word}"
