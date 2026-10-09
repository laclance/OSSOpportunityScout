# Roadmap

This file is intentionally forward-looking.

- `README.md` explains what the project does and how to run it.
- `CONTRIBUTING.md` is the human contributor guide.
- `AGENTS.md` contains AI coding-agent execution rules.
- `ARCHITECTURE.md` describes the current system and its invariants.
- [Private deployment migration](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) is the completed migration/acceptance record, including historical contracts, PR provenance, recovery, and ownership rationale.

## Current baseline

OSS Opportunity Scout is a package-owned scanner with:

- a stable `python opportunity_scout.py` entry point
- typed/versioned transactional seen-state
- upstream development CI with read permissions; instance-owned execution/state, with no upstream state-branch/worktree contract
- privacy-verified private GitHub report delivery
- public host-repository reports disabled by default
- hardened GitHub REST identity, safe-read retry behavior, and evidence-sensitive pagination
- strict Ruff, mypy, and 100% statement/branch coverage gates
- Python 3.12 as the minimum runtime and authoritative quality baseline, with compile/test compatibility CI on CPython 3.13 and 3.14
- immutable non-secret preferences, a strict version-1 TOML parser, and a generic example
- mandatory default `scout.toml` or explicit `--config PATH`, independent `--state PATH`, and repository/lane/strategic global-search controls
- primary repository-language preferences across both lanes with cached metadata
- configured strategic repositories expand discovery sources without extending the built-in target-repository scoring bonus
- exact final-effort preferences across both lanes, applied before selection limits
- inclusive final-classification score thresholds and a 1–8 result limit
- a [configuration reference](docs/CONFIGURATION.md) with generic examples and offline validation
- a [public action and generic manual-only private template](docs/PRIVATE_INSTANCE.md) with full-SHA-pinned external dependencies, main-or-reviewed-SHA scanner selection, complete-transaction serialization, stale-write rejection, private recovery artifacts, and a durable recovery barrier

Coverage completeness is independent of warning thresholds, including quiet-run
maintenance. The distributed private workflow template uses a durable recovery
marker after persistence failure; deployed instances must adopt and validate that
template before relying on this barrier. Authoritative direct-source language
filtering occurs before bounded adaptive inspection. Historical state remains private; public examples never replace that
history.

## Deployment baseline

### Public distribution and private scout instances

`laclance/OSSOpportunityScout` remains the canonical public source of truth for code, development/release CI, and reusable execution machinery. It must not operate any persistent scout instance. The independent private instance owns the actual workflow, configuration/state, triggers/schedules, concurrency, secrets, delivery configuration, state persistence/history, and scanner pin. Forks are optional for scanner-code customization, not runtime state ownership.

The [completed migration record](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) preserves
the implementation, acceptance, and recovery history behind this ownership model.
Private instances explicitly choose whether to follow upstream `main` on each scan or select a reviewed SHA; following `main` intentionally adopts new public changes automatically.

Do not restore upstream scheduling or `scout-state` ownership. Any future schedule
belongs to the private instance and requires a deliberate operational decision.
Forks remain optional for code customization; the generic selector checks out
public upstream `main` or a reviewed full SHA and records the resolved commit.

### Maintain the release baseline

The stable OSS release baseline is established. Reviewed SemVer releases identify promoted `main` commits; use the [latest GitHub release](https://github.com/laclance/OSSOpportunityScout/releases/latest) for the current published baseline rather than duplicating a version number here.

Release notes should describe the current ownership model, private-reporting model, and GitHub API contract. Private deployments select deliberate scanner versions; upstream merges must not silently upgrade their instances.

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
- revisit remaining app compatibility wrappers only when their production, injection, and external consumers are established; three unused strategic-discovery delegates were removed
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
