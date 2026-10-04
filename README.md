# OSS Opportunity Scout

A lightweight GitHub scanner for finding open-source work worth doing across two lanes:

- **Cash now:** explicit paid bounties and sponsored issues.
- **Career value:** bounded, mergeable issues in respected infrastructure/backend repositories.

The scout ranks new opportunities and delivers them only through configured channels, including a separately configured **private** GitHub reports repository. Public host-repository reports are disabled by default. Newly reported seen-state requires successful aggregate delivery and complete scan coverage.

## Deployment ownership

`laclance/OSSOpportunityScout` is the canonical public source of truth for scanner code. It is a distribution and development repository, not a persistent scout instance: upstream runs project CI and publishes reusable execution machinery, while an independent private instance repository owns the actual scout workflow, configuration, state/history, triggers, schedules, concurrency, secrets, delivery configuration, and scanner version pin.

Forking is optional and intended for scanner-code customization. The default private instance consumes pinned upstream code directly; a customized instance may consume a pinned fork instead. Upstream must not run a persistent scout workflow that reaches into another private repository for state.

The migration is implemented through Slice 7: upstream retains development CI,
scanner code, generic examples, and the [reusable action/private-instance template](docs/PRIVATE_INSTANCE.md).
The persistent upstream workflow and state-branch/worktree transport are retired.
Slice 6 acceptance is verified privately; its operational evidence stays private.
Every scan now requires valid configuration, defaulting to `scout.toml`.
See the [migration tracker](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) for contracts,
acceptance status, and historical state recovery.

## How it works

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

## Candidate selection

### Paid lane

Paid candidates require explicit payment evidence and conservative verification. The scanner rejects work that is already assigned, actively claimed, already implemented, historical payout/leaderboard material, unfunded bounty proposals, or otherwise not actionable open work.

Cash ranking considers payment confidence, stated reward, rough expected hourly value, visible competition, and repository legitimacy/activity.

### Strategic OSS lane

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

## Running

For the generic private deployment assets, see the [private instance guide](docs/PRIVATE_INSTANCE.md).
The caller owns configuration, state/history, triggers, concurrency, credentials,
delivery, and the full scanner SHA pin. Read its recovery procedure before rerunning
a transaction whose delivery may have succeeded but persistence failed.

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
additive strategic repositories and cross-lane exclusions, primary repository
languages, final effort estimates, score thresholds, and a 1–8 result limit.
`name` currently has no runtime effect. Credentials and delivery remain separate
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
