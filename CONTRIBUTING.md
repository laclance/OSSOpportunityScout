# Contributing

This is the human contributor guide for OSS Opportunity Scout. Small, behavior-preserving changes are preferred over broad rewrites. AI coding agents should use `AGENTS.md` for agent-specific execution rules.

## Setup

Requires Python 3.12 or a newer CI-tested CPython release (currently 3.13 and 3.14). Use Python 3.12 for the complete quality gate; it is the syntax, typing, and tooling baseline. CI runs recursive compilation and the full test suite on 3.13 and 3.14. Later releases are not supported until added to CI and their checks pass; Python 3.11 is no longer supported.

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

Run these commands with Python 3.12 active, or select its interpreter explicitly,
for example `make quality PYTHON=/path/to/python3.12`. For compatibility checks on
3.13 or 3.14, run `make compile PYTHON=/path/to/python` followed by
`/path/to/python -m unittest -v`; these checks need only the standard library.

## Where changes belong

- Put focused strategic policy in `opportunity_scout/strategic/`.
- Keep root `opportunity_scout.py` as the thin stable executable entry point. `opportunity_scout.app` owns application/environment assembly and package adapters; `opportunity_scout.run` owns the combined scan lifecycle.
- Keep pure policy separate from network I/O where practical.
- Keep public upstream focused on code, development/release CI, and reusable execution machinery. Actual scout workflows and private configuration/state belong to independent private instance repositories; forking is optional for code customization.

## Tests

Every functional change needs a regression test. Use mocked/fake GitHub responses rather than live network calls.

For configuration documentation and examples, follow the
[offline validation commands](docs/CONFIGURATION.md#offline-validation): use the
strict parser and existing invocation/preference regressions. Check documented
scan invocations through mocked application assembly; do not run live discovery
or delivery merely to validate guidance. Keep examples generic, preferences
separate from credentials/delivery, and current behavior distinct from the planned
mandatory-config cutover.

## Pull requests

Keep each PR focused on one behavior or one refactoring boundary. Finish the logical change before running `make format`, then run `make quality`. Prefer committing the finalized formatted result with the logical change rather than adding repeated formatter-only commits.

Migration slices must follow tracker prerequisites and remain separate. Open one focused PR and stop at that boundary; do not bundle later phases, merge the PR, or retire upstream deployment before the private instance is proven. Include the starting SHA, validation result, and migration slice in the handoff.

Describe:

- what changed
- why the old behavior/structure was a problem
- what tests prove
- whether scanner behavior changed or the change is refactor-only
