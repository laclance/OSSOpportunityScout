from __future__ import annotations

import re
from typing import Any

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
EXTERNAL_REUSABLE_WORKFLOW = re.compile(
    r"^[^/@]+/[^/@]+/\.github/workflows/[^@]+@[0-9a-f]{40}$"
)
LOCAL_REUSABLE_WORKFLOW = re.compile(r"^(?:\./|\$/)\.github/workflows/[^@]+$")


def _validate_reusable_workflow(reference: object) -> None:
    if not isinstance(reference, str):
        raise ValueError(f"invalid reusable workflow reference: {reference!r}")
    if LOCAL_REUSABLE_WORKFLOW.fullmatch(reference) is not None:
        return
    if EXTERNAL_REUSABLE_WORKFLOW.fullmatch(reference) is not None:
        return
    raise ValueError(
        "reusable workflow reference must use same-repository same-commit syntax "
        f"or an external full SHA: {reference}"
    )


def validate_workflow_references(
    workflow: dict[str, Any],
    *,
    approved_actions: dict[str, str] | None = None,
) -> None:
    seen_actions: set[str] = set()
    for job in workflow["jobs"].values():
        if "uses" in job:
            _validate_reusable_workflow(job["uses"])

        for step in job.get("steps", []):
            reference = step.get("uses")
            if reference is None or reference.startswith("./"):
                continue
            action, separator, revision = reference.partition("@")
            if separator != "@":
                raise ValueError(f"missing action revision: {reference}")
            if FULL_SHA.fullmatch(revision) is None:
                raise ValueError(f"action revision is not a full SHA: {reference}")
            if approved_actions is not None:
                if revision != approved_actions.get(action):
                    raise ValueError(f"unapproved action reference: {reference}")
                seen_actions.add(action)

    if approved_actions is not None and seen_actions != set(approved_actions):
        raise ValueError(
            f"approved action set mismatch: expected {set(approved_actions)}, got {seen_actions}"
        )
