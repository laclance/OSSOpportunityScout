# Contributing

This is the human contributor guide for OSS Opportunity Scout. Small, behavior-preserving changes are preferred over broad rewrites. AI coding agents should use `AGENTS.md` for agent-specific execution rules.

## Setup

Requires Python 3.11+.

```bash
python -m pip install -r requirements-dev.txt
```

Or:

```bash
make setup
```

## Before changing code

Read `ARCHITECTURE.md` and, when the change affects planned direction, `ROADMAP.md`. Search the relevant tests and source before changing scanner policy; many rules exist because of a real false positive or false negative.

## Commands

```bash
make format      # Ruff formatter
make lint        # Ruff lint
make typecheck   # strict mypy
make test        # unittest + 100% statement/branch coverage
make quality     # local quality gate
```

## Where changes belong

- Put focused strategic policy in `bountyscout/strategic/`.
- Keep root `opportunity_scout.py` as the thin stable executable entry point. `bountyscout.app` owns application/environment assembly and package adapters; `bountyscout.run` owns the combined scan lifecycle.
- Keep pure policy separate from network I/O where practical.

## Tests

Every functional change needs a regression test. Use mocked/fake GitHub responses rather than live network calls.

## Pull requests

Keep each PR focused on one behavior or one refactoring boundary. Finish the logical change before running `make format`, then run `make quality`. Prefer committing the finalized formatted result with the logical change rather than adding repeated formatter-only commits.

Describe:

- what changed
- why the old behavior/structure was a problem
- what tests prove
- whether scanner behavior changed or the change is refactor-only
