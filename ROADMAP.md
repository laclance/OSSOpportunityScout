# Roadmap

This file is intentionally forward-looking.

- `README.md` explains what the project does and how to run it.
- `CONTRIBUTING.md` is the human contributor guide.
- `AGENTS.md` contains AI coding-agent execution rules.
- `ARCHITECTURE.md` describes the current system and its invariants.
- [Private deployment migration](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) owns the agreed migration contracts, ordered PR slices, prerequisites, recovery procedure, and progress.

## Current baseline

OSS Opportunity Scout is a package-owned scanner with:

- a stable `python opportunity_scout.py` entry point
- typed/versioned transactional seen-state
- a legacy workflow contract requiring `scout-state`, absent remotely as verified on 2026-10-04
- privacy-verified private GitHub report delivery
- public host-repository reports disabled by default
- hardened GitHub REST identity, safe-read retry behavior, and evidence-sensitive pagination
- strict Ruff, mypy, and 100% statement/branch coverage gates

The legacy upstream workflow is **manual-only via `workflow_dispatch`**, but cannot restore state while the required remote branch is absent. The coverage-completeness discrepancy documented in the tracker also remains an implementation prerequisite.

Native GitHub `schedule` events are paused while GitHub investigates a reproducible scheduler-delivery failure. The preserved reproduction repository is `laclance/actions-scheduler-probe`.

## Near term

### Separate the public distribution from private scout instances

`laclance/OSSOpportunityScout` remains the canonical public source of truth for code, development/release CI, and reusable execution machinery. It must not operate any persistent scout instance. The independent private instance owns the actual workflow, configuration/state, triggers/schedules, concurrency, secrets, delivery configuration, state persistence/history, and scanner version pin. Forks are optional for scanner-code customization, not runtime state ownership.

Implement the [migration tracker](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) through its focused PR slices:

- raise the minimum runtime/tooling baseline to Python 3.12 and add CI compatibility checks for every stable CPython release >=3.12 available when the slice lands
- fix coverage completeness in its separate prerequisite PR
- add immutable, versioned preferences and wire configuration incrementally
- provide pinned public execution machinery and a generic private-instance template
- recover historical state and prove the private instance before removing upstream production responsibilities

Do not restore upstream scheduling or recreate upstream `scout-state` ownership as the long-term remedy. These are superseded by private instance ownership. Initial private deployment remains manual-only; any future schedule belongs to that private repository. The scheduler probe remains historical evidence, not a reason to restore an upstream scout deployment.

**Immediate next task: Slice 1 only.** Preserve the current Python 3.11+ contract until that coordinated runtime PR; configuration and deployment implementation belong to later slices. Each slice requires merged prerequisites, the full quality gate, and an open-PR stopping boundary.

### Establish a release baseline

Consider tagging the current project state as the first stable OSS release once the desired public release/versioning convention is chosen.

A release should describe its actual migration status, private-reporting model, and GitHub API contract. Private deployments select deliberate scanner versions; upstream merges must not silently upgrade their instances.

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
- remaining application compatibility-wrapper cleanup, separate from this migration
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
- the canonical public repository remains a distribution/development repository; persistent scout workflows, state, schedules, and delivery configuration belong to private instances
- private instances start manual-only; schedules are a deliberate instance-owned follow-up
