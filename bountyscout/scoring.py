"""Opportunity scoring and effort estimation.

Owns ranking math and implementation-effort heuristics over already-fetched evidence.
No network I/O belongs in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Collection, cast

from bountyscout import github
from bountyscout.strategic.readiness import TRUSTED_ASSOCIATIONS
from bountyscout.types import (
    Candidate,
    CandidateLane,
    CompetitionLevel,
    EffortBucket,
    GitHubComment,
    GitHubIssue,
    RepositoryMetadata,
)


def maintainer_ready_signal(labels_text: str) -> bool:
    """Recognize common contributor-ready label dialects."""
    normalized = re.sub(r"[-_]+", " ", labels_text.lower())
    return any(
        marker in normalized
        for marker in (
            "help wanted",
            "good first issue",
            "triage/accepted",
            "refined",
        )
    )


def issue_text(item: GitHubIssue) -> tuple[str, str, str, str]:
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = " ".join(
        str(x.get("name", "")) if isinstance(x, dict) else str(x)
        for x in (item.get("labels") or [])
    ).lower()
    return title, body, labels, f"{title}\n{body}".lower()


CODE_FILE_RE = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:go|sh|py|yaml|yml)\b",
    re.IGNORECASE,
)


def code_reference_count(text: str) -> int:
    return len({match.group(0).lower() for match in CODE_FILE_RE.finditer(text)})


def documentation_microfix(item: GitHubIssue) -> bool:
    """Detect explicitly bounded docs edits, not incidental docs wording."""
    title, body, labels, text = issue_text(item)
    title_and_labels = f"{title}\n{labels}"

    docs_context = bool(
        re.search(
            r"\b(?:docs?|documentation|readme)\b",
            title_and_labels,
            re.IGNORECASE,
        )
    )
    micro_pattern = (
        r"\b(?:typo|spelling|broken\s+(?:(?:docs?|documentation|readme)\s+)?"
        r"(?:link|image)|(?:link|image)\s+fix|documentation cleanup|docs cleanup)\b"
    )
    title_micro_signal = bool(re.search(micro_pattern, title, re.IGNORECASE))
    bounded_body_docs_signal = bool(
        re.search(
            r"\b(?:fix|correct|repair)\b[^\n.!?]{0,100}"
            r"\b(?:typo|spelling|broken\s+(?:(?:docs?|documentation|readme)\s+)?"
            r"(?:link|image))\b[^\n.!?]{0,100}"
            r"\b(?:docs?|documentation|readme)\b|"
            r"\b(?:docs?|documentation|readme)\b[^\n.!?]{0,100}"
            r"\b(?:fix|correct|repair)\b[^\n.!?]{0,100}"
            r"\b(?:typo|spelling|broken\s+(?:link|image))\b",
            body,
            re.IGNORECASE,
        )
    )
    bounded_micro_signal = bool(
        (docs_context and title_micro_signal)
        or (
            docs_context
            and re.search(
                r"\b(?:fix|correct|repair)\b[^\n.!?]{0,100}" + micro_pattern,
                body,
                re.IGNORECASE,
            )
        )
        or bounded_body_docs_signal
    )

    implementation_scope = bool(
        re.search(
            r"\b(?:add|implement|change|modify|extend)\s+(?:the\s+)?"
            r"(?:validator|parser|runtime|validation logic|parsing logic)\b|"
            r"\b(?:validator|parser|runtime)\s+(?:logic|code|behavior)\b|"
            r"\bparse\s+(?:a\s+|the\s+)?json\b|"
            r"\bnormaliz\w*\s+(?:the\s+)?(?:urls?|destinations?)\b|"
            r"\bstructured validation errors?\b|"
            r"\b(?:data|schema) migration\b",
            text,
            re.IGNORECASE,
        )
    )

    return bounded_micro_signal and not implementation_scope and code_reference_count(body) == 0


def _prose_body(body: str) -> str:
    """Remove fenced diagnostics/code so dump size does not masquerade as implementation scope."""
    fenced = re.compile(r"\x60{3}.*?\x60{3}|~~~.*?~~~", re.DOTALL)
    return re.sub(r"\s+", " ", fenced.sub(" ", body)).strip()


def _normalized_labels(labels: str) -> str:
    return re.sub(r"[-_/:]+", " ", labels.lower())


def _feature_signal(title: str, labels: str, text: str) -> bool:
    normalized_labels = _normalized_labels(labels)
    return bool(
        title.lower().startswith(("fr:", "feature request:"))
        or any(
            marker in normalized_labels
            for marker in (
                "kind feature",
                "type feature",
                "feature request",
                "enhancement",
            )
        )
        or re.search(r"\bfeature request\b", text)
    )


def _cross_component_feature(text: str, file_refs: int) -> bool:
    """Identify features that span several persistence/configuration/runtime concerns."""
    if file_refs >= 3:
        return True
    component_terms = (
        "schema",
        "storage",
        "bucket",
        "chunks",
        "index",
        "compactor",
        "configuration",
        "protocol",
        "wire format",
        "database",
        "migration",
        "snapshot",
        "wal",
        "handshake",
    )
    return sum(term in text for term in component_terms) >= 3


def _trusted_history_complexity(
    activity_comments: Collection[GitHubComment] | None,
) -> bool:
    """Recognize maintainer-confirmed complexity exposed by earlier implementation work."""
    trusted_history = "\n".join(
        str(comment.get("body") or "")
        for comment in activity_comments or ()
        if str(comment.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS
    ).lower()
    if not trusted_history:
        return False

    historical_scope = bool(
        re.search(
            r"\b(?:quite a big job|major work|structural changes?|"
            r"needs? (?:the )?code rewritten|rewrite(?:n|ing)? in streaming style)\b",
            trusted_history,
        )
    )
    concern_patterns = (
        r"\b(?:cancellation|cancelled|canceling|cancelling|goroutines?|"
        r"buffering|timeouts?|resource lifecycle)\b",
        r"\b(?:regression tests?|unit tests?|integration tests?|"
        r"benchmarks?|benchmarking|test semantics)\b",
        r"\b(?:streaming style|nested (?:calls?|parsing)|json parser|"
        r"json unmarshal|unmarshal)\b",
        r"\b(?:backwards? compatibility|public api|deprecat\w*|v2)\b",
    )
    technical_concerns = sum(
        bool(re.search(pattern, trusted_history)) for pattern in concern_patterns
    )
    if historical_scope and technical_concerns:
        return True

    prior_attempt_context = bool(
        re.search(
            r"\b(?:previous|prior|attempt|implementation|pull request|"
            r"this work|this change)\b",
            trusted_history,
        )
    )
    return prior_attempt_context and technical_concerns >= 2


@dataclass(frozen=True)
class _EffortContext:
    """Normalized source evidence used by ordered effort rules."""

    title: str
    body: str
    labels: str
    text: str
    prose: str
    file_refs: int
    feature: bool
    normalized_labels: str
    docs_signal: bool
    feature_request_scope: bool
    docs_feature_implementation_scope: bool
    bounded_docs_feature_request: bool


@dataclass(frozen=True)
class EffortEstimate:
    """Bucketed implementation estimate plus concise calibration reasons."""

    bucket: EffortBucket
    reasons: tuple[str, ...]


def _effort_context(item: GitHubIssue) -> _EffortContext:
    """Normalize source-derived evidence once for ordered effort rules."""
    title, body, labels, text = issue_text(item)
    prose = _prose_body(body)
    file_refs = code_reference_count(body)
    feature = _feature_signal(title, labels, text)
    normalized_labels = _normalized_labels(labels)
    docs_signal = bool(
        re.search(
            r"\b(?:docs?|documentation|readme)\b",
            f"{title}\n{labels}",
            re.IGNORECASE,
        )
    )
    feature_request_scope = bool(
        title.lower().startswith(("fr:", "feature request:"))
        or "feature request" in normalized_labels
    )
    docs_feature_implementation_scope = bool(
        re.search(
            r"\b(?:add|implement|change|modify|extend|refactor|rewrite)\s+(?:the\s+)?"
            r"(?:validator|parser|runtime|protocol|subsystem|architecture|api|server|client)\b|"
            r"\b(?:validator|parser|runtime|protocol)\s+(?:logic|code|behavior)\b",
            text,
            re.IGNORECASE,
        )
        or _cross_component_feature(text, file_refs)
    )
    bounded_docs_feature_request = bool(
        feature_request_scope and docs_signal and not docs_feature_implementation_scope
    )
    return _EffortContext(
        title=title,
        body=body,
        labels=labels,
        text=text,
        prose=prose,
        file_refs=file_refs,
        feature=feature,
        normalized_labels=normalized_labels,
        docs_signal=docs_signal,
        feature_request_scope=feature_request_scope,
        docs_feature_implementation_scope=docs_feature_implementation_scope,
        bounded_docs_feature_request=bounded_docs_feature_request,
    )


def _large_scope_effort(ctx: _EffortContext) -> EffortEstimate | None:
    """Apply the ordered 1d+ scope and investigation rules."""
    explicit_large_scope = bool(
        re.search(
            r"\b(?:epic|roadmap|redesign|rewrite|multi-phase|"
            r"architecture (?:redesign|rewrite|overhaul|refactor|change)|"
            r"architectural (?:redesign|rewrite|overhaul|refactor|change)|"
            r"large refactor|rfc|connection pool|explore publishing)\b",
            ctx.text,
        )
        or (ctx.feature_request_scope and not ctx.bounded_docs_feature_request)
    )
    compatibility_risk = bool(
        re.search(
            r"\b(?:backward[- ]incompatible|backwards? compatibility|"
            r"compatibility (?:risk|break|constraint)|persisted (?:state|data)|"
            r"existing deployments?|wire format|on-disk format)\b",
            ctx.text,
        )
        or (
            re.search(r"\b(?:wal|snapshot|handshake)\b", ctx.text)
            and re.search(
                r"\b(?:persist|compatib|existing cluster|rejoin|recover)\w*\b",
                ctx.text,
            )
        )
    )
    environment_heavy = bool(
        re.search(
            r"\b(?:dual[ -]?sim|physical device|device-specific|hardware-dependent)\b",
            ctx.text,
        )
        or re.search(
            r"\b(?:unable to reproduce|cannot reproduce|can't reproduce|"
            r"haven't been able to reproduce|have not been able to reproduce|"
            r"low-probability race|non[- ]deterministic repro)\b",
            ctx.text,
        )
    )

    if explicit_large_scope:
        return EffortEstimate("1d+", ("explicit broad feature/design scope",))
    if compatibility_risk:
        return EffortEstimate(
            "1d+",
            ("backward-compatibility or persisted-state risk",),
        )
    if environment_heavy:
        return EffortEstimate(
            "1d+",
            ("environment/reproduction-heavy investigation",),
        )
    if ctx.feature and _cross_component_feature(ctx.text, ctx.file_refs):
        return EffortEstimate(
            "1d+",
            ("feature spans multiple runtime/configuration components",),
        )
    if len(ctx.prose) > 12000:
        return EffortEstimate("1d+", ("large narrative implementation scope",))
    return None


def _documentation_effort(ctx: _EffortContext) -> EffortEstimate | None:
    """Apply ordered documentation-specific effort rules."""
    broad_docs = bool(
        ctx.docs_signal
        and (
            re.search(
                r"\b(?:all|every|each)\s+(?:the\s+)?(?:grpc\s+)?services?\b|"
                r"\b(?:generated?|generate)\s+(?:docs?|documentation)\b|"
                r"\bdocs?\s+(?:generated|generation)\b|"
                r"\bhost(?:ed|ing)?\s+(?:them\s+)?on\s+(?:the\s+)?website\b|"
                r"\ball\s+in\s+one\s+place\b",
                ctx.text,
            )
        )
    )
    if broad_docs:
        return EffortEstimate(
            "6–12h",
            ("cross-service documentation/generation scope",),
        )
    if ctx.docs_signal and ctx.file_refs == 0 and len(ctx.prose) < 4500:
        return EffortEstimate("1–3h", ("bounded documentation change",))
    return None


def _broader_implementation_effort(
    ctx: _EffortContext,
) -> EffortEstimate | None:
    """Apply the general broader-implementation rule and ordered reasons."""
    suggested_fix_bullets = len(re.findall(r"(?m)^\s*-\s+", ctx.body))
    mobile_or_desktop = any(
        marker in ctx.labels.lower()
        for marker in ("os-android", "os-ios", "os-macos", "os-windows")
    )
    missing_reproduction = "_no response_" in ctx.text or "no response" in ctx.text

    if not (
        ctx.file_refs >= 4
        or ctx.feature
        or len(ctx.prose) > 6500
        or (mobile_or_desktop and missing_reproduction)
        or (re.search(r"\bsuggested fix(?:es)?\b", ctx.text) and suggested_fix_bullets >= 3)
    ):
        return None

    reasons: list[str] = []
    if ctx.feature:
        reasons.append("feature/enhancement scope")
    if ctx.file_refs >= 4:
        reasons.append("multiple referenced files")
    if len(ctx.prose) > 6500:
        reasons.append("large narrative scope")
    if suggested_fix_bullets >= 3:
        reasons.append("multi-step suggested implementation")
    if mobile_or_desktop and missing_reproduction:
        reasons.append("platform-specific reproduction is missing")
    return EffortEstimate(
        "6–12h",
        tuple(reasons[:3]) or ("broader implementation scope",),
    )


def _history_or_investigation_effort(
    ctx: _EffortContext,
    activity_comments: Collection[GitHubComment] | None,
) -> EffortEstimate | None:
    """Apply trusted-history, concurrency, and upstream investigation rules."""
    if _trusted_history_complexity(activity_comments):
        return EffortEstimate(
            "6–12h",
            ("maintainer-confirmed implementation-history complexity",),
        )

    concurrency_risk = bool(
        re.search(r"\b(?:data race|race condition|deadlock|concurren\w*)\b", ctx.text)
    )
    if concurrency_risk:
        return EffortEstimate("3–6h", ("concurrency/lifecycle debugging risk",))

    upstream_dependency = bool(
        re.search(
            r"\b(?:may be related to|upstream (?:issue|dependency)|"
            r"vendor(?:ed)? dependency|third[- ]party dependency)\b",
            ctx.text,
        )
    )
    if upstream_dependency:
        return EffortEstimate("3–6h", ("upstream/dependency investigation",))
    return None


def _bounded_effort(ctx: _EffortContext) -> EffortEstimate | None:
    """Apply localized TODO and bounded deterministic rules."""
    localized_todo = bool(
        ctx.file_refs <= 2
        and re.search(r"\btodo\b", ctx.text)
        and re.search(
            r"\b(?:method|function|handler|header|path|codebase)\b",
            ctx.text,
        )
    )
    bounded = bool(
        re.search(
            r"\b(?:regression|deterministic|panics?|segfault|nil pointer|"
            r"leaks?|incorrect|failing tests?|unit tests?|single|small|narrow|"
            r"no-op|stale)\b|\bnever closes\b|\bevery sync\b",
            f"{ctx.title.lower()} {ctx.labels} {ctx.text[:4500]}",
        )
    )
    if (bounded or localized_todo) and len(ctx.prose) < 4500 and ctx.file_refs <= 2:
        reason = (
            "localized TODO/code-path change"
            if localized_todo
            else "bounded deterministic bug signal"
        )
        return EffortEstimate("1–3h", (reason,))
    return None


def estimate_effort_details(
    item: GitHubIssue,
    activity_comments: Collection[GitHubComment] | None = None,
) -> EffortEstimate:
    """Estimate implementation effort from source text and already-fetched discussion."""
    ctx = _effort_context(item)

    if documentation_microfix(item):
        return EffortEstimate("<1h", ("documentation-only micro-fix",))

    estimate = _large_scope_effort(ctx)
    if estimate is not None:
        return estimate

    estimate = _documentation_effort(ctx)
    if estimate is not None:
        return estimate

    estimate = _broader_implementation_effort(ctx)
    if estimate is not None:
        return estimate

    estimate = _history_or_investigation_effort(ctx, activity_comments)
    if estimate is not None:
        return estimate

    estimate = _bounded_effort(ctx)
    if estimate is not None:
        return estimate

    return EffortEstimate("3–6h", ("moderate implementation scope",))


def estimate_effort(item: GitHubIssue) -> EffortBucket:
    """Return the public effort bucket for an issue."""
    return estimate_effort_details(item).bucket


def effort_hours(effort: EffortBucket) -> float:
    return {
        "<1h": 0.75,
        "1–3h": 2.0,
        "3–6h": 4.5,
        "6–12h": 9.0,
        "1d+": 16.0,
    }[effort]


LIFECYCLE_HOUSEKEEPING_BOTS = {"k8s-triage-robot", "k8s-ci-robot"}
LIFECYCLE_ADMIN_COMMAND_RE = re.compile(
    r"/(?:remove-lifecycle\s+(?:stale|rotten)|"
    r"lifecycle\s+(?:stale|rotten|frozen)|"
    r"(?:remove-)?label\s+\S.*)",
    re.IGNORECASE,
)


def comment_contributes_to_competition(comment: GitHubComment) -> bool:
    """Return whether a comment is substantive enough to count as competition."""
    body = str(comment.get("body") or "").strip()
    if not body:
        return True

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and all(LIFECYCLE_ADMIN_COMMAND_RE.fullmatch(line) for line in lines):
        return False

    login = str((comment.get("user") or {}).get("login", "")).lower()
    bot_author = login.endswith("[bot]") or login in LIFECYCLE_HOUSEKEEPING_BOTS
    if not bot_author:
        return True

    lowered = body.lower()
    lifecycle_notice = (
        "this bot triages" in lowered
        or "automatically marked as stale" in lowered
        or "automatically marked as rotten" in lowered
        or "closed due to inactivity" in lowered
        or "due to inactivity" in lowered
        or (
            login in LIFECYCLE_HOUSEKEEPING_BOTS
            and re.search(
                r"(?:lifecycle/(?:stale|rotten)|"
                r"/lifecycle\s+(?:stale|rotten)|"
                r"/remove-lifecycle\s+(?:stale|rotten))",
                lowered,
            )
        )
    )
    return not bool(lifecycle_notice)


def competition(
    item: GitHubIssue,
    activity_comments: Collection[GitHubComment] | None = None,
) -> CompetitionLevel:
    comments = (
        sum(comment_contributes_to_competition(comment) for comment in activity_comments)
        if activity_comments is not None
        else int(item.get("comments") or 0)
    )
    if comments == 0:
        return "none"
    if comments <= 3:
        return "low"
    if comments <= 8:
        return "medium"
    return "high"


def payment_confidence(signal: str | None) -> int:
    if not signal:
        return 0
    if signal.startswith("confirmed bounty platform"):
        return 100
    if signal.startswith("explicit bounty command"):
        return 100
    if signal.startswith("explicit /reward comment"):
        return 98
    if signal.startswith("explicit /bounty comment"):
        return 98
    if signal.startswith("bounty labels"):
        return 95
    if signal.startswith("named bounty platform"):
        return 90
    return 85


def usd_like_amount_from_signal(signal: str | None) -> float | None:
    """Extract a USD-like amount from a payment signal when comparable."""
    if not signal:
        return None

    dollar = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", signal)
    if dollar:
        return float(dollar.group(1).replace(",", ""))

    currency = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:usd|usdc|usdt)\b",
        signal,
        re.IGNORECASE,
    )
    if currency:
        return float(currency.group(1).replace(",", ""))

    return None


def reward_text(signal: str | None, amount_pattern: str) -> str | None:
    if not signal:
        return None
    match = re.search(amount_pattern, signal, re.IGNORECASE)
    return match.group(0).strip() if match else None


def repo_activity(repo_meta: RepositoryMetadata) -> str:
    pushed = github.parse_github_datetime(repo_meta.get("pushed_at"))
    if not pushed:
        return "unknown"
    days = max(0, (datetime.now(timezone.utc) - pushed).days)
    if days <= 7:
        bucket = "active in last 7d"
    elif days <= 30:
        bucket = "active in last 30d"
    elif days <= 90:
        bucket = "active in last 90d"
    else:
        bucket = f"last push {days}d ago"
    return f"{bucket} ({repo_meta.get('pushed_at')})"


def strategic_priority_score(
    career_score: int,
    effort: EffortBucket,
    competition_level: CompetitionLevel,
) -> tuple[int, list[str]]:
    """Turn career value into actionable priority using execution friction.

    Career score remains the long-term value signal. Priority answers the more
    practical question: given similarly valuable issues, which one is the best
    use of contributor time right now?
    """
    effort_adjustment = {
        "<1h": 5,
        "1–3h": 6,
        "3–6h": 2,
        "6–12h": -4,
        "1d+": -10,
    }[effort]
    competition_adjustment = {
        "none": 7,
        "low": 3,
        "medium": -4,
        "high": -10,
    }[competition_level]

    reasons: list[str] = []
    if effort_adjustment > 0:
        reasons.append(f"{effort} execution bonus")
    else:
        reasons.append(f"{effort} execution penalty")

    if competition_level == "none":
        reasons.append("no visible competition bonus")
    elif competition_adjustment > 0:
        reasons.append(f"{competition_level} competition bonus")
    else:
        reasons.append(f"{competition_level} competition penalty")

    priority = max(
        0,
        min(100, career_score + effort_adjustment + competition_adjustment),
    )
    return priority, reasons


def _paid_cash_score(
    signal: str | None,
    effort: EffortBucket,
    competition_level: CompetitionLevel,
    stars: int,
    active_30d: bool,
) -> tuple[int, float | None, list[str]]:
    cash = 0
    cash_reasons: list[str] = []
    amount = usd_like_amount_from_signal(signal)
    hourly: float | None = None

    confidence = payment_confidence(signal)
    cash += round(confidence * 0.30)
    cash_reasons.append(f"payment confidence {confidence}/100")
    if amount is not None:
        cash += (
            20
            if amount >= 500
            else 16
            if amount >= 100
            else 12
            if amount >= 25
            else 8
            if amount >= 5
            else 4
        )
        hourly = amount / effort_hours(effort)
        cash += (
            25
            if hourly >= 100
            else 21
            if hourly >= 50
            else 16
            if hourly >= 20
            else 10
            if hourly >= 10
            else 4
        )
        cash_reasons.append("~$" + f"{hourly:.0f}/h expected value")
    else:
        cash_reasons.append("reward not USD-comparable")

    cash += {"none": 15, "low": 11, "medium": 6, "high": 0}[competition_level]
    if stars >= 1000:
        cash += 7
        cash_reasons.append("established repo")
    elif stars >= 100:
        cash += 4
    if active_30d:
        cash += 3

    return max(0, min(100, cash)), hourly, cash_reasons


def _base_career_score(
    item: GitHubIssue,
    repo: str | None,
    repo_meta: RepositoryMetadata,
    guide: str | None,
    effort: EffortBucket,
    competition_level: CompetitionLevel,
    stars: int,
    active_30d: bool,
    labels_text: str,
    text: str,
    *,
    target_repos: Collection[str],
) -> tuple[int, list[str], bool, str]:
    career = 0
    career_reasons: list[str] = []

    if stars >= 10000:
        career += 18
        career_reasons.append("10k+ star repo")
    elif stars >= 1000:
        career += 14
        career_reasons.append("1k+ star repo")
    elif stars >= 100:
        career += 9
    elif stars >= 10:
        career += 4
    if active_30d:
        career += 8
        career_reasons.append("repo active in last 30d")
    if repo in target_repos:
        career += 14
        career_reasons.append("target repo bonus")

    language = str(repo_meta.get("language") or "Unknown")
    lang = language.lower()
    skill = (
        12
        if lang == "go"
        else 11
        if lang in ("typescript", "javascript")
        else 9
        if lang in ("ruby", "php")
        else 8
        if lang == "hcl"
        else 0
    )
    if skill:
        career_reasons.append(f"{language} codebase")

    infra_terms = (
        "kubernetes",
        "aws",
        "network",
        "dns",
        "proxy",
        "routing",
        "observability",
        "prometheus",
        "otel",
        "distributed",
        "controller",
        "terraform",
        "gitops",
        "backend",
        "concurrency",
    )
    api_domain_signal = bool(
        re.search(
            r"\b(?:http|rest|grpc|kubernetes|cloud|provider|server|backend)\s+api\b|"
            r"\bapi\s+(?:server|gateway|endpoint|client)\b",
            text,
        )
    )
    if any(term in text for term in infra_terms) or api_domain_signal:
        skill += 6
        career_reasons.append("target infrastructure/domain fit")
    career += min(18, skill)

    depth_terms = (
        "race",
        "deadlock",
        "concurrency",
        "network",
        "dns",
        "proxy",
        "routing",
        "protocol",
        "controller",
        "distributed",
        "storage",
        "performance",
        "memory",
        "leak",
        "api",
    )
    depth = sum(term in text for term in depth_terms)
    career += (
        14
        if depth >= 3
        else 8
        if depth >= 1
        else 4
        if re.search(r"\b(?:test|regression|bug|fix)\b", text)
        else 0
    )
    if depth:
        career_reasons.append("meaningful technical depth")

    issue_points = 0
    if re.search(r"\b(?:test|tests|regression)\b", text):
        issue_points += 6
        career_reasons.append("tests/regression signal")

    maintainer_ready = maintainer_ready_signal(labels_text)
    if maintainer_ready:
        issue_points += 8
        career_reasons.append("maintainer-ready signal")

    if guide:
        issue_points += 3
        career_reasons.append("contribution guide found")

    effort_points = {
        "<1h": 9,
        "1–3h": 7,
        "3–6h": 4,
        "6–12h": 1,
        "1d+": 0,
    }[effort]
    issue_points += effort_points
    if effort in ("<1h", "1–3h"):
        career_reasons.append("bounded implementation scope")

    _, body, _, _ = issue_text(item)
    clarity = 0
    if re.search(r"\b(?:root cause|code path|cause \(from)\b", text):
        clarity += 3
    if "steps to reproduce" in text and "_no response_" not in text:
        clarity += 2
    if re.search(r"\b(?:suggested fix|possible fix|expected behavior)\b", text):
        clarity += 2
    refs = code_reference_count(body)
    clarity += min(3, refs)
    if clarity >= 5:
        career_reasons.append("clear implementation/reproduction detail")
    elif clarity:
        career_reasons.append("implementation detail available")
    issue_points += min(10, clarity)

    career += min(30, issue_points)
    career -= {"none": 0, "low": 2, "medium": 6, "high": 12}[competition_level]
    if effort == "6–12h":
        career -= 3
        career_reasons.append("broader implementation scope")
    elif effort == "1d+":
        career -= 10
        career_reasons.append("large-scope penalty")

    return career, career_reasons, maintainer_ready, language


def _strategic_activity_adjustment(
    item: GitHubIssue,
    activity_comments: list[GitHubComment] | None,
    maintainer_ready: bool,
) -> tuple[int, list[str]]:
    now = datetime.now(timezone.utc)
    created = github.parse_github_datetime(item.get("created_at"))
    updated = github.parse_github_datetime(item.get("updated_at"))
    created_days = max(0, (now - created).days) if created else None

    recent_comment_days: int | None = None
    recent_maintainer_days: int | None = None
    latest_bot_comment: datetime | None = None
    latest_human_comment: datetime | None = None
    for comment in activity_comments or []:
        stamp = github.parse_github_datetime(comment.get("updated_at") or comment.get("created_at"))
        if not stamp:
            continue

        login = str((comment.get("user") or {}).get("login", "")).lower()
        if login.endswith("[bot]"):
            if latest_bot_comment is None or stamp > latest_bot_comment:
                latest_bot_comment = stamp
            continue

        if latest_human_comment is None or stamp > latest_human_comment:
            latest_human_comment = stamp
        days = max(0, (now - stamp).days)
        if recent_comment_days is None or days < recent_comment_days:
            recent_comment_days = days
        association = str(comment.get("author_association", "")).upper()
        if association in TRUSTED_ASSOCIATIONS and (
            recent_maintainer_days is None or days < recent_maintainer_days
        ):
            recent_maintainer_days = days

    effective_updated = updated
    if (
        updated is not None
        and latest_bot_comment is not None
        and abs((updated - latest_bot_comment).total_seconds()) <= 300
        and (latest_human_comment is None or latest_human_comment < latest_bot_comment)
    ):
        effective_updated = latest_human_comment or created
    updated_days = max(0, (now - effective_updated).days) if effective_updated else None

    adjustment = 0
    reasons: list[str] = []
    if updated_days is not None:
        if updated_days <= 14:
            adjustment += 8
            reasons.append("issue active in last 14d")
        elif updated_days <= 60:
            adjustment += 5
            reasons.append("issue active in last 60d")
        elif updated_days <= 180:
            adjustment += 2
            reasons.append("issue active in last 180d")

    if recent_maintainer_days is not None and recent_maintainer_days <= 90:
        adjustment += 8
        reasons.append("recent maintainer activity")
    elif recent_comment_days is not None and recent_comment_days <= 30:
        adjustment += 4
        reasons.append("recent active discussion")

    recently_active = bool(
        (updated_days is not None and updated_days <= 180)
        or (recent_comment_days is not None and recent_comment_days <= 90)
        or (recent_maintainer_days is not None and recent_maintainer_days <= 180)
    )
    if created_days is not None and not maintainer_ready and not recently_active:
        if created_days > 730:
            adjustment -= 15
            reasons.append("stale inactive backlog penalty")
        elif created_days > 365:
            adjustment -= 8
            reasons.append("older inactive backlog penalty")

    return adjustment, reasons


def build_candidate(
    item: GitHubIssue,
    lane: CandidateLane,
    signal: str | None,
    repo_meta: RepositoryMetadata,
    guide: str | None,
    activity_comments: list[GitHubComment] | None = None,
    *,
    target_repos: Collection[str],
    amount_pattern: str,
) -> Candidate:
    repo, number = github.issue_repo_and_number(item)
    effort_estimate = estimate_effort_details(
        item,
        activity_comments if lane == "strategic" else None,
    )
    effort = effort_estimate.bucket
    competition_level = competition(item, activity_comments if lane == "strategic" else None)
    stars = int(repo_meta.get("stargazers_count") or 0)
    pushed = github.parse_github_datetime(repo_meta.get("pushed_at"))
    active_30d = bool(pushed and (datetime.now(timezone.utc) - pushed).days <= 30)
    _, _, labels_text, text = issue_text(item)

    if lane == "paid":
        cash, hourly, cash_reasons = _paid_cash_score(
            signal,
            effort,
            competition_level,
            stars,
            active_30d,
        )
    else:
        cash = 0
        hourly = None
        cash_reasons = []

    career, career_reasons, maintainer_ready, language = _base_career_score(
        item,
        repo,
        repo_meta,
        guide,
        effort,
        competition_level,
        stars,
        active_30d,
        labels_text,
        text,
        target_repos=target_repos,
    )

    if lane == "strategic":
        activity_adjustment, activity_reasons = _strategic_activity_adjustment(
            item,
            activity_comments,
            maintainer_ready,
        )
        career += activity_adjustment
        career_reasons.extend(activity_reasons)

    if lane == "strategic" and documentation_microfix(item):
        career = min(career, 45)
        career_reasons.insert(0, "documentation-only micro-fix cap")

    career = max(0, min(100, career))

    if lane == "strategic":
        priority, priority_reasons = strategic_priority_score(
            career,
            effort,
            competition_level,
        )
    else:
        priority = min(100, max(cash, career) + (5 if cash >= 70 and career >= 70 else 0))
        priority_reasons = []

    labels = [
        str(x.get("name", "")) if isinstance(x, dict) else str(x)
        for x in (item.get("labels") or [])
    ]
    return {
        "repo": cast(str, repo),
        "issue_number": cast(int, number),
        "title": item.get("title"),
        "url": cast(str, item.get("html_url")),
        "paid": lane == "paid",
        "reward": reward_text(signal, amount_pattern),
        "payment_confidence": payment_confidence(signal),
        "cash_score": cash,
        "career_score": career,
        "priority_score": priority,
        "priority_reasons": priority_reasons,
        "effort": effort,
        "effort_reasons": list(effort_estimate.reasons),
        "expected_hourly": hourly,
        "competition": competition_level,
        "stars": stars,
        "recent_activity": repo_activity(repo_meta),
        "language": language,
        "labels": labels,
        "cash_reasons": cash_reasons[:6],
        "career_reasons": career_reasons[:10],
        "contribution_guide": guide,
        "comments": int(item.get("comments") or 0),
        "updated_at": item.get("updated_at"),
        "rejection_reason": None,
    }
