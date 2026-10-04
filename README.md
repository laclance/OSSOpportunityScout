# OSS Opportunity Scout

A lightweight GitHub scanner for finding open-source work worth doing across two lanes:

- **Cash now:** explicit paid bounties and sponsored issues.
- **Career value:** bounded, mergeable issues in respected infrastructure/backend repositories.

The scout ranks new opportunities and delivers them only through configured channels. The bundled production deployment writes ranked output to a separately configured **private** GitHub reports repository. Public host-repository reports are disabled by default, and seen-state advances only after successful delivery with complete scan coverage.

## How it works

Production code lives under `bountyscout/`; `opportunity_scout.py` is the stable executable entry point.

The main boundaries are:

- `bountyscout/app.py` — environment wiring and application assembly.
- `bountyscout/run.py` — combined scan lifecycle, delivery aggregation, and transactional seen-state commit.
- `bountyscout/github.py` — canonical GitHub REST transport, safe-read retries, pagination, and per-scan caching.
- `bountyscout/paid.py` / `paid_verification.py` — paid-opportunity policy and verification.
- `bountyscout/strategic/` — strategic discovery, readiness, competition, and verification policy.
- `bountyscout/scoring.py` — effort estimation and cash/career ranking.
- `bountyscout/delivery.py` — optional notification/report transports, including privacy-verified private GitHub reports.
- `bountyscout/state.py` — typed/versioned seen-state parsing and maintenance.

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

Incomplete discovery or verification coverage is reported explicitly and prevents seen-state advancement.

## Running

The production GitHub Action is currently **manual-only** while native GitHub scheduling is paused. Trigger it from **Actions → OSS Opportunity Scout**.

For local development:

```bash
python -m pip install -r requirements-dev.txt
make quality
GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repository python opportunity_scout.py
```

### GitHub credentials and delivery

`GITHUB_TOKEN` authenticates scanner GitHub REST access. `GITHUB_REPOSITORY` identifies the workflow host repository. Neither enables public report publishing.

The bundled workflow grants `GITHUB_TOKEN` only:

```yaml
permissions:
  contents: write
```

That permission is required to persist `seen_bounties.json` to the `scout-state` branch. The bundled deployment does not grant Issues write.

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

Python 3.11+ is required. Ruff formatting/linting, strict mypy, recursive compilation, and **100% statement + branch coverage** are enforced by the project quality gate.

Useful commands:

```bash
make format
make quality
```

Human contributors should read `CONTRIBUTING.md` before submitting changes. AI coding agents should additionally follow `AGENTS.md`. `ARCHITECTURE.md` is the shared technical reference for both.

