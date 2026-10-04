# Roadmap

This file is intentionally forward-looking.

- `README.md` explains what the project does and how to run it.
- `CONTRIBUTING.md` is the human contributor guide.
- `AGENTS.md` contains AI coding-agent execution rules.
- `ARCHITECTURE.md` describes the current system and its invariants.

## Current baseline

OSS Opportunity Scout is a package-owned scanner with:

- a stable `python opportunity_scout.py` entry point
- typed/versioned transactional seen-state
- a dedicated `scout-state` persistence branch
- privacy-verified private GitHub report delivery
- public host-repository reports disabled by default
- hardened GitHub REST identity, safe-read retry behavior, and evidence-sensitive pagination
- strict Ruff, mypy, and 100% statement/branch coverage gates

Production is currently **manual-only via `workflow_dispatch`**.

Native GitHub `schedule` events are paused while GitHub investigates a reproducible scheduler-delivery failure. The preserved reproduction repository is `laclance/actions-scheduler-probe`.

## Near term

### Restore native scheduling only when GitHub's scheduler path is proven healthy

Keep production manual-only until the external scheduler issue is resolved.

When native scheduling is reconsidered:

- verify the minimal probe receives real `schedule` events
- restore the production cron in a focused change
- preserve private-only report delivery and transactional state behavior
- verify the first automatic production run before treating scheduling as restored

Do not add an external scheduler unless the deployment strategy is explicitly changed.

### Establish a release baseline

Consider tagging the current project state as the first stable OSS release once the desired public release/versioning convention is chosen.

A release should summarize the private-reporting model, GitHub API contract, and manual-only production status.

## Product improvements

Prioritize improvements that materially increase the quality of the opportunity queue:

- use report/audit feedback to reduce false positives and false negatives
- improve source adapters where they add unique paid or strategic opportunities
- refine ranking only with regression-backed evidence
- improve report readability and decision support without exposing private results publicly

Preserve the separation between discovery, verification, scoring, delivery, and state.

## Integration and API work

The current GitHub REST transport is intentionally pinned and hardened. Future API work should be driven by observed need rather than churn.

Potential follow-ups include:

- reconsider conditional requests if request volume grows enough to justify persistent validator/body ownership
- revisit the pinned REST API version only as explicit compatibility work
- consider a GitHub App only if the project becomes installable across other users or organizations

Do not move scanner discovery onto the private-report credential.

## Deferred architecture work

Consider these only when they produce a concrete maintenance benefit:

- immutable result objects for selected mapping-heavy internal results
- relocating `seen_bounties.json` if a future deployment model benefits from a different state path
- lightweight dependency-direction checks if architectural drift becomes recurring

Do not perform broad type/model rewrites or custom architecture tooling merely for stylistic consistency.

## Ongoing invariants

Future work must preserve:

- ranked scout results are not published publicly by default
- private GitHub delivery fails closed unless the destination is verified private
- scanner and private-report credentials remain separate
- incomplete coverage or unsuccessful aggregate delivery does not commit newly reported opportunities
- GitHub mutations are not blindly retried
- state maintenance remains bounded and conservative
- production remains manual-only until scheduling is deliberately restored
