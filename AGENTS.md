# AI Agent Instructions

This file contains execution rules for AI coding agents working on OSS Opportunity Scout. Human contributors should start with `CONTRIBUTING.md`; shared technical truth lives in `ARCHITECTURE.md`.

## Read this context first

1. `README.md` — what the scout does and how to run it.
2. `ARCHITECTURE.md` — current data flow, module boundaries, and invariants.
3. `ROADMAP.md` — deliberate future improvements and sequencing.
4. `CONTRIBUTING.md` — shared setup, quality, and pull-request expectations.
5. Relevant tests under `tests/` — executable behavior and regression cases.

For migration work, additionally read [docs/PRIVATE_DEPLOYMENT_MIGRATION.md](docs/PRIVATE_DEPLOYMENT_MIGRATION.md), the authoritative tracker for contracts, PR slices, prerequisites, historical recovery, and progress.

## Project constraints

- Python 3.11+ currently; migration Slice 1 raises the runtime/tooling contract together to Python 3.12+. Do not change that contract or begin later slices in a documentation-only change.
- Canonical current package identity is `opportunity_scout`. Do not reintroduce `bountyscout` package/import/report-marker branding.
- Prefer the standard library unless a dependency has a clear maintenance payoff.
- Ruff is the formatter/linter, mypy runs in strict mode, and coverage requires 100% statement and branch coverage.
- Production implementation lives under `opportunity_scout/`; strategic policy lives under `opportunity_scout/strategic/`. Root `opportunity_scout.py` is only the stable executable shim.
- Never weaken paid-bounty verification while changing strategic discovery.
- Treat existing seen-state corruption conservatively: malformed, unreadable, or unsupported state must fail the run rather than silently becoming empty.
- Advance newly reported seen-state only when combined coverage is complete and at least one configured delivery channel succeeds. A failed channel does not count as successful delivery, but another configured channel may satisfy the aggregate delivery result. For the optional host-report channel, issue creation plus auto-close must both succeed for that channel to count as delivered.
- Keep GitHub API authentication separate from report delivery. `GITHUB_TOKEN` and `GITHUB_REPOSITORY` must never imply permission to publish ranked scout results; host-repository GitHub reports require explicit opt-in and must default off. Private GitHub reports require their own repository and credential, and delivery must fail closed unless GitHub metadata retrieved with that credential explicitly reports `private: true`.
- Keep `opportunity_scout.state` branch-agnostic. Git/worktree/`scout-state` transport belongs to the workflow, not Python state code.
- Never equate a lifecycle check failure, malformed response, auth/rate-limit error, or GitHub 404 with a confirmed closed issue. Only confirmed `closed` lifecycle evidence may prune a GitHub seen-state entry.
- Keep seen-state maintenance bounded and deterministic. Do not add blind TTL expiry or unbounded first-run revalidation; non-GitHub entries stay seen until they have an explicit platform lifecycle policy.
- Treat `last_checked_at` as the time a maintenance attempt was made, not proof that lifecycle confirmation succeeded.
- The target canonical public upstream is a distribution/development repository, not a persistent scout instance. Private instance repositories own actual scout workflows, configuration/state, triggers/schedules, concurrency, secrets, delivery configuration, persistence/history, and scanner pins. Forks are optional for code customization. Do not make public workflows reach into private repositories for scout state.
- The current warning threshold permits state advancement after one to four recognized strategic verification failures. This is a known violation of the complete-coverage requirement, not an allowed exception; fix it in the tracker’s separate Slice 1B without weakening verification or quality gates.

## Migration execution

Follow the tracker in order. Verify actual latest `main`, merged prerequisite PRs, worktree state, and the quality baseline before branching for each slice. Keep configuration initially opt-in and current invocation behavior available until verified cutover. Do not recreate upstream `scout-state` ownership as the final remedy, reset historical state to the empty example, or restore upstream scheduling.

Preserve historical state privately and prove the private instance before retiring upstream production responsibilities. Keep private opportunity contents and scout credentials out of public files and PRs. Complete one focused slice, run the full quality gate, commit, open its PR, and stop at the open-PR boundary; do not merge or start a later slice implicitly.

## Design rules

- Give each function one clear responsibility. Prefer functions that fit comfortably on one screen; split a function when it mixes policy, I/O, parsing, scoring, or formatting.
- Keep network and filesystem side effects at the edges. Put matching, scoring, normalization, and policy decisions in pure functions when practical.
- New domain logic belongs in the smallest cohesive module that owns that concept. Leaf/domain modules must not import root `opportunity_scout.py` or reach back into `opportunity_scout.app`.
- Package modules must not depend on root executable shims. Keep `opportunity_scout.py` thin and pass narrow typed callables/values across package boundaries when orchestration needs injection.
- Use explicit type hints on function signatures and meaningful domain names. Prefer `Mapping` for read-only mapping inputs.
- Put broadly shared stable mapping records and literals in `opportunity_scout/types.py`. Keep raw external JSON dynamic until validated, then prefer canonical domain types over repeated `dict[str, Any]` / `Mapping[str, Any]` interfaces.
- Avoid new mutable global state. Static configuration constants are fine; shared runtime state needs an explicit reason and synchronization where applicable.
- Docstrings should explain intent, invariants, or surprising behavior. Do not add ceremonial `Args`/`Returns` sections that only repeat obvious type hints.
- Preserve behavior during refactors. Move code first; change behavior in a separate, test-backed step when possible.
- Add regression tests for every functional bug or scanner false positive/negative that motivates a change.
- Scanner policy modules should have direct unit regressions for their decision boundaries, plus orchestration-level coverage when composition changes.
- Unit tests must not depend on live network access.

## Change protocol

Before calling a change complete:

1. Finish the logical code, test, and documentation edits first.
2. If Python changed, run `make format` once across the completed worktree.
3. Update `ARCHITECTURE.md` when responsibilities, dependencies, data flow, or invariants change.
4. Update `README.md` or `CONTRIBUTING.md` when user-facing commands or contributor workflow changes.
5. Run `make quality`.
6. Commit the finalized result together with the logical change where practical.

Avoid mechanical one-file-at-a-time formatter churn or a new commit for each Ruff correction. Legitimate follow-up commits are fine when behavior or substance changes; do not require contributors to rewrite ordinary history solely to satisfy this preference. When using repository/API tooling, batch related finalized file changes into one commit/tree where practical.
