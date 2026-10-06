# OSS Opportunity Scout

[![Python Quality](https://github.com/laclance/OSSOpportunityScout/actions/workflows/python-quality.yml/badge.svg?branch=main)](https://github.com/laclance/OSSOpportunityScout/actions/workflows/python-quality.yml)
[![Latest release](https://img.shields.io/github/v/release/laclance/OSSOpportunityScout?display_name=tag)](https://github.com/laclance/OSSOpportunityScout/releases/latest)
[![License](https://img.shields.io/github/license/laclance/OSSOpportunityScout)](LICENSE)

**Find and rank paid open-source bounties and high-value GitHub contribution opportunities automatically with GitHub Actions.**

OSS Opportunity Scout helps developers find open-source issues worth doing across two lanes:

- **Paid opportunities / cash now:** explicit paid open-source bounties and sponsored issues with payment evidence.
- **Career-value contribution opportunities:** bounded, contributor-ready issues in respected infrastructure/backend repositories.

**[View OSS Opportunity Scout on GitHub Marketplace →](https://github.com/marketplace/actions/oss-opportunity-scout)**

## Find open-source work worth doing

- Discovers explicit paid bounties, sponsored issues, and other paid open-source issues backed by payment evidence.
- Finds bounded, contributor-ready strategic OSS work with strong career value.
- Filters work that is assigned, actively claimed, already implemented, stale/non-actionable, or otherwise unsuitable.
- Ranks opportunities using payment or career value, effort, visible competition, repository quality/activity, and related verification signals.
- Supports automated GitHub Actions execution and configured private delivery.

## Quick start

The public repository is the scanner source and distribution point, not a persistent scout instance. For supported recurring execution, use an independent private instance that owns its configuration, state, secrets, delivery, triggers, and scanner pin.

1. Follow the [private instance guide](docs/PRIVATE_INSTANCE.md) to create or configure the independent private instance.
2. Keep configuration, state, and secrets instance-owned, and consume scanner source through a reviewed immutable full SHA.
3. Run the private workflow with your chosen delivery channels; public host-repository reports remain disabled by default.

Forking is optional and intended only for scanner-code customization.

## Illustrative ranked result

> **Illustrative example — synthetic, not a current bounty.**<br>
> `example/project#123` — Improve retry handling in a contributor-facing CLI<br>
> **Paid:** $250 bounty with explicit reward evidence · **Cash score:** 82 · **Career score:** 74<br>
> **Effort:** 3–6h · **Competition:** low<br>
> **Why it ranked:** funded, actionable scope, healthy repository activity, clear maintainer signals, and limited visible competition.

## How OSS Opportunity Scout works

The scout ranks new opportunities and delivers them only through configured channels, including a separately configured **private** GitHub reports repository. Public host-repository reports are disabled by default. Newly reported seen-state requires successful aggregate delivery and complete scan coverage.

Production code lives under `opportunity_scout/`; `opportunity_scout.py` is the stable executable entry point.

The main boundaries are:

- `opportunity_scout/app.py` — environment wiring and application assembly.
- `opportunity_scout/preferences.py` — immutable non-secret preferences and strict version-1 TOML validation, separate from runtime credentials.
- `opportunity_scout/selection.py` — pure repository exclusions, additive strategic targets, language matching, final effort matching, lane acceptance, and final score thresholds.
- `opportunity_scout/run.py` — combined scan lifecycle, delivery aggregation, and transactional seen-state commit.
- `opportunity_scout/github.py` — canonical GitHub REST transport, safe-read retries, pagination, and per-scan caching.
- `opportunity_scout/paid.py` / `paid_verification.py` — paid-opportunity policy and verification.
- `opportunity_scout/strategic/` — strategic discovery, readiness, competition, and verification policy.
- `opportunity_scout/scoring.py` — effort estimation and cash/career ranking.
- `opportunity_scout/delivery.py` — optional notification/report transports, including privacy-verified private GitHub reports.
- `opportunity_scout/state.py` — typed/versioned seen-state parsing and maintenance.

See `ARCHITECTURE.md` for data flow, module boundaries, GitHub API behavior, and invariants.

## Paid open-source opportunities

Paid candidates require explicit payment evidence and conservative verification. The scanner rejects work that is already assigned, actively claimed, already implemented, historical payout/leaderboard material, unfunded bounty proposals, or otherwise not actionable open work.

Cash ranking considers payment confidence, stated reward, rough expected hourly value, visible competition, and repository legitimacy/activity.

## Career-value GitHub contributions

Strategic discovery combines curated infrastructure/backend repositories with narrow global searches for contributor-ready work. Candidates are refreshed and re-verified before reporting.

Verification checks current issue state, assignment/claim evidence, implementation PRs, maintainer readiness, tracking/umbrella status, security/reporting context, repository availability, and contribution guidance. Deep verification is intentionally bounded so the scout can inspect broadly without unnecessary GitHub API fan-out.

Career ranking considers repository reputation/activity, technical depth, scope, effort, competition, freshness, maintainer activity, and execution fit.

The scanner also records bounded audit diagnostics for strong-looking items that were filtered or narrowly missed selection so false positives and false negatives can be tuned over time.

## Ranked output

Reported candidates include:

- repository, issue number, title, and URL
- paid/unpaid status and reward evidence
- cash, career, and strategic-priority scores
- effort estimate and basis
- visible competition
- repository activity/language/labels
- scoring reasons
- contribution-guide link when available

Complete discovery and verification coverage is required before seen-state advancement. Any recognized failure blocks newly reported URLs and quiet-run maintenance writes, even when strategic failures remain below the warning threshold. Warning thresholds control diagnostics only; successfully delivered candidates may still be reported during an incomplete run, but its state is not committed.

## Deployment ownership

`laclance/OSSOpportunityScout` is the canonical public source of truth for scanner
code. It is a distribution and development repository, not a persistent scout instance:
upstream owns scanner code, development/release CI, documentation, and reusable
distribution assets, while an independent private instance owns the actual scout
workflow, configuration/state, triggers/schedules, concurrency, secrets, delivery
configuration, persistence/history, recovery, and scanner pin.

Forking is optional and intended for scanner-code customization. The default private instance consumes pinned upstream code directly; a customized instance may consume a pinned fork instead. Upstream must not run a persistent scout workflow that reaches into another private repository for state.

Every scan requires valid configuration, defaulting to `scout.toml`. See the
[completed migration record](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) for the history
and rationale behind this ownership model.

## Running

For the generic private deployment assets, see the [private instance guide](docs/PRIVATE_INSTANCE.md).
The caller owns configuration, state/history, triggers, concurrency, credentials,
delivery, and the full scanner SHA pin. Scanner-pin maintenance is manual by default;
operators may optionally copy the narrow Dependabot example to receive reviewed
update PRs while the workflow itself continues executing an immutable full SHA.
Read the recovery procedure before rerunning a transaction whose delivery may have
succeeded but persistence failed.

Upstream workflows use no deployment secrets and own no production scout or runtime state.
Private instances own execution and persistence. Historical state is retained in
private storage; the [empty state example](examples/seen_bounties.example.json) is
only for a deliberately new instance and must never replace recovered history.

For local development:

```bash
python -m pip install -r requirements-dev.txt
make quality
```

For an actual local scan, create instance-owned configuration first and configure
credentials and a delivery channel through the environment:

```bash
cp scout.example.toml scout.toml
python opportunity_scout.py --state /path/to/private-instance/seen_bounties.json
```

Root `scout.toml` and `seen_bounties.json` are ignored by Git. Keep personal
preferences, credentials, and historical state in the private instance.

### Explicit configuration and state paths

```bash
python opportunity_scout.py --config scout.example.toml --state seen_bounties.json
```

Paths are relative to the working directory unless absolute. `--state` defaults to
`seen_bounties.json` and works with or without `--config`; every state read, delivery
commit, and quiet-run maintenance write uses that selected path. Its parent directory
must already exist. Missing state means first run; invalid existing state fails closed.

Every invocation loads a valid version-1 TOML file: `scout.toml` in the working
directory by default, or exactly the supplied `--config PATH`. Missing, unreadable,
or invalid config fails before state loading, discovery, lifecycle maintenance,
or delivery. There is no legacy fallback or search for alternate files.
`--help` remains available without configuration.

See the [configuration reference](docs/CONFIGURATION.md) for every supported field,
default, bound, preference semantic, and generic example. Preferences cover lanes,
additive strategic discovery repositories and cross-lane exclusions, primary repository
languages, final effort estimates, score thresholds, and a 1–8 result limit.
Configured repositories expand strategic discovery only; they do not inherit the
built-in target-repository career-scoring bonus. `name` currently has no runtime
effect. Credentials and delivery remain separate
from TOML preferences. The reference also provides parser-only validation and
offline regression commands; the invocation above performs an actual scan.

### GitHub credentials and delivery

`GITHUB_TOKEN` authenticates scanner GitHub REST access. `GITHUB_REPOSITORY` identifies the workflow host repository. Neither enables public report publishing.

Upstream development CI grants only `contents: read`. The private workflow owns
permissions for discovery and same-instance state persistence; the generic private
template uses `contents: write` for that transaction and no Issues write. Scanner
and private-report credentials remain separate.

Host-repository GitHub reports remain supported as an explicit custom-deployment option through:

```text
GITHUB_REPORTS_ENABLED=true
```

Such a deployment must provide a credential with Issues write permission.

Private GitHub reports use a separate authentication context:

```text
PRIVATE_GITHUB_REPORTS_REPOSITORY=owner/private-reports
PRIVATE_GITHUB_REPORTS_TOKEN=...
```

Before each private report is created, the scanner retrieves repository metadata with the private credential and requires GitHub to report `private: true`. Public, missing, malformed, unauthenticated, or otherwise unverifiable destinations fail closed. The private credential is not used for discovery or verification, and private report issues remain open.

Optional Telegram and Discord transports remain supported through:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL`

## Development quality

Python 3.12 is the minimum supported runtime. CI covers CPython 3.12, 3.13, and 3.14. Python 3.12 remains the syntax, typing, and tooling baseline: Ruff formatting/linting, strict mypy, recursive compilation, and **100% statement + branch coverage** are authoritative on 3.12. Python 3.13 and 3.14 run recursive compilation and the full test suite as compatibility checks. Later Python releases are not supported until they are added to CI and their checks pass.

Useful commands:

```bash
make format
make quality
```

Human contributors should read `CONTRIBUTING.md` before submitting changes. AI coding agents should additionally follow `AGENTS.md`. `ARCHITECTURE.md` is the shared technical reference for both.
