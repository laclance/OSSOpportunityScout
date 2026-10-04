# Contributing

This is the human contributor guide for OSS Opportunity Scout. Small, behavior-preserving changes are preferred over broad rewrites. AI coding agents should use `AGENTS.md` for agent-specific execution rules.

## Setup

Currently requires Python 3.11+. The migration's first implementation slice raises the runtime and tooling contract together to Python 3.12+; this documentation change does not drop 3.11 support.

```bash
python -m pip install -r requirements-dev.txt
```

Or:

```bash
make setup
```

## Before changing code

Read `ARCHITECTURE.md` and, when the change affects planned direction, `ROADMAP.md`. Search the relevant tests and source before changing scanner policy; many rules exist because of a real false positive or false negative.

For migration work, also read the [private deployment tracker](docs/PRIVATE_DEPLOYMENT_MIGRATION.md). It owns the agreed contracts, ordered PR slices, recovery procedure, and acceptance gates. Verify current `main`, merged prerequisites, worktree state, and the quality baseline before branching for a slice. Keep current behavior and planned behavior distinct in documentation.

## Commands

```bash
make format      # Ruff formatter
make lint        # Ruff lint
make typecheck   # strict mypy
make test        # unittest + 100% statement/branch coverage
make quality     # local quality gate
```

## Where changes belong

- Put focused strategic policy in `opportunity_scout/strategic/`.
- Keep root `opportunity_scout.py` as the thin stable executable entry point. `opportunity_scout.app` owns application/environment assembly and package adapters; `opportunity_scout.run` owns the combined scan lifecycle.
- Keep pure policy separate from network I/O where practical.
- Keep public upstream focused on code, development/release CI, and reusable execution machinery. Actual scout workflows and private configuration/state belong to independent private instance repositories; forking is optional for code customization.

## Tests

Every functional change needs a regression test. Use mocked/fake GitHub responses rather than live network calls.

## Pull requests

Keep each PR focused on one behavior or one refactoring boundary. Finish the logical change before running `make format`, then run `make quality`. Prefer committing the finalized formatted result with the logical change rather than adding repeated formatter-only commits.

Migration slices must follow tracker prerequisites and remain separate. Open one focused PR and stop at that boundary; do not bundle later phases, merge the PR, or retire upstream deployment before the private instance is proven. Include the starting SHA, validation result, and migration slice in the handoff.

Describe:

- what changed
- why the old behavior/structure was a problem
- what tests prove
- whether scanner behavior changed or the change is refactor-only
