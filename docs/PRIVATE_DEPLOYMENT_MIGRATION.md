# Private deployment migration — completed record

This is the authoritative completed migration and acceptance record for separating
the public scanner distribution from private scout instances.

Slices 1–7 are complete and merged. Private Slice 6 operational acceptance was
verified privately, with operational evidence kept out of this public repository.
The canonical upstream/private-instance ownership migration is complete.

### Migration PR record

| Work | PR | Status |
| --- | --- | --- |
| Slice 1 — Python 3.12+ | #32 | Complete and merged |
| Slice 1B — coverage completeness | #33 | Complete and merged |
| Slice 2 — immutable preferences / TOML | #34 | Complete and merged |
| Slice 3A | #35 | Complete and merged |
| Slice 3B | #36 | Complete and merged |
| Slice 3C | #37 | Complete and merged |
| Slice 3D | #38 | Complete and merged |
| Slice 4 | #39 | Complete and merged |
| Slice 5 | #40 | Complete and merged |
| Slice 6 acceptance | #41 | Complete and merged |
| Slice 7 — upstream retirement / mandatory config | #42 | Complete and merged |

Post-migration correctness follow-ups are recorded separately because they are not
migration slices:

- #43 — durable recovery barrier for failed persistence
- #44 — early language filtering before adaptive inspection

## Repository and workflow ownership

> `laclance/OSSOpportunityScout` is a distribution and development repository, not a
> persistent scout instance.

`laclance/OSSOpportunityScout` remains the canonical public upstream and source of
truth for scanner code. It owns implementation, verification/scoring policy, tests,
documentation, the configuration parser/schema, generic examples, and reusable
execution machinery. It may run tests, lint, typing, coverage, release checks, and
other project CI, and publish a composite action or deployment template.
It must not operate any persistent scout instance or own user schedules, scout
state, private configuration, scout secrets, report destinations, or persistent
opportunity history.

The private instance repository owns the actual scout workflow and when it runs:

- `scout.toml` and `seen_bounties.json`
- `workflow_dispatch` and any future schedule
- concurrency and secrets
- delivery configuration
- state persistence/history and Git commits
- the scanner version pin

The repository holding the user's private configuration and state also owns when
that scout runs. The preferred default model is:

```text
laclance/OSSOpportunityScout
        │
        │ pinned release / full commit SHA
        ▼
user/private-scout-instance
├── scout.toml
├── seen_bounties.json
├── .github/workflows/scout.yml
├── secrets
└── private delivery configuration
```

Do not make a public workflow reach into another private repository for state.
The private workflow invokes public execution machinery; the upstream does not
trigger or operate private instances.

Forking OSSOpportunityScout is optional and is for code customization, not private
state ownership. Default users consume upstream directly. Users maintaining custom
scanner code may instead use:

```text
laclance/OSSOpportunityScout
        │
        ▼
user/OSSOpportunityScout fork
        │
        │ pinned commit
        ▼
user/private-scout-instance
```

## Verified baseline and prerequisites

Snapshot verified on 2026-10-04; recheck it at the start of every slice:

- Slice 1 starting local and remote `main`:
  `4a18842bd2d1f5cc7f370606271defdf26bdc8d8` (PR #31). PRs #29, #30, and the
  migration-plan PR #31 were confirmed merged before branching.
- Slice 1 raises the runtime minimum and Ruff/mypy targets from 3.11 to 3.12.
  The complete strict gate runs on 3.12; compatibility CI runs recursive compilation
  and the full test suite on 3.13 and 3.14. These are all stable CPython releases
  >=3.12 listed by [Python.org](https://www.python.org/downloads/) on 2026-10-04;
  3.15 is listed as pre-release and is not supported.
- PR #29 fixed paid claim-comment verification failing open; PR #30 made state-save
  failure fail the scanner process. Both are merged in this baseline.
- At the original migration baseline, the legacy upstream workflow was manual-only
  and required an absent remote `scout-state` branch, blocking restore. Slice 7
  removed that workflow; this remains historical context, not a runtime contract.
- Historical candidate `f96022aa161974701e7ff906b48946cbfbb2f663` is available locally
  and parses as version 2 with eight entries. This does not establish that it is
  the latest trustworthy production snapshot.
- Local documentation closeout passed `make quality` on Python 3.12.3: Ruff formatting
  and lint, recursive compilation, strict mypy, all 386 tests, and 100% statement
  and branch coverage. This records local validation, not a GitHub Actions result;
  each implementation PR must run the current gate afresh.
- Slice 1 independently passed `make quality` on Python 3.12.3 both before branching
  from the verified baseline and after the runtime/tooling/workflow edits: all 386
  tests, 100% statement and branch coverage, and every strict quality check passed.
  The focused PR additionally requires passing 3.13 and 3.14 compatibility CI.

- Slice 1B starting local and freshly fetched remote `main`:
  `2b12fc2509d3589ab0c8ff742c0d388967a7c19b`. PR #32 (Slice 1) was confirmed merged
  on 2026-10-04 before branching. The worktree was clean, and the baseline passed
  `make quality` on Python 3.12.3: all 386 tests, every strict check, and 100%
  statement and branch coverage.

**Coverage prerequisite implemented by Slice 1B:** `CoverageStatus.complete` in
`opportunity_scout/run.py` requires zero recognized discovery/verification failures.
Both newly reported URL advancement and quiet-run maintenance persistence use this
result rather than warning absence. Existing warning thresholds remain diagnostic
policy: one to four strategic verification failures can leave the warning unset but
never permit state writes. Incomplete quiet runs below the warning threshold skip
delivery and maintenance and print that state was not updated. Candidate delivery
and immediate warnings for discovery/paid verification failures remain unchanged.
Targeted regressions cover every recognized verification reason, discovery failures,
one to four strategic failures, warning-boundary and raised-threshold cases, and
application-level quiet/candidate runs. Slice 1B merged in PR #33 before Slice 2.

- Slice 2 starting local and freshly fetched remote `main`:
  `e4295138629fe8f0d27129eed63ac001df80b0d0`. PR #33 was confirmed merged on
  2026-10-04, local `main` matched remote `main`, and the worktree was clean.
  Before branching, `make quality` passed on Python 3.12.3: all 389 tests,
  every strict check, and 100% statement and branch coverage.

- Slice 3A starting local and freshly fetched remote `main`:
  `688a789dbd880c6f65f2d999143aa71f897af691`. PR #34 was confirmed merged on
  2026-10-04, local `main` matched remote `main`, and the starting worktree was clean.
  Before branching, `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed
  on Python 3.12.3: all 409 tests, every strict check, and 100% statement and branch
  coverage. The explicit interpreter selects the available development environment.

- Slice 3B starting local and freshly verified remote `main`:
  `67be1bb3bdea24d82311af69b3c3ca64eed6c893`. PR #35 was confirmed merged on
  2026-10-04, local `main` matched remote `main`, and the starting worktree was clean.
  Before branching, `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed
  on Python 3.12.3: all 426 tests, every strict check, and 100% statement and branch
  coverage. The explicit interpreter selects the available development environment.

- Slice 3C starting local and freshly fetched remote `main`:
  `9acc450935fe6a76ff173a3b9377c4e63b6c195b`. PR #36 was confirmed merged on
  2026-10-04, local `main` matched remote `main`, and the starting worktree was clean.
  Before branching, `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed
  on Python 3.12.3: all 432 tests, every strict check, and 100% statement and branch
  coverage. The explicit interpreter selects the available development environment.

- Slice 3D starting local and freshly verified remote `main`:
  `043f615f789106cf10886dc33b1d90474c041adf`. PR #37 was confirmed merged on
  2026-10-04, local `main` matched remote `main`, and the starting worktree was clean.
  The Python Quality workflow for that SHA passed, including 3.13/3.14 compatibility.
  Before branching, `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed
  on Python 3.12.3: all 439 tests, every strict check, and 100% statement and branch
  coverage. The explicit interpreter selects the available development environment.

- Slice 4 starting local and freshly verified remote `main`:
  `2dd6095b49e9dff5bbc67e6085a35e93b9adeab8`. PR #38 was confirmed merged on
  2026-10-04, local `main` matched remote `main`, and the starting worktree was clean.
  The Python Quality workflow for that SHA passed. Before branching,
  `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed on Python 3.12.3:
  all 447 tests, every strict check, and 100% statement and branch coverage.
  The explicit interpreter selects the available development environment.

- Slice 5 starting local and freshly fetched remote `main`:
  `11303a95cc33d3338fd5c6570f5802f38c924f83`. PR #39 was confirmed merged on
  2026-10-04 with successful Python Quality and 3.13/3.14 compatibility checks.
  Local `main` matched remote `main`, the worktree was clean, and the baseline passed
  `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` on Python 3.12.3:
  all 447 tests, every strict gate, and 100% statement and branch coverage.

- Slice 6 starting local and freshly fetched remote `main`:
  `301241a6e8c14325fab65ada5c4dcc3a8b7933fb`. PR #40 was confirmed merged on
  2026-10-04 with successful Python Quality and 3.13/3.14 compatibility checks.
  The starting worktree was clean and local `main` matched remote `main`.
  Before branching, `make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed
  on Python 3.12.3: all 460 tests, every strict gate, and 100% statement and branch
  coverage. Private recovery provenance and operational evidence stay outside
  this public tracker.

The missing-state finding is superseded as a long-term remediation: do not recreate
the upstream `scout-state` architecture as the final solution. Preserve historical
state for private migration instead. Upstream scheduling restoration is also
superseded; initial private deployment is manual-only, and any future schedule
belongs to the private instance. Compatibility-wrapper cleanup remains separate.

## Preference schema and runtime semantics

Slice 2 adds immutable `ScoutPreferences` in `opportunity_scout/preferences.py`,
separate from credential-bearing `RunConfig`. It uses standard-library `tomllib`,
deterministic defaults, and the existing canonical `EffortBucket` vocabulary.
Configuration contains preferences, never credentials or correctness controls.

The generic v1 [scout.example.toml](../scout.example.toml) fixes the field names and
defaults below. Slice 3A loads an explicit `--config PATH` and applies repositories,
exclusions, lanes, and strategic global-search control. `--state PATH` selects every
state read/write, independently of config. Slice 7 superseded the migration-era
no-config behavior: every scan now requires `scout.toml` from the working directory or
exactly the explicit `--config PATH`. Slice 3B applies languages across both lanes;
Slice 3C applies final effort estimates. Slice 3D applies thresholds and result
limits. Slice 4 publishes the [configuration reference](CONFIGURATION.md), including
every supported field/default/bound, current path behavior, generic examples, and
offline validation. Slice 7 completed the mandatory-config cutover.

```toml
version = 1
# Optional non-secret metadata; currently has no runtime effect:
# name = "Weekend scout"

[lanes]
paid = true
strategic = true

[discovery]
languages = []
repositories = []
exclude_repositories = []
global_search = true

[preferences]
effort = ["<1h", "1–3h", "3–6h", "6–12h", "1d+"]
min_career_score = 55
min_cash_score = 55
max_results = 8
```

- Require `version = 1`; reject malformed/unreadable files, unsupported versions,
  unknown keys, malformed types, invalid effort buckets, and invalid values before
  network or delivery activity. Scores are integers from 0–100; `max_results` is an
  integer from 1–8. Boolean values do not satisfy integer fields.
- Parser details: only `version` is required; omitted tables and fields use the
  defaults above. Explicit values must have their exact schema types. Name and
  list entries must be non-empty strings after surrounding whitespace is trimmed.
  Repository entries must be `owner/repository` identifiers, not URLs. Effort
  entries must use the canonical labels above (including en dashes). Tuples retain
  input order, spelling/case, and duplicates; runtime matching follows the policies
  below.
  Empty lists and both lanes disabled are valid: an empty effort list accepts no
  final estimates, while an empty language list accepts all languages. `name`
  currently has no runtime effect.
  `parse_scout_preferences(document)` is pure; `load_scout_preferences(Path(...))`
  reads an explicit path. Both raise `ScoutPreferencesError` on invalid input;
  missing/unreadable/invalid files never fall back to defaults. The parser does not
  import application assembly or `RunConfig`, make network requests, or write state.
- Repository targets add curated strategic sources; they are not an allowlist.
  Exclusions take precedence and apply across both lanes, including the final
  upstream repository after source refresh or aggregator resolution.
- Languages match primary repository-language metadata case-insensitively. An
  empty list accepts every language; an explicit list excludes unknown languages.
  Reuse cached metadata and do not add searches per configured language.
- Effort accepts the existing final estimates without changing estimation policy.
  Final paid candidates use the cash threshold; final unpaid candidates use the
  career threshold, regardless of discovery source.
- Disabled lanes skip their source requests and cannot contribute corresponding
  final results. `global_search` controls strategic searches only; it does not
  disable paid-source discovery. Disabled sources are not coverage failures.
- Apply preferences before repository-slot settlement and final queue truncation
  so ineligible candidates cannot hide eligible ones. Early pruning must remain
  sound when refresh changes effort or paid classification. Preserve deterministic
  ranking and existing discovery/verification budgets.
- Application assembly accepts `--config PATH` and `--state PATH`, retaining the
  thin executable shim. The default invocation requires `scout.toml`; explicit
  `--config PATH` selects exactly that file. Missing/unreadable/invalid config fails
  before runtime assembly, state loading, network, maintenance, or delivery, with
  no legacy fallback. All state reads and writes use the selected path, relative
  to the working directory unless absolute; the parent must exist.

Keep parsing, selection, verification, scoring, reporting, and persistence cohesive.
Use pure selection functions, typed inputs, narrow injected dependencies, and
immutable preference values. Do not assign preferences to mutable globals, make
leaf modules import application assembly, add unnecessary packaging, or expose
retry, pagination, payment verification, privacy, coverage, state, or worker-count
policy as user preferences. Public examples must contain no personal repository
lists or state. Multi-profile support remains deferred.

## Implementation sequence and acceptance

Slices 1–7 were completed in order through PR #42. Slice 6 operational acceptance
was verified privately before Slice 7 retired upstream production-instance
responsibilities and completed the mandatory-config cutover. Slices 3A–3D remained
separate focused changes, as planned.

Slice 1 establishes the Python support contract:

- Minimum supported Python: 3.12.
- Python 3.12 is the syntax, typing, and tooling baseline and remains the interpreter
  for the complete authoritative quality gate.
- At Slice 1 merge, CI must include every stable CPython release >=3.12 available
  at that time. Interpreters newer than 3.12 run compatibility checks including
  recursive compilation and the test suite; Ruff, strict mypy, and coverage remain
  authoritative on 3.12 unless deliberately expanded later.
- A newly released Python version is not supported until CI explicitly includes it
  and its compatibility checks pass. `>=3.12` therefore never means arbitrary
  untested future interpreters.

| Slice | Deliverable | Acceptance gate | Status |
| --- | --- | --- | --- |
| 1 | Raise the minimum runtime to Python 3.12 and add stable-version compatibility CI across workflows and documentation | Full strict quality gate on 3.12; recursive compile + tests on every stable CPython release >=3.12 available when the slice lands; no configuration changes; no newer-than-3.12 syntax; later Python releases are not supported until their CI checks pass | Complete |
| 1B | Fix coverage completeness independently of warning thresholds | Any recognized verification/discovery failure prevents state advancement, including quiet-run maintenance | Complete |
| 2 | Add immutable preferences, strict TOML parser, and generic example | Parser regressions; existing invocation behavior preserved | Complete |
| 3A | Wire explicit config/state paths, repositories, exclusions, lanes, and global discovery | Disabled sources make no requests; legacy and configured invocation both work | Complete |
| 3B | Wire language preferences | Cached metadata reused; no increased Search fan-out | Complete |
| 3C | Wire effort preferences | Final estimates control acceptance; rejected candidates do not consume selection slots | Complete |
| 3D | Wire thresholds and result limits | Final classification governs thresholds; deterministic ranking preserved | Complete |
| 4 | Publish configuration guidance and validated examples | Every documented command and field works | Complete |
| 5 | Add a public pinned composite action and a generic private-instance template | Private instance owns triggers, schedules, concurrency, secrets, config, state, persistence, delivery configuration, and scanner pin; post-delivery persistence failure preserves the resulting state as a private short-retention recovery artifact before the workflow fails; no persistent upstream deployment created | Complete |
| 6 | Recover and migrate the personal instance privately | Trustworthy state seeded; private delivery, persistence, and subsequent deduplication verified | Complete |
| 7 | Remove canonical public upstream production-instance responsibilities and complete config cutover | Slice 6 proven; upstream retains only development/release CI and reusable execution machinery | Complete |

Slice 1 inspected all runtime declarations, updated existing sources of truth
together, deliberately dropped 3.11 compatibility, kept syntax/types/tooling pinned
to the 3.12 baseline, and added compatibility CI for every stable CPython release
>=3.12 available when the slice landed. It did not add packaging metadata solely to
declare the minimum version or begin configuration/deployment implementation.

For Slice 5, the chosen mechanism is a public composite action executing scanner
source from its pinned action directory. The private workflow owns surrounding
checkout/state persistence steps and passes runtime credentials explicitly. Its
normal GitHub Actions `GITHUB_TOKEN` may provide scanner GitHub REST access and
same-repository state persistence when least-privilege caller permissions allow.
Keep the private-report credential as a separate trust boundary and never reuse it
for scanner discovery. Introduce a separate persistence credential only if a later
repository boundary or deployment design actually requires one. Use full commit
SHA pins for execution; a release may identify the chosen SHA, but a moving
branch/tag must not silently upgrade production. See GitHub's
[workflow/action comparison](https://docs.github.com/en/actions/concepts/workflows-and-actions/reusing-workflow-configurations)
and [action-path context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context).

Serialize the full private scan/state transaction with one group for its shared
state, `cancel-in-progress: false`, and `queue: max`. Read current state after
serialization begins; reject stale writes without force-pushing. The pending queue
is bounded, and concurrency does not guarantee exactly-once delivery. See
[GitHub concurrency semantics](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Slice 7 removed the upstream persistent scout workflow, `scout-state` ownership,
personal state persistence, deployment-only permissions, and documentation implying
upstream is a production scout. The CLI no longer uses source defaults without
configuration; personal preferences belong in private `scout.toml`. Generic schema
defaults, curated sources, verification and ranking remain scanner policy. Obsolete
branch/worktree transport was removed without deleting historical Git objects or
private state. The empty root state seed moved to `examples/seen_bounties.example.json`;
root instance config/state are ignored by Git. Development CI retains read
permissions; the public action and inactive private template remain reusable assets.

## Post-migration correctness follow-ups

PR #43 added the durable `.scout/recovery-required` barrier for failed remote
persistence. The exact resulting state is preserved in the private short-retention
recovery artifact; marker establishment starts from current remote history. Later
runs fetch current private state and fail before scanner execution while the marker
exists. Queue cancellation runs separately with only Actions write permission and
remains defense in depth. Only deliberate operator recovery removes the marker.
Delivery and persistence remain separate operations with no exactly-once claim.

PR #44 moved language eligibility for authoritative direct-source repositories to
immediately after the already-required cached repository-metadata lookup and before
bounded base/adaptive inspection. Recognized wrapper/aggregator sources defer
language judgment until upstream resolution. Final verification still rechecks the
resolved repository's authoritative language. Search and repository-metadata
fan-out are unchanged, and configured-repository scoring semantics were not changed.

## Historical state migration and recovery

Slice 6 was operational work performed privately. Its acceptance record required:

1. Later state sources were checked before selecting the latest trustworthy snapshot;
   `f96022aa161974701e7ff906b48946cbfbb2f663` remained a candidate, not proof of freshness.
2. The selected snapshot was parsed with the canonical version-2 parser and its
   provenance/entry count recorded without publishing opportunity contents.
3. An independent private instance was provisioned with private configuration,
   workflow, scanner pin, secrets, and delivery configuration.
4. `seen_bounties.json` was seeded from recovered history and verified not to be the
   empty public example or a first-run reset.
5. Manual execution verified discovery, privacy-verified delivery, remote state
   persistence, and subsequent deduplication.
6. Serialization and stale-write handling were verified before upstream production
   responsibilities were retired; the initial private deployment remained manual-only.

Keep the acceptance record privately using the
[Slice 6 evidence checklist](PRIVATE_INSTANCE.md#slice-6-private-acceptance-record).
Historical recovery and offline workflow checks do not establish live deployment
success. Resolve instance/report repository names, personal preferences, intended
delivery channels/destinations, and secure credential provisioning before dependent
setup or dispatch. Public status may record which gates remain pending, but must
not publish personal configuration, state contents/counts, opportunity URLs,
private repository identities, run IDs, recovery hashes, or operational logs.

Preserve fail-closed state parsing, bounded lifecycle maintenance, and transactional
maintenance/new-URL persistence. Only absent state means first-run empty state;
only confirmed `closed` lifecycle evidence permits pruning. Newly reported URLs
advance only with complete coverage and successful aggregate delivery; one failed
channel may be satisfied by another successful channel. Host issue creation plus
auto-close must both succeed. Complete quiet runs may persist maintenance alone.
Actual local-save or remote-persistence failure must fail the run.

Keep the private-report credential distinct from the scanner/private-instance
GitHub credential. Use least-privilege caller permissions for discovery and state
persistence. The private instance's normal GitHub Actions `GITHUB_TOKEN` may handle
scanner GitHub REST access and persistence to that same private repository when its
permissions allow; introduce a separate persistence credential only if the actual
repository boundary or deployment model requires one. Private reporting still uses
its own credential, that credential is never reused for scanner discovery, and its
metadata check must report `private: true`. Config files and public docs contain no
secrets or private opportunity contents.

If delivery succeeds but remote state persistence fails, the private deployment
preserves the exact resulting `seen_bounties.json` in a private short-retention
recovery artifact and establishes `.scout/recovery-required` from current remote
history before the shared transaction lock is released. A separate follow-up job
with only `actions: write` cancels queued runs as defense in depth. Every later
normal run fetches the current private default branch and fails before scanner
execution while the marker exists. Operator recovery restores or reconciles state,
removes the marker deliberately, and verifies remote state before scanning resumes.
Never automatically replay delivery, reset to older state, force-push a stale
snapshot, or claim exactly-once delivery.

## Historical quality and slice protocol

Slices 1–7 each began from verified current `main`, with prerequisite PRs and the
current Python Quality baseline checked before branching. Each slice kept one
logical boundary, ran the repository quality gate, and preserved Ruff, recursive
compilation, strict mypy, all tests, and 100% statement/branch coverage. Operational
verification stayed private.

The migration used one focused PR per slice and stopped at the open-PR boundary for
review. Those slices are now all merged; future work must not reuse or renumber the
completed migration slices. General focused-PR and verify-current-`main` practices
remain in `CONTRIBUTING.md` and `AGENTS.md`.

Historical recovery, independent private provisioning and deliberate seeding,
reviewed scanner pin, manual-only execution, separate report credentials, intended
private delivery, exact resulting-state remote persistence, and subsequent
deduplication were verified privately. Controlled checks covered serialization,
current-state reads after queued admission, stale-write rejection, recovery-artifact
preservation, and recovery. Operational evidence and operator choices remain
private; this record makes no exactly-once delivery claim.

Slices 1–7 are complete and merged through PR #42; Slice 6 operational acceptance remains verified privately.
Every scan loads default `scout.toml` or exactly explicit `--config PATH` before
state/network/delivery activity. Missing or invalid config never selects legacy
defaults. All state reads and delivery/quiet-maintenance writes use `--state PATH`,
defaulting to `seen_bounties.json`; the parent must exist.
Repository targets add strategic curated sources in first-seen, case-insensitive
order; exclusions take precedence across both lanes, including resolved upstream
repositories. Disabled lanes skip their discovery sources and reject corresponding
final classifications before repository-slot settlement and queue truncation.
`global_search` controls strategic global discovery only. Disabled sources do not
make coverage incomplete; bounded quiet maintenance remains available.

## Earlier slice implementation records

These records describe behavior at each earlier slice. Slice 7 superseded their
migration-era opt-in configuration and legacy invocation contracts.

Slice 3A originally covered enabled-source request counts with/without prefetch, target
addition/deduplication, exclusions in source pools and after refresh/aggregator
resolution, final classifications and selection slots, configured/legacy executable
invocation, explicit invalid configuration, and custom state deduplication,
delivery/maintenance persistence, corruption, save failure, incomplete coverage,
and failed delivery. Existing verification, privacy, coverage, and aggregate-delivery
contracts remain in force.

Slice 3B matches languages case-insensitively using the primary language from the
resolved upstream repository's cached metadata. Empty lists accept all languages,
including unknown; explicit lists exclude absent/null/empty language and the
canonical `Unknown` value. Issue text, labels, and source/wrapper language do not
control acceptance. Verification filters after refresh/aggregator resolution and
metadata availability/archive checks, before contribution-guide lookup. Both lane
adapters and final queue assembly enforce acceptance before strategic repository
slots settle and the queue is truncated. No per-language Search queries, additional
metadata reads, new caches, or discovery/verification budget changes are introduced.

Targeted policy and orchestration regressions cover both lanes, unknown metadata,
case-insensitive matching, resolved repositories, selection limits, coverage and
delivery/state boundaries, and cross-lane metadata reuse. With and without
authenticated prefetch, request counts stay fixed as the language list grows.
Local Slice 3B validation passed `make format` once, then
`make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` on Python 3.12.3:
all 432 tests, Ruff formatting/lint, recursive compilation, strict mypy, and 100%
statement and branch coverage. This records local validation, not a CI result.

Slice 3C applies exact effort-bucket membership to existing final estimates through
the pure selection guard. An omitted list accepts all five canonical buckets; an
empty list accepts no candidates. Both discovery lanes filter after candidate
construction, and strategic rejection precedes accepted-slot settlement. The
combined queue filters before URL deduplication and truncation, so rejected higher
ranked candidates cannot hide eligible ones. No effort preference is applied to
source-only preview estimates or preview paid classification; refreshed source,
resolved upstream evidence, and existing strategic discussion may change effort.
Estimation policy, ranking rules/bounds, request budgets, verification safeguards,
privacy, coverage, delivery, and state contracts remain unchanged.

Targeted regressions cover every effort bucket across both final classifications,
empty/default/duplicate preferences, paid refresh changing estimates in both
directions, strategic discussion changing final effort, refreshed paid/unpaid
classification and effort, repository-slot settlement, pre-deduplication queue
filtering, complete/incomplete coverage and delivery transactions, and configured
and legacy invocation with/without authenticated prefetch. Request counts remain
fixed for the exercised preferences, including an empty list.

Local Slice 3C validation ran `make format` once, then passed
`make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` on Python 3.12.3:
all 439 tests, Ruff formatting/lint, recursive compilation, strict mypy, and 100%
statement and branch coverage. This records local validation, not a CI result.

Slice 3D applies inclusive `min_cash_score` and `min_career_score` thresholds using
final paid/unpaid classification, regardless of discovery source. The shared pure
score guard consumes refreshed final scores; it never accepts payment based on
preview evidence. Strategic threshold rejection happens after deep verification,
before accepted candidates settle repository slots. A low preview career upper
bound cannot safely reject a candidate whose refreshed score/classification may
change. Existing ranking upper bounds still determine repository settlement;
inspection/adaptive budgets, worker caps, failure breakers, scoring, and ranking
remain unchanged. Low-scoring previews may require checks within the same pool.

The combined queue filters eligibility before URL deduplication and caps its final
ranked results at `max_results` (1–8), respecting any narrower injected report limit.
Lower output limits do not shrink discovery or verification budgets. Rejected
higher-priority duplicates and threshold failures cannot hide eligible candidates.
Verification, privacy, coverage, aggregate delivery, and state invariants remain
in force. Legacy invocation ignores `scout.toml` and retains both score defaults
at 55 and the eight-result queue; `name` still has no current runtime effect.

Targeted regressions cover inclusive boundaries at 0, 55, and 100; both final
classifications through both discovery adapters; low previews with refreshed scores
and payment changes in both directions; resolved upstream payment scores; rejected
repository slots and duplicate URLs; every result limit from 1–8; complete/incomplete
coverage and delivery transactions; configured/legacy invocation; and unchanged
request counts with/without authenticated prefetch for the exercised preferences.
Existing effort and language tests now use score-eligible paid fixtures, and the
previous preview-threshold regression verifies refreshed evidence instead.

Local Slice 3D validation ran `make format` once, then passed
`make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` on Python 3.12.3:
all 447 tests, Ruff formatting/lint, recursive compilation, strict mypy, and 100%
statement and branch coverage. This records local validation, not a CI result.

Slice 4 publishes [configuration guidance](CONFIGURATION.md), refreshes the
complete default example, and aligns README, architecture, roadmap, and contributor
validation guidance. The reference documents all version-1 fields, strict types,
defaults/bounds, normalization and matching, path resolution, opt-in configuration,
legacy `scout.toml` ignorance, and the lack of runtime effect for `name`. Generic
examples contain no personal targets, state, or credentials. Preferences remain
separate from authentication and delivery; private instance ownership and optional
fork customization remain in force.

Local Slice 4 offline validation checked all 13 documented fields against the
preference model, the complete default file and all three TOML blocks in the
reference/tracker, five documented scan invocations through mocked executable
assembly, relative/custom state paths, legacy invocation beside an invalid
`scout.toml`, the parser-only command, CLI help, and local documentation links.
Network, state I/O, and delivery were forbidden during the example/command checks.
The documented five-module regression command passed all 56 tests with fake
network/delivery results. No live scanning or delivery was performed. No Python
changed, so `make format` was unnecessary. Local Slice 4 validation passed
`make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` on Python 3.12.3:
all 447 tests, Ruff formatting/lint, recursive compilation, strict mypy, and 100%
statement and branch coverage. This records local validation, not a Slice 4 CI result.

Slice 5 adds root [action.yml](../action.yml), executing scanner source from its
pinned action directory under isolated Python 3.12, with explicit config/state
paths and separate discovery/private-report credentials. The generic
[private template](../examples/private-instance/scout.yml) remains outside active
upstream workflows. The private caller owns triggers, future schedules, concurrency,
secrets, config/state, Git persistence/history, delivery configuration, and the full
scanner SHA pin. Forking remains optional. No runtime scanner policy or no-config
CLI behavior changes; no personal state or credentials are published.

The template is manual-only, rejects public/non-default-branch execution, and
serializes the entire checkout/read/scan/delivery/persist/recovery transaction with
one shared `scout-seen-state` group, `cancel-in-progress: false`, and `queue: max`.
State is deliberately seeded in the instance default branch; current state is read
after serialization begins. Persistence rejects a changed base SHA and races fail
the normal non-forced push. Only the state file is committed. Job `contents: write`
is sufficient for scanner REST access and same-instance persistence; private report
credentials stay separate, without Issues write on the caller token.

Any remote persistence failure attempts to preserve the exact resulting state in
a private, three-day recovery artifact before an explicit workflow failure.
[Private instance guidance](PRIVATE_INSTANCE.md) documents recovery before rerunning,
stopping queued transactions during recovery, missing-artifact reconstruction,
incomplete-coverage limitations, and the lack of exactly-once delivery guarantees.
There is no automatic delivery replay, stale force push, or upstream deployment.

Offline action/template checks parse YAML, validate Bash syntax and workflow
contracts, execute pinned-source launch with isolated imports and quoted paths,
and test real local Git state reads/persistence, unchanged state, missing/symlinked
files, stale heads, fetch/commit/push failures, a race after fetch, and recovery
success/failure diagnostics. PyYAML and its typing stubs are development-only;
compatibility CI installs the YAML parser to run these same tests. Scanner runtime
remains standard-library-only. The action is committed first so the template can
select a real immutable source SHA without a circular self-reference:
`fde5e99d10385a14f51140ebde18ed117c084c7a`. Both commits belong to this one focused
Slice 5 PR. Action dependency pins were verified against public GitHub commits;
current action-path, pinning, concurrency, and artifact guidance was consulted
through Context7, with the live GitHub concurrency page confirming `queue: max`.

Local Slice 5 validation ran `make format` once, then passed
`make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` on Python 3.12.3:
all 460 tests, Ruff formatting/lint, recursive compilation, strict mypy, and 100%
statement and branch coverage. The final real scanner pin was verified locally
and the quality gate rerun after that YAML change. This records local validation;
live private-instance, delivery, and recovery-artifact validation was subsequently
verified in Slice 6.

Slice 5 merged in PR #40. Historical migration (Slice 6) and the private acceptance
gates above were verified; supporting evidence stays private. PR #41 recorded that
acceptance. Slice 7 then completed upstream retirement and the mandatory-config
cutover in merged PR #42; private evidence remains private.


## Slice 7 implementation

Starting clean local/fetched `main`: `540d3fd07c34431554808be3e725b0c90549e5fa`.
PR #41 was confirmed merged on 2026-10-04; private Slice 6 acceptance remains
satisfied as confirmed by the operator. Baseline `make quality` on Python 3.12.3
passed all 460 tests, every strict gate, and 100% statement/branch coverage.

Slice 7 deletes the active upstream scout workflow and its production state-branch/
worktree persistence, deployment permissions, and delivery-secret wiring. Only
Python Quality CI remains active with `contents: read`. The reusable composite
action and inactive private template remain. The empty root state file is relocated
to a generic example; root instance config/state are ignored. No historical Git
objects, branches, worktrees, private state, or private operational evidence are
modified or deleted.

The CLI requires `scout.toml` in the working directory by default, or exactly
`--config PATH`. Both load before runtime assembly/state/network/delivery, with no
legacy fallback. `--state PATH` and all verification, privacy, coverage, delivery,
lifecycle maintenance, ranking, and local state contracts remain intact. Generic
schema defaults and curated scanner policy remain unchanged; personal choices
belong to private configuration. Documentation and examples describe current
ownership and invocation consistently.

Targeted regressions cover default executable and state-only invocation, explicit
overrides beside missing/invalid/different defaults, missing/malformed/unsupported/
unreadable/non-UTF-8 config before I/O, help without config, unchanged request
budgets and selection, and the absence of upstream deployment/state responsibilities.
After one `make format` across the completed Python worktree,
`make quality PYTHON=/tmp/oss-scout-docs-venv/bin/python` passed on Python 3.12.3:
all 463 tests, Ruff formatting/lint, recursive compilation, strict mypy, and 100%
statement and branch coverage (2,715 statements and 998 branches, no misses or
partial branches). Offline documentation checks validated local links, TOML examples,
and documented scan commands through mocked assembly without scanning or delivery.
This records the local Slice 7 validation. PR #42 subsequently merged the cutover.
Post-migration corrections #43 and #44 are recorded separately above and are not
additional migration slices; no private pin upgrade is implied.
