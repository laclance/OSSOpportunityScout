# OSS Opportunity Scout

A lightweight GitHub scanner for finding open-source work worth doing across two lanes:

- **Cash now:** explicit paid bounties and sponsored issues.
- **Career value:** bounded, mergeable issues in respected infrastructure/backend repositories.

The scout ranks new opportunities and delivers them only through configured channels, including a separately configured **private** GitHub reports repository. Public host-repository reports are disabled by default. Newly reported seen-state requires successful aggregate delivery and complete scan coverage.

## Deployment direction

`laclance/OSSOpportunityScout` is the canonical public source of truth for scanner code. Its target role is a distribution and development repository, not a persistent scout instance: upstream runs project CI and publishes reusable execution machinery, while an independent private instance repository owns the actual scout workflow, configuration, state/history, triggers, schedules, concurrency, secrets, delivery configuration, and scanner version pin.

Forking is optional and intended for scanner-code customization. The default private instance consumes pinned upstream code directly; a customized instance may consume a pinned fork instead. Upstream must not run a persistent scout workflow that reaches into another private repository for state.

The Python support upgrade (Slice 1), coverage-completeness fix (Slice 1B), and immutable preferences/parser/example (Slice 2, PR #34) are merged. Slice 3A (merged in PR #35) adds opt-in `--config PATH` and `--state PATH`, repository targets/exclusions, lane controls, and strategic global-search control. Legacy invocation remains available and does not automatically read `scout.toml`. Slice 3B wires primary repository-language preferences across both lanes. Effort, threshold, and result-limit wiring remain deferred to Slices 3C–3D; deployment work also remains deferred. See the [migration tracker](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) for schema semantics, prerequisites, and historical state recovery.

## How it works

Production code lives under `opportunity_scout/`; `opportunity_scout.py` is the stable executable entry point.

The main boundaries are:

- `opportunity_scout/app.py` — environment wiring and application assembly.
- `opportunity_scout/preferences.py` — immutable non-secret preferences and strict version-1 TOML validation, separate from runtime credentials.
- `opportunity_scout/selection.py` — pure repository exclusions, additive strategic targets, language matching, and final lane acceptance.
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

The legacy upstream GitHub Action remains **manual-only** while native scheduling is paused. As verified on 2026-10-04, its required remote `scout-state` branch is absent, so the restore step cannot succeed. Treat this as a blocked legacy deployment, not a healthy production instance; preserve historical state for private migration rather than resetting it to the empty example.

For local development:

```bash
python -m pip install -r requirements-dev.txt
make quality
GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repository python opportunity_scout.py
```

### Explicit configuration and state paths

```bash
python opportunity_scout.py --config scout.example.toml --state seen_bounties.json
```

Paths are relative to the working directory unless absolute. `--state` defaults to
`seen_bounties.json` and works with or without `--config`; every state read, delivery
commit, and quiet-run maintenance write uses that selected path. Its parent directory
must already exist. Missing state means first run; invalid existing state fails closed.

An explicit config must be a valid version-1 TOML file. Missing, unreadable, or invalid
config fails before state loading, discovery, or delivery; it never falls back to
legacy defaults. Without `--config`, the scout retains its existing source behavior
and ignores any `scout.toml` in the working directory.

Slice 3A applies `[lanes].paid`, `[lanes].strategic`, and
`[discovery].repositories`, `exclude_repositories`, and `global_search`:

- Repository targets add strategic sources to the existing curated list; they are
  not an allowlist. Targets are deduplicated case-insensitively in first-seen order.
- Exclusions match case-insensitively across both lanes and resolved upstream
  repositories, and take precedence over targets.
- Disabled lanes skip their discovery requests and reject corresponding final
  classifications. Both lanes may be disabled; bounded state maintenance still runs
  on a complete quiet scan.
- `global_search = false` skips strategic global searches while retaining curated
  strategic sources and paid discovery.

Slice 3B applies `[discovery].languages` across both lanes using the final upstream
repository's primary language from cached GitHub metadata. Matching is
case-insensitive. `languages = []` accepts every language, including unknown; an
explicit list excludes unknown languages. Issue text, labels, and wrapper-repository
language do not decide acceptance. Filtering precedes strategic repository-slot
settlement and final queue truncation; it adds no Search queries or metadata reads.

`name`, effort, score thresholds, and `max_results` are parsed and
validated but do not yet affect runtime behavior. The existing scoring, thresholds,
verification budgets, and eight-result queue remain in effect until their designated
slices. Delivery credentials and privacy controls remain environment configuration.

### GitHub credentials and delivery

`GITHUB_TOKEN` authenticates scanner GitHub REST access. `GITHUB_REPOSITORY` identifies the workflow host repository. Neither enables public report publishing.

The legacy bundled workflow grants `GITHUB_TOKEN` only:

```yaml
permissions:
  contents: write
```

That permission is required for its existing `scout-state` persistence mechanism. The legacy deployment does not grant Issues write. After private migration is proven, upstream removes that workflow and its deployment-only permissions; the private instance owns state persistence.

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
