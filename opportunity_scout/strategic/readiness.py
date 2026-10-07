"""Pure strategic readiness and issue-lifecycle policy.

These helpers interpret issue metadata and maintainer comments. They perform no
network I/O and do not depend on the application orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final

from opportunity_scout.strategic.claims import normalized_claim_text
from opportunity_scout.types import GitHubComment, GitHubIssue

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

_LABEL_SEPARATOR_RE: Final = re.compile(r"[-_/:]+")

_TRIAGE_MARKERS: Final = (
    "needs triage",
    "triage pending",
    "bug possible",
    "needs analysis",
    "needs investigation",
)
_PROPOSAL_LABEL_MARKERS: Final = (
    "kind proposal",
    "type proposal",
    "status proposal",
    "rfc",
    "needs discussion",
    "discussion",
)
_CONTRIBUTOR_AUTHORITY_MARKERS: Final = (
    "hand it off to the engineering team",
    "hand this off to the engineering team",
    "we're marking this as",
    "we are marking this as",
    "we'll reevaluate",
    "we will reevaluate",
    "i'm closing this",
    "i am closing this",
    "we're closing this",
    "we are closing this",
    "prevailing wisdom on the maintainer team",
)
_READY_MARKERS: Final = (
    "ready for implementation",
    "ready to implement",
    "feel free to work on this",
    "contributions welcome",
    "prs welcome",
    "pull requests welcome",
    "go ahead and implement",
    "you can start implementation",
    "happy to accept a pr",
    "happy to accept a pull request",
    "pr in this repository is welcome",
    "pull request in this repository is welcome",
    "implementation is wanted here",
    "reviving this issue",
    "revive this issue",
    "this issue is active again",
    "reopening this for implementation",
)
_WRONG_APPROACH_MARKERS: Final = (
    "would be the wrong solution",
    "is the wrong solution",
    "would be the wrong approach",
    "is the wrong approach",
    "not the right solution",
    "not the right approach",
    "should not be implemented",
    "shouldn't be implemented",
    "use case for provider functions",
    "use case for a provider function",
    "should be implemented as a provider function",
    "should live in a provider",
    "should be done in a provider",
)
_DISCUSSION_MARKERS: Final = (
    "needs discussion",
    "need more discussion",
    "need to discuss this first",
    "should discuss this first",
    "being discussed on spec level",
    "being discussed at the spec level",
    "spec-level discussion",
    "specification discussion is ongoing",
    "worth discussion",
    "would like to hear from the community",
    "like to hear from the community",
)
_INVESTIGATION_MARKERS: Final = (
    "needs investigation",
    "need more investigation",
    "need to investigate",
    "needs engineering investigation",
)
_REPRODUCTION_MARKERS: Final = (
    "needs reproduction",
    "need a reproduction",
    "need reproduction",
    "please reproduce",
    "can you reproduce",
)
_IMPLEMENTATION_WAIT_MARKERS: Final = (
    "not ready for implementation",
    "not ready to implement",
    "please wait before implementing",
    "please wait to implement",
    "hold off on implementation",
    "hold off implementing",
    "do not start implementation",
    "don't start implementation",
)
_CLARIFICATION_MARKERS: Final = (
    "needs clarification",
    "need clarification",
    "need to clarify",
    "please clarify before",
)
_PROPOSAL_FEEDBACK_MARKERS: Final = (
    "gauge community interest",
    "gather feedback",
    "collect feedback",
    "community time to weigh in",
    "reevaluate based on the feedback",
    "re-evaluate based on the feedback",
    "before committing",
)
_REPORTER_UNRESOLVED_MARKERS: Final = (
    "still reproduces",
    "still reproducible",
    "not fixed",
    "isn't fixed",
    "issue is still present",
    "problem is still present",
)
_REPORTER_RESOLVED_MARKERS: Final = (
    "issue got fixed",
    "issue is fixed",
    "problem got fixed",
    "problem is fixed",
    "no longer reproduces",
    "can't reproduce anymore",
    "cannot reproduce anymore",
)
_REPORTER_EXTERNAL_INFRA_RE: Final = re.compile(
    r"\b(?:infra(?:structure)?\s+flakiness|external\s+infrastructure|"
    r"registry/network-side\s+transient\s+flakiness|network-side\s+transient\s+flakiness)\b",
    re.IGNORECASE,
)
_REPORTER_NO_CODE_FIX_RE: Final = re.compile(
    r"\bno\s+code\s+fix\s+is\s+being\s+attempted\b|"
    r"\bnothing\s+we\s+can\s+fix(?:\s+really)?\b",
    re.IGNORECASE,
)
_PAYOUT_SUMMARY_MARKERS: Final = (
    "total bounty distributed",
    "top contributors",
    "fastest fixes",
    "monthly stats",
)

_DIAGNOSTIC_EVIDENCE_RE: Final = re.compile(
    r"\b(?:attached|provided|uploaded|included|here(?:'s| is)|see)\b"
    r".{0,160}\b(?:cpu profile|memory profile|heap profile|profile|stack trace|"
    r"minimal reproducer|reproducer|logs?|benchmark|trace|dump)\b",
    re.DOTALL,
)
_DIAGNOSTIC_REQUEST_RE: Final = re.compile(
    r"\b(?:could|can|would)\s+you\s+(?:please\s+)?"
    r"(?:provide|share|attach|capture|collect|send)\b|"
    r"\bwould\s+it\s+be\s+possible\s+to\s+(?:provide|share|attach)\b|"
    r"\bplease\s+(?:provide|share|attach|capture|collect|send)\b|"
    r"\bwe\s+need\s+(?:a|the)\b"
)
_DIAGNOSTIC_KIND_RE: Final = re.compile(
    r"\b(?:cpu|memory|heap)\s+profiles?\b|"
    r"\bprofiles?\s+from\b|"
    r"\bstack traces?\b|"
    r"\bminimal reproduc(?:er|tion)\b|"
    r"\breproducers?\b|"
    r"\blogs?\s+(?:from|required|showing)\b|"
    r"\b(?:node|instance|daemon|pod|container)\s+logs?\b|"
    r"\bbenchmarks?\b|"
    r"\btraces?\b|"
    r"\bdumps?\b"
)
_REDIRECT_TARGET_RE: Final = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+|"
    r"(?<![\w.-])[\w.-]+/[\w.-]+(?![\w.-])|"
    r"\b(?:specification|upstream|another repository|another project|canonical repository)\b"
)
_REDIRECT_ACTION_RE: Final = re.compile(
    r"\brequires?\s+(?:a\s+)?(?:change|fix)\s+in\b|"
    r"\bneeds?\s+to\s+be\s+(?:fixed|changed|implemented)\s+in\b|"
    r"\bplease\s+open\s+(?:a\s+)?(?:ticket|issue)\s+(?:there|against)\b|"
    r"\bbest\s+to\s+open\s+(?:a\s+)?(?:ticket|issue)\s+there\b|"
    r"\b(?:implementation|change|fix)\s+belongs\s+in\b|"
    r"\bthis\s+is\s+an?\s+upstream\s+issue\b"
)
_DUPLICATE_REFERENCE_RE: Final = re.compile(
    r"(?<!\w)#\d+\b|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+"
)
_DUPLICATE_ACTION_RE: Final = re.compile(
    r"\blooks?\s+like\s+(?:a\s+)?(?:\(?possible\)?\s+)?duplicate\s+of\b|"
    r"\bpossible\s+duplicate\s+of\b|"
    r"\btracked\s+in\s+(?:issue\s+)?(?:#|https://github\.com/)|"
    r"\bdiscussion\s+is\s+(?:being\s+)?tracked\s+there\b|"
    r"\bplease\s+continue\s+(?:this|the discussion)\s+in\b"
)
_DECISION_STAGE_RE: Final = re.compile(
    r"\b(?:this|the) issue is to decide\b|"
    r"\bwe (?:still )?need to decide\b|"
    r"\bdecision (?:is|remains) (?:open|pending)\b"
)
_MAINTAINER_FUTURE_OWNERSHIP_RE: Final = re.compile(
    r"\bwe(?:'ll| will)\s+(?:probably\s+)?look\s+into\s+this\s+when\s+we\s+"
    r"(?:make|implement|add|land|finish)\b",
    re.IGNORECASE,
)
_MAINTAINER_IDEA_HEADING_RE: Final = re.compile(
    r"(?m)^\s*#{1,6}\s+just\s+an\s+idea\s*$",
    re.IGNORECASE,
)
_MAINTAINER_IDEA_UNCERTAINTY_RE: Final = re.compile(
    r"\bnot\s+sure\s+if\b.{0,180}\bshould\b|"
    r"\bmaybe\s+(?:it(?:'s| is)|this(?: is)?)\s+fine\s+to\s+implement\b",
    re.IGNORECASE | re.DOTALL,
)
_MAINTAINER_OPINION_REQUEST_RE: Final = re.compile(
    r"\bdo\s+you\s+have\s+an\s+opinion\s+on\s+this\b",
    re.IGNORECASE,
)
_MAINTAINER_CURRENT_DEFAULT_RE: Final = re.compile(
    r"\bnow\s+the\s+default\s+for\s+new\s+installations\b",
    re.IGNORECASE,
)
_MAINTAINER_SAVE_LOAD_RESOLUTION_RE: Final = re.compile(
    r"\bsave\s+the\s+image\b.{0,1200}\bload\s+it\s+again\b"
    r".{0,1200}\bafter\s+loading\b.{0,120}\bdigests?\s+are\s+the\s+same\b",
    re.IGNORECASE | re.DOTALL,
)
_SUBMISSION_CLOSED_RE: Final = re.compile(
    r"\b(?:a |the )?(?:pr|pull request).{0,80}\bwill be closed\b",
    re.DOTALL,
)
_SUPPORT_QUESTION_RE: Final = re.compile(
    r"(?m)^\s*\d+\.\s+(?:is|are|could|would|should|can|do|does)\b.*\?\s*$",
    re.IGNORECASE,
)
_REPORTER_GUIDANCE_REQUEST_RE: Final = re.compile(
    r"\b(?:seeking|looking for|requesting)\s+guidance\s+(?:on|about|for)\b"
    r".{0,180}\b(?:how\s+to|configur(?:e|ation)|use|using|supply|provide|add|set\s*up)\b",
    re.IGNORECASE | re.DOTALL,
)
_REPORTER_IMPLEMENTATION_APPROVAL_RE: Final = re.compile(
    r"\bbefore\s+(?:another\s+|an?\s+)?(?:implementation\s+)?(?:pr|pull request)\b"
    r".{0,260}\b(?:maintainers?\b.{0,100}\b(?:review|approve)|"
    r"guidance\b.{0,100}\b(?:design|scope|direction)|"
    r"(?:preferred|right)\s+(?:design|approach|direction))\b",
    re.IGNORECASE | re.DOTALL,
)
_REPORTER_DESIGN_PLANNING_RE: Final = re.compile(
    r"\b(?:we\s+)?need\s+a\s+plan\s+for\b|"
    r"\bfigure\s+out\s+(?:the\s+)?(?:lifecycle|gc|garbage collection|"
    r"bookkeeping|ownership|semantics)\b",
    re.IGNORECASE,
)
_REPORTER_DESIGN_QUESTION_RE: Final = re.compile(
    r"\b(?:what if|when is it safe|how should|who should|should we|"
    r"whether we should|won't know|will not know)\b",
    re.IGNORECASE,
)
_REPORTER_OPEN_DESIGN_RE: Final = re.compile(
    r"\bstill\s+open\s+discussion\b",
    re.IGNORECASE,
)
_REPORTER_IMPLEMENTATION_CHOICE_RE: Final = re.compile(
    r"\b(?:it\s+isn['’]t\s+obvious\s+to\s+me\s+how\s+best|"
    r"is\s+having\b.{0,140}\breasonable|would\s+it\s+be\s+better|"
    r"do\s+we\s+need|should\s+(?:the|this|we)\b|what\s+happens\s+when)\b",
    re.IGNORECASE | re.DOTALL,
)
_TRACKING_CONTAINER_RE: Final = re.compile(
    r"\b(?:use|using)\s+(?:this|the)\s+issue\s+for\s+tracking\b"
)
_CHILD_DELEGATION_RE: Final = re.compile(
    r"\b(?:create|open|file)\s+(?:sub[- ]?issues?|child issues?|separate issues?)\b"
)
_TRACKING_INTENT_RE: Final = re.compile(
    r"\b(?:track and resolve|track the following|tracking issue for)\b",
    re.IGNORECASE,
)
_CHILD_CHECKBOX_RE: Final = re.compile(r"(?m)^\s*[-*]\s*\[[ xX]\]\s*#\d+\b")
_DEPENDENCY_TRACKING_RE: Final = re.compile(
    r"\b(?:dependency|dependencies)\s+(?:dashboard|tracker|tracking)\b"
)
_AUTOMATED_MANAGEMENT_RE: Final = re.compile(
    r"\b(?:automatically managed|managed automatically|auto-managed)\b"
)
_TRACKING_OR_MONITORING_RE: Final = re.compile(r"\b(?:tracks?|tracking|monitors?|monitoring)\b")
_NON_ACTIONABLE_RE: Final = re.compile(
    r"\b(?:no action (?:to take|is )?required|no action to take|do not assign|don't assign)\b"
)
_NEXT_SUCCESS_RE: Final = re.compile(
    r"\bnext successful\s+(?:build|publish|publication|run)"
    r"\s+will\s+(?:update|close)\s+this\s+(?:issue|incident)\b"
)
_RELEASE_TRACKING_RE: Final = re.compile(
    r"\brelease(?:\s+\S+){0,2}\s+(?:tracking|tracker|checklist|planning)\b|"
    r"\b(?:tracking|tracker|checklist)\s+(?:for\s+)?release\b|"
    r"\bplan(?:ned|ning)?\s+to\s+release\b|"
    r"\bplanned(?:\s+\S+){0,3}\s+release\b"
)
_IMPLEMENTATION_DONE_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\bpr\s*#\d+.{0,120}\bmerged\b",
        r"\balready\s+(?:merged|fixed|implemented|resolved)\b",
        r"\b(?:seems|appears)\s+to\s+be\s+resolved\s+by\b",
        r"\b(?:is|was)\s+resolved\s+by\b",
        r"\b(?:fixed|implemented|resolved)\s+(?:on|in)\s+(?:the\s+)?(?:main|master)\b",
        r"\b(?:fix|implementation)\s+(?:is|has\s+been)\s+(?:already\s+)?"
        r"(?:on|in)\s+(?:main|master)\b",
        r"\b(?:version|release)\s+update\s+was\s+merged\b",
    )
)
_RELEASE_ONLY_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\brelease\s+tag\s+has\s+not\s+yet\s+been\s+published\b",
        r"\brequest\s*:\s*please\s+tag\s+(?:a\s+)?(?:new\s+)?release\b",
        r"\bplease\s+(?:tag|cut|publish)\s+(?:a\s+)?(?:new\s+)?release\b",
        r"\bwaiting\s+for\s+(?:a\s+)?(?:release|tag)\b",
        r"\bonly\s+(?:release|tagging)\s+remains\b",
        r"\b(?:not|isn't|is\s+not|hasn't\s+been|has\s+not\s+been)\s+released\s+yet\b",
        r"\b(?:awaiting|waiting\s+for)\s+(?:the\s+)?next\s+release\b",
        r"\b(?:will|should)\s+be\s+(?:good\s+)?(?:in|with)\s+(?:the\s+)?next\s+release\b",
        r"\b(?:will|should)\s+be\s+included\s+in\s+(?:the\s+)?next\s+release\b",
        r"\brelease\s+step.{0,120}\bnever\s+triggered\b",
        r"\btag,?\s+but\s+no\s+release\b",
    )
)

_REASON_DIAGNOSTIC = "maintainer is waiting for requested diagnostic evidence"
_REASON_REDIRECT = "maintainer redirected implementation/discussion to another project"
_REASON_DUPLICATE = "maintainer indicates this is probably tracked by another canonical issue"
_REASON_WRONG_APPROACH = "maintainer indicates the proposed implementation approach is not wanted"
_REASON_DISCUSSION = "maintainer says issue still needs discussion"
_REASON_INVESTIGATION = "maintainer says issue still needs investigation"
_REASON_REPRODUCTION = "maintainer says reproduction is still required"
_REASON_WAIT = "maintainer asked contributors to wait before implementation"
_REASON_CLARIFICATION = "maintainer says issue still needs clarification"
_REASON_PROPOSAL_FEEDBACK = "proposal is still gathering feedback"
_REASON_UMBRELLA = "umbrella tracking issue, not a single implementation task"


@dataclass(frozen=True, slots=True)
class _IssueEvidence:
    title: str
    body: str
    title_lower: str
    body_lower: str
    normalized_body_lower: str
    labels_text: str
    label_set: frozenset[str]
    author_association: str
    reporter_login: str


@dataclass(frozen=True, slots=True)
class _CommentEvidence:
    body_lower: str
    normalized_body_lower: str
    author_association: str
    login: str


def _label_names(item: GitHubIssue) -> tuple[str, ...]:
    return tuple(
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in (item.get("labels") or [])
    )


def _issue_evidence(item: GitHubIssue) -> _IssueEvidence:
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    label_names = _label_names(item)
    return _IssueEvidence(
        title=title,
        body=body,
        title_lower=title.lower(),
        body_lower=body.lower(),
        normalized_body_lower=normalized_claim_text(body).lower(),
        labels_text=" ".join(label_names).lower(),
        label_set=frozenset(name.strip().lower() for name in label_names if name.strip()),
        author_association=str(item.get("author_association", "")).upper(),
        reporter_login=str((item.get("user") or {}).get("login", "")).lower(),
    )


def _comment_evidence(comment: GitHubComment) -> _CommentEvidence:
    body = str(comment.get("body", ""))
    return _CommentEvidence(
        body_lower=body.lower(),
        normalized_body_lower=normalized_claim_text(body).lower(),
        author_association=str(comment.get("author_association", "")).upper(),
        login=str((comment.get("user") or {}).get("login", "")).lower(),
    )


def _normalize_label_separators(text: str) -> str:
    return _LABEL_SEPARATOR_RE.sub(" ", text.lower())


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def triage_pending_signal(labels_text: str) -> bool:
    """Recognize pending-triage label dialects used by target repositories."""
    return _contains_any(_normalize_label_separators(labels_text), _TRIAGE_MARKERS)


def issue_label_set(item: GitHubIssue) -> set[str]:
    """Return normalized raw label names without flattening their separators."""
    return set(_issue_evidence(item).label_set)


def proposal_stage_signal(item: GitHubIssue) -> bool:
    """Recognize explicit proposal/RFC/discussion-stage metadata."""
    evidence = _issue_evidence(item)
    normalized_labels = _normalize_label_separators(evidence.labels_text)
    return bool(
        re.search(r"\b(?:proposal|rfc)\b", evidence.title, re.IGNORECASE)
        or _contains_any(normalized_labels, _PROPOSAL_LABEL_MARKERS)
    )


def _comment_has_maintainer_authority(evidence: _CommentEvidence) -> bool:
    if evidence.author_association in TRUSTED_ASSOCIATIONS:
        return True
    return evidence.author_association == "CONTRIBUTOR" and _contains_any(
        evidence.body_lower,
        _CONTRIBUTOR_AUTHORITY_MARKERS,
    )


def maintainer_comment_authority(comment: GitHubComment) -> bool:
    """Trust maintainer associations plus explicit project-action comments."""
    return _comment_has_maintainer_authority(_comment_evidence(comment))


def _diagnostic_evidence_supplied(body: str) -> bool:
    return _DIAGNOSTIC_EVIDENCE_RE.search(body) is not None


def _diagnostic_requested(body: str) -> bool:
    return (
        _DIAGNOSTIC_REQUEST_RE.search(body) is not None
        and _DIAGNOSTIC_KIND_RE.search(body) is not None
    )


def _redirects_to_other_project(body: str) -> bool:
    if "closing it here" in body and re.search(r"\b(?:i think\s+)?we should do it in core\b", body):
        return True
    return (
        _REDIRECT_TARGET_RE.search(body) is not None
        and _REDIRECT_ACTION_RE.search(body) is not None
    )


def _canonical_duplicate(body: str) -> bool:
    if _DUPLICATE_REFERENCE_RE.search(body) is None or "not a duplicate" in body:
        return False
    return _DUPLICATE_ACTION_RE.search(body) is not None


def _explicit_ready_signal(body: str) -> bool:
    return _contains_any(body, _READY_MARKERS)


def _maintainer_hold_reason(body: str, proposal_stage: bool) -> tuple[str | None, bool]:
    """Return the first applicable hold reason in policy precedence order."""
    if _diagnostic_requested(body):
        return _REASON_DIAGNOSTIC, True
    if _redirects_to_other_project(body):
        return _REASON_REDIRECT, False
    if _canonical_duplicate(body):
        return _REASON_DUPLICATE, False
    if _contains_any(body, _WRONG_APPROACH_MARKERS):
        return _REASON_WRONG_APPROACH, False
    if _contains_any(body, _DISCUSSION_MARKERS):
        return _REASON_DISCUSSION, False
    if _contains_any(body, _INVESTIGATION_MARKERS):
        return _REASON_INVESTIGATION, False
    if _contains_any(body, _REPRODUCTION_MARKERS) or (
        "are you sure" in body and "reproduc" in body
    ):
        return _REASON_REPRODUCTION, False
    if _contains_any(body, _IMPLEMENTATION_WAIT_MARKERS):
        return _REASON_WAIT, False
    if _contains_any(body, _CLARIFICATION_MARKERS):
        return _REASON_CLARIFICATION, False
    if proposal_stage and _contains_any(body, _PROPOSAL_FEEDBACK_MARKERS):
        return _REASON_PROPOSAL_FEEDBACK, False
    if proposal_stage and "time-boxed" in body and "discussion" in body:
        return _REASON_PROPOSAL_FEEDBACK, False
    return None, False


def maintainer_readiness_comment_state(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> tuple[bool | None, str | None]:
    """Return the latest explicit trusted-maintainer readiness stance."""
    proposal_stage = proposal_stage_signal(item)
    state: bool | None = None
    reason: str | None = None
    diagnostic_pending = False

    for comment in comments or []:
        evidence = _comment_evidence(comment)
        body = evidence.normalized_body_lower

        if diagnostic_pending and _diagnostic_evidence_supplied(body):
            state = None
            reason = None
            diagnostic_pending = False

        if not _comment_has_maintainer_authority(evidence):
            continue

        if _explicit_ready_signal(body):
            state = True
            reason = None
            diagnostic_pending = False
            continue

        hold_reason, requested_diagnostics = _maintainer_hold_reason(body, proposal_stage)
        if hold_reason is not None:
            state = False
            reason = hold_reason
            diagnostic_pending = requested_diagnostics

    return state, reason


def readiness_pending_label_reason(
    item: GitHubIssue,
    ready_override: bool = False,
) -> str | None:
    """Reject explicit not-ready label states unless readiness is overridden."""
    if ready_override:
        return None

    normalized_labels = tuple(_normalize_label_separators(label) for label in issue_label_set(item))
    rules = (
        ("needs reproduction", "awaiting reproduction confirmation"),
        ("waiting for reproduction", "awaiting reproduction confirmation"),
        ("needs discussion", "awaiting maintainer discussion"),
        ("needs investigation", "awaiting maintainer investigation"),
        ("needs analysis", "awaiting maintainer investigation"),
        ("needs clarification", "awaiting maintainer clarification"),
        ("needs design", "awaiting maintainer design decision"),
    )
    for marker, reason in rules:
        if any(marker in label for label in normalized_labels):
            return reason
    return None


def abandoned_lifecycle_reason(
    item: GitHubIssue,
    ready_override: bool = False,
) -> str | None:
    """Reject unambiguously abandoned lifecycle states unless explicitly revived."""
    if ready_override:
        return None
    if "lifecycle/rotten" in issue_label_set(item):
        return "issue is in an abandoned/rotten lifecycle state"
    return None


def maintainer_issue_decision_reason(item: GitHubIssue) -> str | None:
    """Reject trusted maintainer-authored issues that remain unready or project-owned."""
    evidence = _issue_evidence(item)
    if evidence.author_association not in TRUSTED_ASSOCIATIONS:
        return None

    body = evidence.normalized_body_lower
    if _explicit_ready_signal(body):
        return None
    if _DECISION_STAGE_RE.search(evidence.body_lower):
        return "maintainer-authored issue is still deciding implementation semantics"
    if _MAINTAINER_FUTURE_OWNERSHIP_RE.search(body):
        return "maintainer-authored issue is planned as related project follow-up"
    return None


def maintainer_current_behavior_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> str | None:
    """Reject when a trusted maintainer proves the current default behavior resolves the issue."""
    issue_text = _issue_evidence(item).normalized_body_lower
    if not all(marker in issue_text for marker in ("save", "load", "digest")):
        return None

    for comment in comments or []:
        evidence = _comment_evidence(comment)
        if evidence.author_association not in TRUSTED_ASSOCIATIONS:
            continue

        body = evidence.normalized_body_lower
        if (
            _MAINTAINER_CURRENT_DEFAULT_RE.search(body)
            and _MAINTAINER_SAVE_LOAD_RESOLUTION_RE.search(body)
        ):
            return "trusted maintainer demonstrates current default behavior already resolves issue"
    return None


def maintainer_open_idea_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> str | None:
    """Reject maintainer-authored ideas while trusted maintainers are still deciding."""
    evidence = _issue_evidence(item)
    if evidence.author_association not in TRUSTED_ASSOCIATIONS:
        return None
    if _MAINTAINER_IDEA_HEADING_RE.search(evidence.body) is None:
        return None

    uncertain = False
    opinion_requested = False
    for comment in comments or []:
        comment_evidence = _comment_evidence(comment)
        if comment_evidence.author_association not in TRUSTED_ASSOCIATIONS:
            continue

        body = comment_evidence.normalized_body_lower
        if _explicit_ready_signal(body):
            uncertain = False
            opinion_requested = False
            continue
        if _MAINTAINER_IDEA_UNCERTAINTY_RE.search(body):
            uncertain = True
        if _MAINTAINER_OPINION_REQUEST_RE.search(body):
            opinion_requested = True

    if uncertain and opinion_requested:
        return "maintainer-authored idea still needs implementation decision"
    return None


def maintainer_submission_hold_reason(item: GitHubIssue) -> str | None:
    """Reject trusted maintainer-authored issues that explicitly tell contributors not to PR."""
    evidence = _issue_evidence(item)
    if evidence.author_association not in TRUSTED_ASSOCIATIONS:
        return None

    body = evidence.normalized_body_lower
    if _contains_any(
        body,
        (
            "do not open a pr for this issue",
            "don't open a pr for this issue",
            "do not submit a pr for this issue",
            "don't submit a pr for this issue",
            "do not open a pull request for this issue",
            "don't open a pull request for this issue",
        ),
    ) or _SUBMISSION_CLOSED_RE.search(body):
        return "maintainer explicitly says not to open a PR for this issue"
    return None


def reporter_design_discussion_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> str | None:
    """Reject when the reporter says multiple implementation choices remain under discussion."""
    evidence = _issue_evidence(item)
    reporter = evidence.reporter_login
    if not reporter:
        return None

    reporter_comments = [
        _comment_evidence(comment)
        for comment in comments or []
        if _comment_evidence(comment).login == reporter
    ]
    if not reporter_comments:
        return None

    latest = reporter_comments[-1].normalized_body_lower
    if _explicit_ready_signal(latest):
        return None
    if (
        _REPORTER_OPEN_DESIGN_RE.search(latest)
        and len(_REPORTER_IMPLEMENTATION_CHOICE_RE.findall(evidence.normalized_body_lower)) >= 2
    ):
        return "issue reporter says implementation design is still under discussion"
    return None


def reporter_external_infrastructure_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> str | None:
    """Reject when the reporter's latest status says the failure is external and has no code fix."""
    reporter = _issue_evidence(item).reporter_login
    if not reporter:
        return None

    reporter_comments = [
        _comment_evidence(comment)
        for comment in comments or []
        if _comment_evidence(comment).login == reporter
    ]
    if not reporter_comments:
        return None

    latest = reporter_comments[-1].normalized_body_lower
    if _REPORTER_EXTERNAL_INFRA_RE.search(latest) and _REPORTER_NO_CODE_FIX_RE.search(latest):
        return "issue reporter says failure is external infrastructure with no repository fix"
    return None


def reporter_resolution_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> str | None:
    """Use the issue reporter's latest explicit status to reject already-resolved reports."""
    reporter = _issue_evidence(item).reporter_login
    if not reporter:
        return None

    latest_state: bool | None = None
    for comment in comments or []:
        evidence = _comment_evidence(comment)
        if evidence.login != reporter:
            continue
        body = evidence.normalized_body_lower

        if _contains_any(body, _REPORTER_UNRESOLVED_MARKERS):
            latest_state = False
            continue
        if _contains_any(body, _REPORTER_RESOLVED_MARKERS) or (
            "don't see it" in body
            and _contains_any(body, ("latest version", "latest release", "current version"))
        ):
            latest_state = True

    if latest_state is True:
        return "issue reporter says the problem is already resolved"
    return None


def security_disclosure_reason(item: GitHubIssue) -> str | None:
    """Reject public vulnerability disclosures as normal contributor work."""
    evidence = _issue_evidence(item)
    structured_disclosure = all(
        marker in evidence.body_lower for marker in ("cvss", "cwe-", "disclosure timeline")
    )
    if "security disclosure" in evidence.title_lower or structured_disclosure:
        return "security disclosure, not a normal contributor task"
    return None


def reward_history_reason(item: GitHubIssue) -> str | None:
    """Reject payout summaries/leaderboards that are not open paid work."""
    evidence = _issue_evidence(item)
    leaderboard = "hall-of-fame" in evidence.labels_text or "hall of fame" in evidence.title_lower
    if leaderboard and _contains_any(evidence.body_lower, _PAYOUT_SUMMARY_MARKERS):
        return "bounty history/leaderboard, not an open paid task"
    return None


def reporter_support_triage_reason(item: GitHubIssue) -> str | None:
    """Reject reporter-authored support or unresolved pre-implementation planning."""
    evidence = _issue_evidence(item)
    body = evidence.normalized_body_lower

    if _REPORTER_IMPLEMENTATION_APPROVAL_RE.search(body):
        return "reporter is awaiting maintainer design approval before implementation"

    design_planning_text = f"{evidence.title}\n{evidence.body}"
    if (
        _REPORTER_DESIGN_PLANNING_RE.search(design_planning_text)
        and len(_REPORTER_DESIGN_QUESTION_RE.findall(body)) >= 2
    ):
        return "reporter issue is still defining design/lifecycle semantics"

    guidance_request = (
        "would like to determine whether" in body
        or "would particularly appreciate guidance" in body
        or "would appreciate guidance" in body
    )
    if (
        guidance_request
        and len(_SUPPORT_QUESTION_RE.findall(evidence.body)) >= 3
        or _REPORTER_GUIDANCE_REQUEST_RE.search(body)
    ):
        return "support/triage issue rather than a contributor task"
    return None


def manual_tracking_issue_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Reject explicit umbrella issues that track multiple child implementation tasks."""
    evidence = _issue_evidence(item)
    if (
        evidence.author_association in TRUSTED_ASSOCIATIONS
        and "umbrella issue" in evidence.normalized_body_lower
    ):
        return _REASON_UMBRELLA

    for comment in comments or []:
        comment_evidence = _comment_evidence(comment)
        if (
            comment_evidence.author_association in TRUSTED_ASSOCIATIONS
            and "umbrella issue" in comment_evidence.normalized_body_lower
        ):
            return _REASON_UMBRELLA

    explicit_tracking_container = _TRACKING_CONTAINER_RE.search(evidence.normalized_body_lower)
    child_issue_delegation = _CHILD_DELEGATION_RE.search(evidence.normalized_body_lower)
    if explicit_tracking_container and child_issue_delegation:
        return _REASON_UMBRELLA

    if _TRACKING_INTENT_RE.search(evidence.body) is None:
        return None
    if len(_CHILD_CHECKBOX_RE.findall(evidence.body)) >= 3:
        return _REASON_UMBRELLA
    return None


def automated_tracking_issue_reason(item: GitHubIssue) -> str | None:
    """Reject bot-maintained dashboards/trackers that are not contributor tasks."""
    evidence = _issue_evidence(item)
    login = evidence.reporter_login
    bot_authored = login.endswith("[bot]") or _contains_any(login, ("renovate", "dependabot"))
    if not bot_authored:
        return None

    dependency_tracking = _DEPENDENCY_TRACKING_RE.search(evidence.title_lower) is not None
    renovate_dashboard = (
        "renovate" in f"{login}\n{evidence.body}".lower()
        and "dependencies" in evidence.labels_text
        and "dashboard" in evidence.title_lower
    )
    if dependency_tracking or renovate_dashboard:
        return "automated dependency dashboard, not an implementation task"

    automated_management = _AUTOMATED_MANAGEMENT_RE.search(evidence.body_lower) is not None
    tracking_or_monitoring = _TRACKING_OR_MONITORING_RE.search(evidence.body_lower) is not None
    explicitly_non_actionable = _NON_ACTIONABLE_RE.search(evidence.body_lower) is not None
    if automated_management and tracking_or_monitoring and explicitly_non_actionable:
        return "automated monitoring tracker, not an implementation task"

    ci_incident = (
        "nightly-failure" in evidence.labels_text
        or "this issue closes itself on the next successful" in evidence.body_lower
        or _NEXT_SUCCESS_RE.search(evidence.body_lower) is not None
    )
    if ci_incident:
        return "automated CI/release incident, not an implementation task"
    return None


def release_tracking_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Reject release bookkeeping and work that is already implemented."""
    evidence = _issue_evidence(item)
    title_and_body = f"{evidence.title}\n{evidence.body}".lower()
    republish_only = ("re-release" in title_and_body or "republish" in evidence.body_lower) and (
        "same content as" in evidence.body_lower or "trusted-publisher" in title_and_body
    )
    if republish_only:
        return "existing package content; only release/publication remains"

    if _RELEASE_TRACKING_RE.search(evidence.title_lower) or (
        "announcement" in evidence.labels_text and "release" in evidence.title_lower
    ):
        return "release planning/tracking issue, not implementation work"

    comment_text = "\n".join(str(comment.get("body", "")) for comment in comments or [])
    lifecycle_evidence = f"{evidence.body}\n{comment_text}".lower()
    implementation_done = any(
        pattern.search(lifecycle_evidence) for pattern in _IMPLEMENTATION_DONE_PATTERNS
    )
    release_only = any(pattern.search(lifecycle_evidence) for pattern in _RELEASE_ONLY_PATTERNS)
    if implementation_done and release_only:
        return "implementation already merged; only release/tagging remains"
    return None
