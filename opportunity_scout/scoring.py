"""Opportunity scoring and effort estimation.

Owns ranking math and implementation-effort heuristics over already-fetched evidence.
No network I/O belongs in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Collection, cast

from opportunity_scout import github
from opportunity_scout.strategic.readiness import TRUSTED_ASSOCIATIONS
from opportunity_scout.types import (
    Candidate,
    CandidateLane,
    CompetitionLevel,
    EffortBucket,
    GitHubComment,
    GitHubIssue,
    RepositoryMetadata,
)


CODE_FILE_RE = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:go|sh|py|yaml|yml)\b",
    re.IGNORECASE,
)
LIFECYCLE_HOUSEKEEPING_BOTS = {"k8s-triage-robot", "k8s-ci-robot"}
LIFECYCLE_ADMIN_COMMAND_RE = re.compile(
    r"/(?:remove-lifecycle\s+(?:stale|rotten)|"
    r"lifecycle\s+(?:stale|rotten|frozen)|"
    r"(?:remove-)?label\s+\S.*)",
    re.IGNORECASE,
)

_DOCS_RE = re.compile(r"\b(?:docs?|documentation|readme)\b", re.IGNORECASE)
_DOC_MICRO_PATTERN = (
    r"\b(?:typo|spelling|broken\s+(?:(?:docs?|documentation|readme)\s+)?"
    r"(?:link|image)|(?:link|image)\s+fix|documentation cleanup|docs cleanup)\b"
)
_DOC_IMPLEMENTATION_RE = re.compile(
    r"\b(?:add|implement|change|modify|extend)\s+(?:the\s+)?"
    r"(?:validator|parser|runtime|validation logic|parsing logic)\b|"
    r"\b(?:validator|parser|runtime)\s+(?:logic|code|behavior)\b|"
    r"\bparse\s+(?:a\s+|the\s+)?json\b|"
    r"\bnormaliz\w*\s+(?:the\s+)?(?:urls?|destinations?)\b|"
    r"\bstructured validation errors?\b|"
    r"\b(?:data|schema) migration\b",
    re.IGNORECASE,
)
_FEATURE_IMPLEMENTATION_RE = re.compile(
    r"\b(?:add|implement|change|modify|extend|refactor|rewrite)\s+(?:the\s+)?"
    r"(?:validator|parser|runtime|protocol|subsystem|architecture|api|server|client)\b|"
    r"\b(?:validator|parser|runtime|protocol)\s+(?:logic|code|behavior)\b",
    re.IGNORECASE,
)
_CROSS_COMPONENT_TERMS = (
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


@dataclass(frozen=True)
class EffortEstimate:
    """Bucketed implementation estimate plus concise calibration reasons."""

    bucket: EffortBucket
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _IssueEvidence:
    """Normalized issue evidence shared by effort and ranking calculations."""

    title: str
    body: str
    labels: str
    text: str
    prose: str
    normalized_labels: str
    file_refs: int

    @classmethod
    def from_issue(cls, item: GitHubIssue) -> _IssueEvidence:
        title, body, labels, text = issue_text(item)
        fenced = re.compile(r"\x60{3}.*?\x60{3}|~~~.*?~~~", re.DOTALL)
        prose = re.sub(r"\s+", " ", fenced.sub(" ", body)).strip()
        return cls(
            title=title,
            body=body,
            labels=labels,
            text=text,
            prose=prose,
            normalized_labels=re.sub(r"[-_/:]+", " ", labels.lower()),
            file_refs=code_reference_count(body),
        )

    @property
    def docs_signal(self) -> bool:
        return bool(_DOCS_RE.search(f"{self.title}\n{self.labels}"))

    @property
    def feature_request_scope(self) -> bool:
        return self.title.lower().startswith(("fr:", "feature request:")) or (
            "feature request" in self.normalized_labels
        )

    @property
    def feature_signal(self) -> bool:
        return bool(
            self.title.lower().startswith(("fr:", "feature request:"))
            or any(
                marker in self.normalized_labels
                for marker in (
                    "kind feature",
                    "type feature",
                    "feature request",
                    "enhancement",
                )
            )
            or re.search(r"\bfeature request\b", self.text)
        )

    @property
    def cross_component(self) -> bool:
        return self.file_refs >= 3 or sum(term in self.text for term in _CROSS_COMPONENT_TERMS) >= 3

    @property
    def bounded_docs_feature_request(self) -> bool:
        if not self.feature_request_scope or not self.docs_signal:
            return False
        return not (_FEATURE_IMPLEMENTATION_RE.search(self.text) or self.cross_component)


@dataclass(frozen=True)
class _PaymentAssessment:
    confidence: int
    amount: float | None
    hourly: float | None
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _CareerAssessment:
    score: int
    reasons: tuple[str, ...]
    maintainer_ready: bool
    language: str


def maintainer_ready_signal(labels_text: str) -> bool:
    """Recognize common contributor-ready label dialects."""
    normalized = re.sub(r"[-_]+", " ", labels_text.lower())
    return any(
        marker in normalized
        for marker in ("help wanted", "good first issue", "triage/accepted", "refined")
    )


def issue_text(item: GitHubIssue) -> tuple[str, str, str, str]:
    """Return title, body, flattened labels, and normalized title/body text."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = " ".join(
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in (item.get("labels") or [])
    ).lower()
    return title, body, labels, f"{title}\n{body}".lower()


def code_reference_count(text: str) -> int:
    """Count distinct source/config file references in free-form text."""
    return len({match.group(0).lower() for match in CODE_FILE_RE.finditer(text)})


def documentation_microfix(item: GitHubIssue) -> bool:
    """Detect explicitly bounded docs edits, not incidental docs wording."""
    evidence = _IssueEvidence.from_issue(item)
    docs_context = evidence.docs_signal
    title_micro = bool(re.search(_DOC_MICRO_PATTERN, evidence.title, re.IGNORECASE))
    body_micro = bool(
        re.search(
            r"\b(?:fix|correct|repair)\b[^\n.!?]{0,100}"
            r"\b(?:typo|spelling|broken\s+(?:(?:docs?|documentation|readme)\s+)?"
            r"(?:link|image))\b[^\n.!?]{0,100}"
            r"\b(?:docs?|documentation|readme)\b|"
            r"\b(?:docs?|documentation|readme)\b[^\n.!?]{0,100}"
            r"\b(?:fix|correct|repair)\b[^\n.!?]{0,100}"
            r"\b(?:typo|spelling|broken\s+(?:link|image))\b",
            evidence.body,
            re.IGNORECASE,
        )
    )
    bounded = bool(
        (docs_context and title_micro)
        or (
            docs_context
            and re.search(
                r"\b(?:fix|correct|repair)\b[^\n.!?]{0,100}" + _DOC_MICRO_PATTERN,
                evidence.body,
                re.IGNORECASE,
            )
        )
        or body_micro
    )
    return bool(
        bounded and not _DOC_IMPLEMENTATION_RE.search(evidence.text) and evidence.file_refs == 0
    )


def _trusted_history_complexity(activity_comments: Collection[GitHubComment] | None) -> bool:
    trusted_text = "\n".join(
        str(comment.get("body") or "")
        for comment in activity_comments or ()
        if str(comment.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS
    ).lower()
    if not trusted_text:
        return False

    broad_history = bool(
        re.search(
            r"\b(?:quite a big job|major work|structural changes?|"
            r"needs? (?:the )?code rewritten|rewrite(?:n|ing)? in streaming style)\b",
            trusted_text,
        )
    )
    concern_groups = (
        r"\b(?:cancellation|cancelled|canceling|cancelling|goroutines?|"
        r"buffering|timeouts?|resource lifecycle)\b",
        r"\b(?:regression tests?|unit tests?|integration tests?|"
        r"benchmarks?|benchmarking|test semantics)\b",
        r"\b(?:streaming style|nested (?:calls?|parsing)|json parser|"
        r"json unmarshal|unmarshal)\b",
        r"\b(?:backwards? compatibility|public api|deprecat\w*|v2)\b",
    )
    concerns = sum(bool(re.search(pattern, trusted_text)) for pattern in concern_groups)
    if broad_history and concerns:
        return True

    prior_work = bool(
        re.search(
            r"\b(?:previous|prior|attempt|implementation|pull request|this work|this change)\b",
            trusted_text,
        )
    )
    return prior_work and concerns >= 2


def estimate_effort_details(
    item: GitHubIssue,
    activity_comments: Collection[GitHubComment] | None = None,
) -> EffortEstimate:
    """Estimate implementation effort from source text and already-fetched discussion."""
    evidence = _IssueEvidence.from_issue(item)

    if documentation_microfix(item):
        return EffortEstimate("<1h", ("documentation-only micro-fix",))

    explicit_large = bool(
        re.search(
            r"\b(?:epic|roadmap|redesign|rewrite|multi-phase|"
            r"architecture (?:redesign|rewrite|overhaul|refactor|change)|"
            r"architectural (?:redesign|rewrite|overhaul|refactor|change)|"
            r"large refactor|rfc|connection pool|explore publishing)\b",
            evidence.text,
        )
        or (evidence.feature_request_scope and not evidence.bounded_docs_feature_request)
    )
    if explicit_large:
        return EffortEstimate("1d+", ("explicit broad feature/design scope",))

    compatibility_risk = bool(
        re.search(
            r"\b(?:backward[- ]incompatible|backwards? compatibility|"
            r"compatibility (?:risk|break|constraint)|persisted (?:state|data)|"
            r"existing deployments?|wire format|on-disk format)\b",
            evidence.text,
        )
        or (
            re.search(r"\b(?:wal|snapshot|handshake)\b", evidence.text)
            and re.search(
                r"\b(?:persist|compatib|existing cluster|rejoin|recover)\w*\b",
                evidence.text,
            )
        )
    )
    if compatibility_risk:
        return EffortEstimate("1d+", ("backward-compatibility or persisted-state risk",))

    reproduction_heavy = bool(
        re.search(
            r"\b(?:dual[ -]?sim|physical device|device-specific|hardware-dependent)\b",
            evidence.text,
        )
        or re.search(
            r"\b(?:unable to reproduce|cannot reproduce|can't reproduce|"
            r"haven't been able to reproduce|have not been able to reproduce|"
            r"low-probability race|non[- ]deterministic repro)\b",
            evidence.text,
        )
    )
    if reproduction_heavy:
        return EffortEstimate("1d+", ("environment/reproduction-heavy investigation",))

    if evidence.feature_signal and evidence.cross_component:
        return EffortEstimate(
            "1d+",
            ("feature spans multiple runtime/configuration components",),
        )
    if len(evidence.prose) > 12000:
        return EffortEstimate("1d+", ("large narrative implementation scope",))

    broad_docs = bool(
        evidence.docs_signal
        and re.search(
            r"\b(?:all|every|each)\s+(?:the\s+)?(?:grpc\s+)?services?\b|"
            r"\b(?:generated?|generate)\s+(?:docs?|documentation)\b|"
            r"\bdocs?\s+(?:generated|generation)\b|"
            r"\bhost(?:ed|ing)?\s+(?:them\s+)?on\s+(?:the\s+)?website\b|"
            r"\ball\s+in\s+one\s+place\b",
            evidence.text,
        )
    )
    if broad_docs:
        return EffortEstimate("6–12h", ("cross-service documentation/generation scope",))
    if evidence.docs_signal and evidence.file_refs == 0 and len(evidence.prose) < 4500:
        return EffortEstimate("1–3h", ("bounded documentation change",))

    suggested_fix_bullets = len(re.findall(r"(?m)^\s*-\s+", evidence.body))
    platform_label = any(
        marker in evidence.labels.lower()
        for marker in ("os-android", "os-ios", "os-macos", "os-windows")
    )
    missing_reproduction = "_no response_" in evidence.text or "no response" in evidence.text
    specialized_device_repro = bool(
        re.search(
            r"\b(?:android\s*tv|androidtv|apple\s*tv|tvos|fire\s*tv|roku)\b",
            evidence.text,
        )
        and re.search(r"\b(?:steps to reproduce|reproduc(?:e|tion))\b", evidence.text)
        and re.search(r"\bdevice\b", evidence.text)
    )
    constrained_network_repro = bool(
        re.search(r"\brestrictive network\b", evidence.text)
        and re.search(
            r"\b(?:blocks?|blocked)\b.{0,120}\b(?:coordination server|derp(?: servers?| relays?))\b",
            evidence.text,
        )
        and re.search(r"\bdirect connection\b", evidence.text)
        and re.search(r"\b(?:steps to reproduce|reproduc(?:e|tion))\b", evidence.text)
    )
    broader = bool(
        evidence.file_refs >= 4
        or evidence.feature_signal
        or len(evidence.prose) > 6500
        or (platform_label and missing_reproduction)
        or specialized_device_repro
        or constrained_network_repro
        or (re.search(r"\bsuggested fix(?:es)?\b", evidence.text) and suggested_fix_bullets >= 3)
    )
    if broader:
        reasons: list[str] = []
        if evidence.feature_signal:
            reasons.append("feature/enhancement scope")
        if evidence.file_refs >= 4:
            reasons.append("multiple referenced files")
        if len(evidence.prose) > 6500:
            reasons.append("large narrative scope")
        if suggested_fix_bullets >= 3:
            reasons.append("multi-step suggested implementation")
        if platform_label and missing_reproduction:
            reasons.append("platform-specific reproduction is missing")
        if specialized_device_repro:
            reasons.append("specialized device reproduction/setup")
        if constrained_network_repro:
            reasons.append("constrained network reproduction/setup")
        return EffortEstimate("6–12h", tuple(reasons[:3]) or ("broader implementation scope",))

    if _trusted_history_complexity(activity_comments):
        return EffortEstimate(
            "6–12h",
            ("maintainer-confirmed implementation-history complexity",),
        )
    if re.search(r"\b(?:data race|race condition|deadlock|concurren\w*)\b", evidence.text):
        return EffortEstimate("3–6h", ("concurrency/lifecycle debugging risk",))
    if re.search(
        r"\b(?:may be related to|upstream (?:issue|dependency)|"
        r"vendor(?:ed)? dependency|third[- ]party dependency)\b",
        evidence.text,
    ):
        return EffortEstimate("3–6h", ("upstream/dependency investigation",))

    localized_todo = bool(
        evidence.file_refs <= 2
        and re.search(r"\btodo\b", evidence.text)
        and re.search(r"\b(?:method|function|handler|header|path|codebase)\b", evidence.text)
    )
    bounded_scope = bool(
        re.search(
            r"\b(?:small|narrow|bounded|localized)\s+"
            r"(?:bug|fix|change|scope|patch|implementation)\b|"
            r"\b(?:one|single)[ -]line\s+(?:fix|change|patch)\b|"
            r"\bsingle\s+(?:code path|function|method|handler|file|test|assertion)\b",
            evidence.text[:4500],
        )
    )
    deterministic_local_failure = bool(
        re.search(r"\bdeterministic(?:ally)?\b", evidence.text[:4500])
        and re.search(
            r"\b(?:panics?|segfault|nil pointer|leaks?|incorrect|failing tests?|"
            r"unit tests?|no-op)\b|\bnever closes\b|\bevery sync\b",
            evidence.text[:4500],
        )
    )
    bounded_bug = bounded_scope or deterministic_local_failure
    if (localized_todo or bounded_bug) and len(evidence.prose) < 4500 and evidence.file_refs <= 2:
        reason = (
            "localized TODO/code-path change"
            if localized_todo
            else "bounded deterministic bug signal"
        )
        return EffortEstimate("1–3h", (reason,))

    return EffortEstimate("3–6h", ("moderate implementation scope",))


def estimate_effort(item: GitHubIssue) -> EffortBucket:
    """Return the public effort bucket for an issue."""
    return estimate_effort_details(item).bucket


def effort_hours(effort: EffortBucket) -> float:
    return {"<1h": 0.75, "1–3h": 2.0, "3–6h": 4.5, "6–12h": 9.0, "1d+": 16.0}[effort]


def comment_contributes_to_competition(comment: GitHubComment) -> bool:
    """Return whether a comment is substantive enough to count as competition."""
    body = str(comment.get("body") or "").strip()
    if not body:
        return True

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and all(LIFECYCLE_ADMIN_COMMAND_RE.fullmatch(line) for line in lines):
        return False

    login = str((comment.get("user") or {}).get("login", "")).lower()
    is_bot = login.endswith("[bot]") or login in LIFECYCLE_HOUSEKEEPING_BOTS
    if not is_bot:
        return True

    lowered = body.lower()
    is_housekeeping = (
        "this bot triages" in lowered
        or "automatically marked as stale" in lowered
        or "automatically marked as rotten" in lowered
        or "closed due to inactivity" in lowered
        or "due to inactivity" in lowered
        or (
            login in LIFECYCLE_HOUSEKEEPING_BOTS
            and re.search(
                r"(?:lifecycle/(?:stale|rotten)|/(?:remove-)?lifecycle\s+(?:stale|rotten))",
                lowered,
            )
        )
    )
    return not bool(is_housekeeping)


def competition(
    item: GitHubIssue,
    activity_comments: Collection[GitHubComment] | None = None,
) -> CompetitionLevel:
    """Classify visible implementation competition."""
    count = (
        sum(comment_contributes_to_competition(comment) for comment in activity_comments)
        if activity_comments is not None
        else int(item.get("comments") or 0)
    )
    if count == 0:
        return "none"
    if count <= 3:
        return "low"
    if count <= 8:
        return "medium"
    return "high"


def payment_confidence(signal: str | None) -> int:
    """Return confidence for an already-verified payment signal."""
    if not signal:
        return 0
    prefixes = (
        ("confirmed bounty platform", 100),
        ("explicit bounty command", 100),
        ("explicit /reward comment", 98),
        ("explicit /bounty comment", 98),
        ("bounty labels", 95),
        ("named bounty platform", 90),
    )
    for prefix, confidence in prefixes:
        if signal.startswith(prefix):
            return confidence
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
    return float(currency.group(1).replace(",", "")) if currency else None


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
    """Adjust strategic value for execution effort and visible competition."""
    effort_delta = {"<1h": 5, "1–3h": 6, "3–6h": 2, "6–12h": -4, "1d+": -10}[effort]
    competition_delta = {"none": 7, "low": 3, "medium": -4, "high": -10}[competition_level]

    effort_reason = f"{effort} execution {'bonus' if effort_delta > 0 else 'penalty'}"
    if competition_level == "none":
        competition_reason = "no visible competition bonus"
    elif competition_delta > 0:
        competition_reason = f"{competition_level} competition bonus"
    else:
        competition_reason = f"{competition_level} competition penalty"

    score = max(0, min(100, career_score + effort_delta + competition_delta))
    return score, [effort_reason, competition_reason]


def _amount_points(amount: float) -> int:
    if amount >= 500:
        return 20
    if amount >= 100:
        return 16
    if amount >= 25:
        return 12
    if amount >= 5:
        return 8
    return 4


def _hourly_points(hourly: float) -> int:
    if hourly >= 100:
        return 25
    if hourly >= 50:
        return 21
    if hourly >= 20:
        return 16
    if hourly >= 10:
        return 10
    return 4


def _assess_payment(
    signal: str | None,
    effort: EffortBucket,
    competition_level: CompetitionLevel,
    stars: int,
    active_30d: bool,
) -> _PaymentAssessment:
    confidence = payment_confidence(signal)
    amount = usd_like_amount_from_signal(signal)
    reasons = [f"payment confidence {confidence}/100"]
    score_parts = [round(confidence * 0.30)]
    hourly: float | None = None

    if amount is None:
        reasons.append("reward not USD-comparable")
    else:
        hourly = amount / effort_hours(effort)
        score_parts.extend((_amount_points(amount), _hourly_points(hourly)))
        reasons.append(f"~${hourly:.0f}/h expected value")

    score_parts.append({"none": 15, "low": 11, "medium": 6, "high": 0}[competition_level])
    if stars >= 1000:
        score_parts.append(7)
        reasons.append("established repo")
    elif stars >= 100:
        score_parts.append(4)
    if active_30d:
        score_parts.append(3)

    return _PaymentAssessment(
        confidence=confidence,
        amount=amount,
        hourly=hourly,
        score=max(0, min(100, sum(score_parts))),
        reasons=tuple(reasons),
    )


def _repository_career_value(
    repo: str | None,
    stars: int,
    active_30d: bool,
    *,
    target_repos: Collection[str],
) -> tuple[int, list[str]]:
    points = 0
    reasons: list[str] = []
    if stars >= 10000:
        points += 18
        reasons.append("10k+ star repo")
    elif stars >= 1000:
        points += 14
        reasons.append("1k+ star repo")
    elif stars >= 100:
        points += 9
    elif stars >= 10:
        points += 4

    if active_30d:
        points += 8
        reasons.append("repo active in last 30d")
    if repo in target_repos:
        points += 14
        reasons.append("target repo bonus")
    return points, reasons


def _technical_career_value(
    evidence: _IssueEvidence,
    language: str,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    lang = language.lower()
    language_points = (
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
    if language_points:
        reasons.append(f"{language} codebase")

    infrastructure_terms = (
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
    api_domain = bool(
        re.search(
            r"\b(?:http|rest|grpc|kubernetes|cloud|provider|server|backend)\s+api\b|"
            r"\bapi\s+(?:server|gateway|endpoint|client)\b",
            evidence.text,
        )
    )
    if any(term in evidence.text for term in infrastructure_terms) or api_domain:
        language_points += 6
        reasons.append("target infrastructure/domain fit")

    points = min(18, language_points)
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
    depth = sum(term in evidence.text for term in depth_terms)
    if depth >= 3:
        points += 14
    elif depth >= 1:
        points += 8
    elif re.search(r"\b(?:test|regression|bug|fix)\b", evidence.text):
        points += 4
    if depth:
        reasons.append("meaningful technical depth")
    return points, reasons


def _execution_career_value(
    evidence: _IssueEvidence,
    guide: str | None,
    effort: EffortBucket,
) -> tuple[int, list[str], bool]:
    points = 0
    reasons: list[str] = []
    if re.search(r"\b(?:test|tests|regression)\b", evidence.text):
        points += 6
        reasons.append("tests/regression signal")

    maintainer_ready = maintainer_ready_signal(evidence.labels)
    if maintainer_ready:
        points += 8
        reasons.append("maintainer-ready signal")
    if guide:
        points += 3
        reasons.append("contribution guide found")

    effort_points = {"<1h": 9, "1–3h": 7, "3–6h": 4, "6–12h": 1, "1d+": 0}[effort]
    points += effort_points
    if effort in ("<1h", "1–3h"):
        reasons.append("bounded implementation scope")

    clarity = 0
    if re.search(r"\b(?:root cause|code path|cause \(from)\b", evidence.text):
        clarity += 3
    if "steps to reproduce" in evidence.text and "_no response_" not in evidence.text:
        clarity += 2
    if re.search(r"\b(?:suggested fix|possible fix|expected behavior)\b", evidence.text):
        clarity += 2
    clarity += min(3, evidence.file_refs)
    if clarity >= 5:
        reasons.append("clear implementation/reproduction detail")
    elif clarity:
        reasons.append("implementation detail available")
    points += min(10, clarity)
    return min(30, points), reasons, maintainer_ready


def _career_assessment(
    item: GitHubIssue,
    repo: str | None,
    repo_meta: RepositoryMetadata,
    guide: str | None,
    effort: EffortBucket,
    competition_level: CompetitionLevel,
    stars: int,
    active_30d: bool,
    evidence: _IssueEvidence,
    *,
    target_repos: Collection[str],
) -> _CareerAssessment:
    language = str(repo_meta.get("language") or "Unknown")
    repo_points, repo_reasons = _repository_career_value(
        repo,
        stars,
        active_30d,
        target_repos=target_repos,
    )
    technical_points, technical_reasons = _technical_career_value(evidence, language)
    execution_points, execution_reasons, maintainer_ready = _execution_career_value(
        evidence,
        guide,
        effort,
    )

    score = repo_points + technical_points + execution_points
    reasons = repo_reasons + technical_reasons + execution_reasons
    score -= {"none": 0, "low": 2, "medium": 6, "high": 12}[competition_level]
    if effort == "6–12h":
        score -= 3
        reasons.append("broader implementation scope")
    elif effort == "1d+":
        score -= 10
        reasons.append("large-scope penalty")

    return _CareerAssessment(score, tuple(reasons), maintainer_ready, language)


def _strategic_activity_adjustment(
    item: GitHubIssue,
    activity_comments: list[GitHubComment] | None,
    maintainer_ready: bool,
) -> tuple[int, list[str]]:
    now = datetime.now(timezone.utc)
    created = github.parse_github_datetime(item.get("created_at"))
    updated = github.parse_github_datetime(item.get("updated_at"))
    created_days = max(0, (now - created).days) if created else None

    latest_bot: datetime | None = None
    latest_human: datetime | None = None
    recent_comment_days: int | None = None
    recent_maintainer_days: int | None = None

    for comment in activity_comments or []:
        stamp = github.parse_github_datetime(comment.get("updated_at") or comment.get("created_at"))
        if not stamp:
            continue
        login = str((comment.get("user") or {}).get("login", "")).lower()
        if login.endswith("[bot]"):
            if latest_bot is None or stamp > latest_bot:
                latest_bot = stamp
            continue

        if latest_human is None or stamp > latest_human:
            latest_human = stamp
        age = max(0, (now - stamp).days)
        if recent_comment_days is None or age < recent_comment_days:
            recent_comment_days = age
        association = str(comment.get("author_association", "")).upper()
        if association in TRUSTED_ASSOCIATIONS and (
            recent_maintainer_days is None or age < recent_maintainer_days
        ):
            recent_maintainer_days = age

    effective_updated = updated
    if (
        updated is not None
        and latest_bot is not None
        and abs((updated - latest_bot).total_seconds()) <= 300
        and (latest_human is None or latest_human < latest_bot)
    ):
        effective_updated = latest_human or created
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
    """Build the canonical ranked candidate from verified issue/repository evidence."""
    repo, number = github.issue_repo_and_number(item)
    strategic_comments = activity_comments if lane == "strategic" else None
    effort_estimate = estimate_effort_details(item, strategic_comments)
    effort = effort_estimate.bucket
    competition_level = competition(item, strategic_comments)
    stars = int(repo_meta.get("stargazers_count") or 0)
    pushed = github.parse_github_datetime(repo_meta.get("pushed_at"))
    active_30d = bool(pushed and (datetime.now(timezone.utc) - pushed).days <= 30)
    evidence = _IssueEvidence.from_issue(item)

    payment = (
        _assess_payment(signal, effort, competition_level, stars, active_30d)
        if lane == "paid"
        else _PaymentAssessment(0, None, None, 0, ())
    )
    career = _career_assessment(
        item,
        repo,
        repo_meta,
        guide,
        effort,
        competition_level,
        stars,
        active_30d,
        evidence,
        target_repos=target_repos,
    )
    career_score = career.score
    career_reasons = list(career.reasons)

    if lane == "strategic":
        activity_delta, activity_reasons = _strategic_activity_adjustment(
            item,
            activity_comments,
            career.maintainer_ready,
        )
        career_score += activity_delta
        career_reasons.extend(activity_reasons)
        if documentation_microfix(item):
            career_score = min(career_score, 45)
            career_reasons.insert(0, "documentation-only micro-fix cap")

    career_score = max(0, min(100, career_score))
    if lane == "strategic":
        priority_score, priority_reasons = strategic_priority_score(
            career_score,
            effort,
            competition_level,
        )
    else:
        priority_score = min(
            100,
            max(payment.score, career_score)
            + (5 if payment.score >= 70 and career_score >= 70 else 0),
        )
        priority_reasons = []

    labels = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in (item.get("labels") or [])
    ]
    return {
        "repo": cast(str, repo),
        "issue_number": cast(int, number),
        "title": item.get("title"),
        "url": cast(str, item.get("html_url")),
        "paid": lane == "paid",
        "reward": reward_text(signal, amount_pattern),
        "payment_confidence": payment_confidence(signal),
        "cash_score": payment.score,
        "career_score": career_score,
        "priority_score": priority_score,
        "priority_reasons": priority_reasons,
        "effort": effort,
        "effort_reasons": list(effort_estimate.reasons),
        "expected_hourly": payment.hourly,
        "competition": competition_level,
        "stars": stars,
        "recent_activity": repo_activity(repo_meta),
        "language": career.language,
        "labels": labels,
        "cash_reasons": list(payment.reasons[:6]),
        "career_reasons": career_reasons[:10],
        "contribution_guide": guide,
        "comments": int(item.get("comments") or 0),
        "updated_at": item.get("updated_at"),
        "rejection_reason": None,
    }
