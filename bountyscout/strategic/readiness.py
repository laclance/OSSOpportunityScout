"""Pure strategic readiness and issue-lifecycle policy.

These helpers interpret issue metadata and maintainer comments. They perform no
network I/O and do not depend on the application orchestrator.
"""

from __future__ import annotations

import re
from bountyscout.strategic.claims import normalized_claim_text
from bountyscout.types import GitHubComment, GitHubIssue

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _labels_text(item: GitHubIssue) -> str:
    """Return issue labels as normalized lowercase text."""
    return " ".join(
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in (item.get("labels") or [])
    ).lower()


def triage_pending_signal(labels_text: str) -> bool:
    """Recognize pending-triage label dialects used by target repositories."""
    normalized = re.sub(r"[-_/:]+", " ", labels_text.lower())
    return any(
        marker in normalized
        for marker in (
            "needs triage",
            "triage pending",
            "bug possible",
            "needs analysis",
            "needs investigation",
        )
    )


def issue_label_set(item: GitHubIssue) -> set[str]:
    """Return normalized raw label names without flattening their separators."""
    return {
        (str(label.get("name", "")) if isinstance(label, dict) else str(label)).strip().lower()
        for label in (item.get("labels") or [])
        if (str(label.get("name", "")) if isinstance(label, dict) else str(label)).strip()
    }


def proposal_stage_signal(item: GitHubIssue) -> bool:
    """Recognize explicit proposal/RFC/discussion-stage metadata."""
    title = str(item.get("title", ""))
    normalized_labels = re.sub(r"[-_/:]+", " ", _labels_text(item))
    return bool(
        re.search(r"\b(?:proposal|rfc)\b", title, re.IGNORECASE)
        or any(
            marker in normalized_labels
            for marker in (
                "kind proposal",
                "type proposal",
                "status proposal",
                "rfc",
                "needs discussion",
                "discussion",
            )
        )
    )


def maintainer_comment_authority(comment: GitHubComment) -> bool:
    """Trust maintainer associations plus explicit project-action comments."""
    association = str(comment.get("author_association", "")).upper()
    if association in TRUSTED_ASSOCIATIONS:
        return True
    if association != "CONTRIBUTOR":
        return False

    body = str(comment.get("body", "")).lower()
    return any(
        marker in body
        for marker in (
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
    )


def _diagnostic_evidence_supplied(body: str) -> bool:
    """Return whether a commenter supplied evidence previously requested by maintainers."""
    return bool(
        re.search(
            r"\b(?:attached|provided|uploaded|included|here(?:'s| is)|see)\b"
            r".{0,160}\b(?:cpu profile|memory profile|heap profile|profile|stack trace|"
            r"minimal reproducer|reproducer|logs?|benchmark|trace|dump)\b",
            body,
            re.DOTALL,
        )
    )


def _diagnostic_requested(body: str) -> bool:
    """Return whether a maintainer explicitly requested concrete diagnostic evidence."""
    request = re.search(
        r"\b(?:could|can|would)\s+you\s+(?:please\s+)?"
        r"(?:provide|share|attach|capture|collect|send)\b|"
        r"\bwould\s+it\s+be\s+possible\s+to\s+(?:provide|share|attach)\b|"
        r"\bplease\s+(?:provide|share|attach|capture|collect|send)\b|"
        r"\bwe\s+need\s+(?:a|the)\b",
        body,
    )
    evidence = re.search(
        r"\b(?:cpu|memory|heap)\s+profiles?\b|"
        r"\bprofiles?\s+from\b|"
        r"\bstack traces?\b|"
        r"\bminimal reproduc(?:er|tion)\b|"
        r"\breproducers?\b|"
        r"\blogs?\s+(?:from|required|showing)\b|"
        r"\b(?:node|instance|daemon|pod|container)\s+logs?\b|"
        r"\bbenchmarks?\b|"
        r"\btraces?\b|"
        r"\bdumps?\b",
        body,
    )
    return bool(request and evidence)


def _redirects_to_other_project(body: str) -> bool:
    """Return whether the maintainer redirects the actual change to another project."""
    if "closing it here" in body and re.search(
        r"\b(?:i think\s+)?we should do it in core\b",
        body,
    ):
        return True

    redirect_target = bool(
        re.search(
            r"https://github\.com/[\w.-]+/[\w.-]+|"
            r"(?<![\w.-])[\w.-]+/[\w.-]+(?![\w.-])|"
            r"\b(?:specification|upstream|another repository|another project|"
            r"canonical repository)\b",
            body,
        )
    )
    if not redirect_target:
        return False
    return bool(
        re.search(
            r"\brequires?\s+(?:a\s+)?(?:change|fix)\s+in\b|"
            r"\bneeds?\s+to\s+be\s+(?:fixed|changed|implemented)\s+in\b|"
            r"\bplease\s+open\s+(?:a\s+)?(?:ticket|issue)\s+(?:there|against)\b|"
            r"\bbest\s+to\s+open\s+(?:a\s+)?(?:ticket|issue)\s+there\b|"
            r"\b(?:implementation|change|fix)\s+belongs\s+in\b|"
            r"\bthis\s+is\s+an?\s+upstream\s+issue\b",
            body,
        )
    )


def _canonical_duplicate(body: str) -> bool:
    """Return whether a maintainer points to another issue as the canonical tracker."""
    canonical_reference = bool(
        re.search(
            r"(?<!\w)#\d+\b|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+",
            body,
        )
    )
    if not canonical_reference or "not a duplicate" in body:
        return False
    return bool(
        re.search(
            r"\blooks?\s+like\s+(?:a\s+)?(?:\(?possible\)?\s+)?"
            r"duplicate\s+of\b|"
            r"\bpossible\s+duplicate\s+of\b|"
            r"\btracked\s+in\s+(?:issue\s+)?"
            r"(?:#|https://github\.com/)|"
            r"\bdiscussion\s+is\s+(?:being\s+)?tracked\s+there\b|"
            r"\bplease\s+continue\s+(?:this|the discussion)\s+in\b",
            body,
        )
    )


def _explicit_ready_signal(body: str) -> bool:
    """Return whether a maintainer explicitly says implementation may proceed."""
    return any(
        marker in body
        for marker in (
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
    )


def _maintainer_hold_reason(body: str, proposal_stage: bool) -> tuple[str | None, bool]:
    """Return a not-ready reason and whether it represents pending diagnostics."""
    diagnostic_requested = _diagnostic_requested(body)
    if diagnostic_requested:
        return "maintainer is waiting for requested diagnostic evidence", True
    if _redirects_to_other_project(body):
        return "maintainer redirected implementation/discussion to another project", False
    if _canonical_duplicate(body):
        return "maintainer indicates this is probably tracked by another canonical issue", False
    if any(
        marker in body
        for marker in (
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
    ):
        return "maintainer indicates the proposed implementation approach is not wanted", False
    if any(
        marker in body
        for marker in (
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
    ):
        return "maintainer says issue still needs discussion", False
    if any(
        marker in body
        for marker in (
            "needs investigation",
            "need more investigation",
            "need to investigate",
            "needs engineering investigation",
        )
    ):
        return "maintainer says issue still needs investigation", False
    if any(
        marker in body
        for marker in (
            "needs reproduction",
            "need a reproduction",
            "need reproduction",
            "please reproduce",
            "can you reproduce",
        )
    ) or ("are you sure" in body and "reproduc" in body):
        return "maintainer says reproduction is still required", False
    if any(
        marker in body
        for marker in (
            "not ready for implementation",
            "not ready to implement",
            "please wait before implementing",
            "please wait to implement",
            "hold off on implementation",
            "hold off implementing",
            "do not start implementation",
            "don't start implementation",
        )
    ):
        return "maintainer asked contributors to wait before implementation", False
    if any(
        marker in body
        for marker in (
            "needs clarification",
            "need clarification",
            "need to clarify",
            "please clarify before",
        )
    ):
        return "maintainer says issue still needs clarification", False
    if proposal_stage and any(
        marker in body
        for marker in (
            "gauge community interest",
            "gather feedback",
            "collect feedback",
            "community time to weigh in",
            "reevaluate based on the feedback",
            "re-evaluate based on the feedback",
            "before committing",
        )
    ):
        return "proposal is still gathering feedback", False
    if proposal_stage and "time-boxed" in body and "discussion" in body:
        return "proposal is still gathering feedback", False
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
        body = normalized_claim_text(str(comment.get("body", ""))).lower()

        if diagnostic_pending and _diagnostic_evidence_supplied(body):
            state = None
            reason = None
            diagnostic_pending = False

        if not maintainer_comment_authority(comment):
            continue

        if _explicit_ready_signal(body):
            state = True
            reason = None
            diagnostic_pending = False
            continue

        hold_reason, diagnostic_requested = _maintainer_hold_reason(body, proposal_stage)
        if hold_reason:
            state = False
            reason = hold_reason
            diagnostic_pending = diagnostic_requested

    return state, reason


def readiness_pending_label_reason(
    item: GitHubIssue,
    ready_override: bool = False,
) -> str | None:
    """Reject explicit not-ready label states unless readiness is overridden."""
    if ready_override:
        return None

    normalized = [re.sub(r"[-_/:]+", " ", label) for label in issue_label_set(item)]
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
        if any(marker in label for label in normalized):
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
    """Reject trusted maintainer-authored issues that explicitly remain in decision stage."""
    association = str(item.get("author_association", "")).upper()
    if association not in TRUSTED_ASSOCIATIONS:
        return None

    body = str(item.get("body", "")).lower()
    if re.search(
        r"\b(?:this|the) issue is to decide\b|"
        r"\bwe (?:still )?need to decide\b|"
        r"\bdecision (?:is|remains) (?:open|pending)\b",
        body,
    ):
        return "maintainer-authored issue is still deciding implementation semantics"
    return None


def maintainer_submission_hold_reason(item: GitHubIssue) -> str | None:
    """Reject trusted maintainer-authored issues that explicitly tell contributors not to PR."""
    association = str(item.get("author_association", "")).upper()
    if association not in TRUSTED_ASSOCIATIONS:
        return None

    body = normalized_claim_text(str(item.get("body", ""))).lower()
    if any(
        marker in body
        for marker in (
            "do not open a pr for this issue",
            "don't open a pr for this issue",
            "do not submit a pr for this issue",
            "don't submit a pr for this issue",
            "do not open a pull request for this issue",
            "don't open a pull request for this issue",
        )
    ) or re.search(
        r"\b(?:a |the )?(?:pr|pull request).{0,80}\bwill be closed\b",
        body,
        re.DOTALL,
    ):
        return "maintainer explicitly says not to open a PR for this issue"
    return None


def reporter_resolution_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None,
) -> str | None:
    """Use the issue reporter's latest explicit status to reject already-resolved reports."""
    reporter = str((item.get("user") or {}).get("login", "")).lower()
    if not reporter:
        return None

    latest_state: bool | None = None
    for comment in comments or []:
        login = str((comment.get("user") or {}).get("login", "")).lower()
        if login != reporter:
            continue
        body = normalized_claim_text(str(comment.get("body", ""))).lower()

        if any(
            marker in body
            for marker in (
                "still reproduces",
                "still reproducible",
                "not fixed",
                "isn't fixed",
                "issue is still present",
                "problem is still present",
            )
        ):
            latest_state = False
            continue

        if any(
            marker in body
            for marker in (
                "issue got fixed",
                "issue is fixed",
                "problem got fixed",
                "problem is fixed",
                "no longer reproduces",
                "can't reproduce anymore",
                "cannot reproduce anymore",
            )
        ) or (
            "don't see it" in body
            and any(
                marker in body for marker in ("latest version", "latest release", "current version")
            )
        ):
            latest_state = True

    if latest_state is True:
        return "issue reporter says the problem is already resolved"
    return None


def security_disclosure_reason(item: GitHubIssue) -> str | None:
    """Reject public vulnerability disclosures as normal contributor work."""
    title = str(item.get("title", "")).lower()
    body = str(item.get("body", "")).lower()

    explicit = "security disclosure" in title
    structured_disclosure = all(
        marker in body
        for marker in (
            "cvss",
            "cwe-",
            "disclosure timeline",
        )
    )
    if explicit or structured_disclosure:
        return "security disclosure, not a normal contributor task"
    return None


def reward_history_reason(item: GitHubIssue) -> str | None:
    """Reject payout summaries/leaderboards that are not open paid work."""
    title = str(item.get("title", "")).lower()
    body = str(item.get("body", "")).lower()
    labels = _labels_text(item)

    leaderboard = "hall-of-fame" in labels or "hall of fame" in title
    payout_summary = any(
        marker in body
        for marker in (
            "total bounty distributed",
            "top contributors",
            "fastest fixes",
            "monthly stats",
        )
    )
    if leaderboard and payout_summary:
        return "bounty history/leaderboard, not an open paid task"
    return None


def reporter_support_triage_reason(item: GitHubIssue) -> str | None:
    """Reject reporter-authored diagnostic/support requests without defined implementation."""
    body = str(item.get("body", ""))
    normalized = normalized_claim_text(body).lower()
    guidance_request = (
        "would like to determine whether" in normalized
        or "would particularly appreciate guidance" in normalized
        or "would appreciate guidance" in normalized
    )
    question_count = len(
        re.findall(
            r"(?m)^\s*\d+\.\s+(?:is|are|could|would|should|can|do|does)\b.*\?\s*$",
            body,
            re.IGNORECASE,
        )
    )
    if guidance_request and question_count >= 3:
        return "support/triage issue rather than a contributor task"
    return None


def manual_tracking_issue_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Reject explicit umbrella issues that track multiple child implementation tasks."""
    author_association = str(item.get("author_association", "")).upper()
    normalized_issue_body = normalized_claim_text(str(item.get("body", ""))).lower()
    if author_association in TRUSTED_ASSOCIATIONS and "umbrella issue" in normalized_issue_body:
        return "umbrella tracking issue, not a single implementation task"

    for comment in comments or []:
        association = str(comment.get("author_association", "")).upper()
        body = normalized_claim_text(str(comment.get("body", ""))).lower()
        if association in TRUSTED_ASSOCIATIONS and "umbrella issue" in body:
            return "umbrella tracking issue, not a single implementation task"

    body = str(item.get("body", ""))
    normalized_body = normalized_claim_text(body).lower()
    explicit_tracking_container = bool(
        re.search(
            r"\b(?:use|using)\s+(?:this|the)\s+issue\s+for\s+tracking\b",
            normalized_body,
        )
    )
    child_issue_delegation = bool(
        re.search(
            r"\b(?:create|open|file)\s+(?:sub[- ]?issues?|child issues?|separate issues?)\b",
            normalized_body,
        )
    )
    if explicit_tracking_container and child_issue_delegation:
        return "umbrella tracking issue, not a single implementation task"

    tracking_intent = bool(
        re.search(
            r"\b(?:track and resolve|track the following|tracking issue for)\b",
            body,
            re.IGNORECASE,
        )
    )
    if not tracking_intent:
        return None

    child_issue_refs = re.findall(
        r"(?m)^\s*[-*]\s*\[[ xX]\]\s*#\d+\b",
        body,
    )
    if len(child_issue_refs) >= 3:
        return "umbrella tracking issue, not a single implementation task"
    return None


def automated_tracking_issue_reason(item: GitHubIssue) -> str | None:
    """Reject bot-maintained dashboards/trackers that are not contributor tasks."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = _labels_text(item)
    login = str((item.get("user") or {}).get("login", "")).lower()
    bot_authored = login.endswith("[bot]") or any(
        marker in login for marker in ("renovate", "dependabot")
    )
    if not bot_authored:
        return None

    title_text = title.lower()
    dependency_tracking = bool(
        re.search(
            r"\b(?:dependency|dependencies)\s+(?:dashboard|tracker|tracking)\b",
            title_text,
        )
    )
    renovate_dashboard = (
        "renovate" in f"{login}\n{body}".lower()
        and "dependencies" in labels
        and "dashboard" in title_text
    )
    if dependency_tracking or renovate_dashboard:
        return "automated dependency dashboard, not an implementation task"

    body_text = body.lower()
    automated_management = bool(
        re.search(
            r"\b(?:automatically managed|managed automatically|auto-managed)\b",
            body_text,
        )
    )
    tracking_or_monitoring = bool(
        re.search(
            r"\b(?:tracks?|tracking|monitors?|monitoring)\b",
            body_text,
        )
    )
    explicitly_non_actionable = bool(
        re.search(
            r"\b(?:no action (?:to take|is )?required|no action to take|"
            r"do not assign|don't assign)\b",
            body_text,
        )
    )
    if automated_management and tracking_or_monitoring and explicitly_non_actionable:
        return "automated monitoring tracker, not an implementation task"

    ci_incident = (
        "nightly-failure" in labels
        or "this issue closes itself on the next successful" in body_text
        or bool(
            re.search(
                r"\bnext successful\s+(?:build|publish|publication|run)"
                r"\s+will\s+(?:update|close)\s+this\s+(?:issue|incident)\b",
                body_text,
            )
        )
    )
    if ci_incident:
        return "automated CI/release incident, not an implementation task"
    return None


def release_tracking_reason(
    item: GitHubIssue,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Reject release bookkeeping and work that is already implemented."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    tracking_text = title.lower()
    labels = _labels_text(item)
    republish_only = (
        "re-release" in f"{title}\n{body}".lower() or "republish" in body.lower()
    ) and ("same content as" in body.lower() or "trusted-publisher" in f"{title}\n{body}".lower())
    if republish_only:
        return "existing package content; only release/publication remains"

    if re.search(
        r"\brelease(?:\s+\S+){0,2}\s+(?:tracking|tracker|checklist|planning)\b|"
        r"\b(?:tracking|tracker|checklist)\s+(?:for\s+)?release\b|"
        r"\bplan(?:ned|ning)?\s+to\s+release\b|"
        r"\bplanned(?:\s+\S+){0,3}\s+release\b",
        tracking_text,
    ) or ("announcement" in labels and "release" in tracking_text):
        return "release planning/tracking issue, not implementation work"

    comment_text = "\n".join(str(comment.get("body", "")) for comment in comments or [])
    evidence = f"{body}\n{comment_text}".lower()
    implementation_done = any(
        re.search(pattern, evidence, re.IGNORECASE | re.DOTALL)
        for pattern in (
            r"\bpr\s*#\d+.{0,120}\bmerged\b",
            r"\balready\s+(?:merged|fixed|implemented|resolved)\b",
            r"\b(?:seems|appears)\s+to\s+be\s+resolved\s+by\b",
            r"\b(?:is|was)\s+resolved\s+by\b",
            r"\b(?:fixed|implemented|resolved)\s+(?:on|in)\s+(?:the\s+)?(?:main|master)\b",
            r"\b(?:fix|implementation)\s+(?:is|has\s+been)\s+(?:already\s+)?(?:on|in)\s+(?:main|master)\b",
            r"\b(?:version|release)\s+update\s+was\s+merged\b",
        )
    )
    release_only = any(
        re.search(pattern, evidence, re.IGNORECASE | re.DOTALL)
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
    if implementation_done and release_only:
        return "implementation already merged; only release/tagging remains"
    return None
