# Contributing

Thanks for improving OSS Opportunity Scout. Small, behavior-preserving changes are preferred over broad rewrites.

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

Read `AGENTS.md`, then `ARCHITECTURE.md` and `CODEBASE_MAP.md`. Search the relevant tests before changing scanner policy; many rules exist because of a real false positive or false negative.

## Commands

```bash
make format      # Ruff formatter
make lint        # Ruff lint
make typecheck   # strict mypy
make test        # unittest + 100% statement/branch coverage
make map         # regenerate CODEBASE_MAP.md
make quality     # local quality gate
```

## Where changes belong

- Put focused strategic policy in `bountyscout/strategic/`.
- Keep root `opportunity_scout.py` as the thin stable executable entry point. `bountyscout.app` owns application/environment assembly and package adapters; `bountyscout.run` owns the combined scan lifecycle.
- Keep pure policy separate from network I/O where practical.

## Tests

Every functional change needs a regression test. Use mocked/fake GitHub responses rather than live network calls.

If production Python symbols move or change, run `make map` and commit the updated `CODEBASE_MAP.md`.

## Pull requests

Keep each PR focused on one behavior or one refactoring boundary. Finish the logical change before running `make format`, then run `make quality`. Prefer committing the finalized formatted result with the logical change rather than adding repeated formatter-only commits.

Describe:

- what changed
- why the old behavior/structure was a problem
- what tests prove
- whether scanner behavior changed or the change is refactor-only
