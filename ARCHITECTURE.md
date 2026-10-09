# Architecture

## Goals

OSS Opportunity Scout has two lanes with different risk profiles:

- **Paid bounty lane:** conservative payment and competition verification protected by regression tests.
- **Strategic OSS lane:** broader discovery plus project-specific readiness, competition, scoring, and ranking.

The architecture should make those lanes easy to reason about without forcing contributors or AI tools to load the full scanner into context.

## Deployment ownership

> `laclance/OSSOpportunityScout` is a distribution and development repository, not a persistent scout instance.

The canonical public upstream remains the source of truth for scanner code and may run development/release CI and publish reusable execution machinery or generic templates. It must not operate persistent scout instances or own user schedules, private configuration, scout secrets, report destinations, scout state, or opportunity history.

The independent private instance repository owns `scout.toml`, `seen_bounties.json`, the actual scout workflow, `workflow_dispatch`, any future schedule, concurrency, secrets, delivery configuration, state persistence/history, and the scanner pin. The repository holding private configuration/state also owns when the scout runs; its workflow invokes pinned public execution machinery. Upstream does not run a workflow that reaches into private state repositories.

Forking is optional for code customization. Default instances consume pinned upstream code directly; customized instances may consume a pinned fork while keeping runtime ownership private.

The public/private ownership migration is complete. The
[completed migration record](docs/PRIVATE_DEPLOYMENT_MIGRATION.md) preserves its
implementation, recovery, and acceptance history. Only development/release CI and
reusable distribution assets remain upstream.

Application assembly loads `scout.toml` from the working directory by default,
or exactly the explicit `--config PATH`, before constructing runtime configuration,
loading state, or doing network/delivery work. Missing or invalid config fails
closed without legacy defaults. Root instance config/state are ignored by Git;
the empty seed is distributed as `examples/seen_bounties.example.json`.

### Public action and private transaction template

Root `action.yml` runs the scanner from its pinned `github.action_path` using
isolated Python 3.12. It explicitly supplies config/state paths and separate
discovery/delivery credentials, disables host reporting, and leaves Git transport
to the caller. Scanner policy and local state semantics remain unchanged.

`examples/private-instance/scout.yml` is an inactive distribution template, outside
upstream workflows. The private caller restricts execution to its private default
branch and owns the full transaction under one shared concurrency group with
`cancel-in-progress: false` and `queue: max`. Checkout/current-state reading occurs
inside that lock; the recorded branch head is the persistence base. Before scanner
execution, the workflow fails closed if private coordination marker
`.scout/recovery-required` exists.

Persistence rejects intervening branch updates and uses a normal non-forced push.
The workflow classifies recovery causally. If the scanner saved the resulting state
locally and remote Git persistence fails, the exact local bytes are preserved in a
three-day private recovery artifact. If delivery succeeded but the scanner's local
state save failed, the pinned action emits an explicit recovery signal and the
workflow does not claim that the remaining local state file is an exact
post-delivery snapshot. Both cases establish `.scout/recovery-required` from a
freshly fetched current remote head in a separate temporary worktree; the local-save
case requires operator reconstruction from trustworthy private evidence.

The marker commit touches no seen-state and uses an ordinary push, so newer branch
history is never overwritten. The normal scan job has only `contents: write`; the
pinned scanner never receives Actions write capability. Recovery-required outcomes
publish a small handoff to a dependent follow-up job whose token has only
`actions: write`. That job cancels other runs waiting in the same
`scout-seen-state` group as defense in depth and emits the terminal failure.
Because concurrency is workflow-level, the shared lock remains held until the
follow-up job completes. The durable marker is the correctness barrier; queued-run
cancellation is secondary protection. Ordinary pre-delivery scanner failures do not
enter recovery mode without the explicit post-delivery signal. Recovery is
operator-driven and removes the marker only in a deliberate recovery commit.
Delivery and persistence remain separate operations without exactly-once guarantees.
See the [private instance guide](docs/PRIVATE_INSTANCE.md) for adoption and recovery.

## Data flow

```text
GitHub + bounty-platform sources
            |
            v
       discovery pools
            |
            v
 cheap eligibility / pre-ranking
            |
            v
 source refresh + readiness + competition verification
            |
            v
      scoring / effort model
            |
            v
 ranked queue + audit diagnostics
            |
            v
 instance-configured delivery channels
            |
            v
      seen_bounties.json
```

The important boundary is between **I/O** and **policy**. Network fetches gather evidence; pure functions should interpret that evidence whenever practical.

Strategic effort calibration distinguishes compound upgrade/firewall/bridge/host-reboot
regressions, macOS keychain failures under Tailscale SSH, and trusted maintainer
warnings about changing default-field normalization. Such evidence raises effort,
not contributor readiness; verified comments are reused without extra requests.

For strategic readiness, a current `stale` or `lifecycle/stale` label holds an
issue even if earlier maintainer comments indicated readiness. It becomes
eligible for normal verification again when the stale label is removed.
Only `OWNER`, `MEMBER`, and `COLLABORATOR` comments may approve implementation;
contributor project-action comments can still provide hold evidence. Explicit owning-team
triage handoffs and unresolved safe-scope questions hold strategic opportunities
until a later trusted maintainer approval; ordinary contributor opinions do not.
Cloud-managed metric discrepancies with unknown backend version and step-sensitive
samples, and untriaged failing-test image-artifact incidents, also need diagnostic
ownership and an implementation scope before being recommended. A later trusted
maintainer readiness comment can clear those holds.

## Current modules

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `opportunity_scout.py` | Stable executable entry point that calls `opportunity_scout.app.main()` | Root shim only; no scanner policy or compatibility façade |
| `opportunity_scout/app.py` | Package-only executable/application assembly, CLI/environment wiring, and mixed paid/strategic verification adapter | Uses the canonical package GitHub transport and package-owned delivery callbacks |
| `opportunity_scout/run.py` | Combined scan lifecycle, queue assembly, coverage accounting, delivery aggregation, and transactional seen-state commit | Owns one combined run without importing `opportunity_scout.app` or `opportunity_scout.py`; delivery transports enter only through typed callbacks, host reports remain explicit opt-in, and private reports require their own repository plus credential |
| `opportunity_scout/preferences.py` | Frozen `ScoutPreferences`, pure version-1 schema parsing, and explicit TOML file loading | Non-secret preferences only; depends on canonical `EffortBucket`, never imports app/run; required by application assembly using the default or explicit path |
| `opportunity_scout/selection.py` | Pure additive strategic targets, case-insensitive repository exclusions and language matching, exact final-effort matching, lane acceptance, and classification-based score thresholds | Uses immutable preferences and canonical candidates; no I/O or app/run dependency |
| `opportunity_scout/github.py` | Canonical GitHub JSON transport, explicit collection pagination, bounded Issues Search, issue/timestamp parsing, and keyed per-scan cache fills | `github_get()` stays single-page; `github_collection()` follows validated Links through the same safe-read retries and discards incomplete evidence; injectable fetchers remain deterministic test seams |
| `opportunity_scout/paid.py` | Pure paid-opportunity basic eligibility and issue-level payment-signal recognition over already-fetched issue evidence | No network I/O; canonical owner of `MAX_COMMENTS`, `PAYMENT_TERM_RE`, `AMOUNT_RE`, `payment_signal()`, and `is_clean_candidate()` |
| `opportunity_scout/paid_verification.py` | Paid proposal/meta rejection, active-claim detection, and open implementation-PR competition verification | May perform GitHub-backed verification through injectable transport; does not own discovery, scoring, or delivery |
| `opportunity_scout/types.py` | Canonical static domain literals and mapping records shared across package-owned scanner code | Dependency-light typing vocabulary only; raw external JSON remains dynamic until validated |
| `opportunity_scout/state.py` | Canonical typed, versioned seen-state parsing, logical membership/mutation, and deterministic atomic persistence | Local-file state only; branch-agnostic and fail-closed for malformed or unsupported existing state |
| `opportunity_scout/reporting.py` | GitHub queue reports, compact reject/audit summaries, and length-safe notification rendering | Presentation-only; no network I/O or scanner policy decisions |
| `opportunity_scout/delivery.py` | Telegram, Discord, host GitHub report delivery, and privacy-verified private GitHub report delivery | Transport only; the private path verifies repository metadata before issue creation, while report rendering and state orchestration remain elsewhere |
| `opportunity_scout/scoring.py` | Pure-ish effort estimation plus cash/career ranking over already-fetched evidence | No network I/O; owns scoring math and effort calibration, including trusted maintainer-history signals |
| `opportunity_scout/sources.py` | Curated GitHub issue pools, issue/comment fetches, contribution-guide lookup, bounty-platform adapters, and bounded adaptive inspection selection | Owns external source retrieval/parsing; does not rank final candidates or decide readiness |
| `opportunity_scout/strategic/claims.py` | Pure first-person ownership / implementation / PR-intent language detection | No network I/O and no dependency on `opportunity_scout.py` |
| `opportunity_scout/strategic/competition.py` | Active-claim, linked/timeline implementation-PR detection, and competition precedence | Uses package-owned paid verification plus strategic-only evidence |
| `opportunity_scout/strategic/discovery.py` | Strategic source-pool collection, near-miss audit diagnostics, adaptive inspection selection, and deterministic pre-verification ranking | Accepts narrow app adapters for paid predicates/signals; never imports `opportunity_scout.app` |
| `opportunity_scout/strategic/verification.py` | Ranked strategic deep-verification orchestration, bounded per-repo settlement, source-failure handling, and final strategic selection | Accepts typed app callbacks for mixed verification/preflight behavior; never imports `opportunity_scout.app` |
| `opportunity_scout/strategic/readiness.py` | Pure maintainer-readiness, triage, lifecycle, dashboard, and release-tracking policy | Interprets issue/comment evidence only; no network I/O or dependency on `opportunity_scout.py` |
| `seen_bounties.json` (or `--state PATH`) | Selected local runtime seen-state file | Version 2 is canonical and the only supported on-disk schema; incompatible existing files fail closed |
| `examples/seen_bounties.example.json` | Generic empty version-2 state seed | New instances only; never a replacement for historical state |
| `.github/workflows/python-quality.yml` | Authoritative formatting, lint, recursive compile, strict typing, tests, and coverage on Python 3.12; recursive compile and full-suite compatibility checks on 3.13 and 3.14 | Explicit CI versions define support; later releases require passing CI before support is claimed |
| `action.yml` | Pinned-source composite scanner execution with explicit config/state and credentials | No instance checkout, Git persistence, or trigger ownership |
| `examples/private-instance/scout.yml` | Generic manual private caller and serialized scan/state/recovery transaction | Private instance owns the deployed copy and pin; inactive in upstream |

## Dependency direction

```text
opportunity_scout.py --> opportunity_scout.app

opportunity_scout.app
    |-- opportunity_scout.delivery
    |-- opportunity_scout.github
    |-- opportunity_scout.paid
    |-- opportunity_scout.paid_verification
    |-- opportunity_scout.run
    |-- opportunity_scout.reporting
    |-- opportunity_scout.scoring
    |-- opportunity_scout.sources
    |-- opportunity_scout.strategic.claims
    |-- opportunity_scout.strategic.competition
    |-- opportunity_scout.strategic.discovery
    |-- opportunity_scout.strategic.verification
    |-- opportunity_scout.strategic.readiness

opportunity_scout.run --> opportunity_scout.reporting
opportunity_scout.run --> opportunity_scout.state
opportunity_scout.run --> opportunity_scout.preferences / opportunity_scout.selection
opportunity_scout.run -X-> opportunity_scout.app
opportunity_scout.run -X-> opportunity_scout.py
opportunity_scout.types -X-> package policy / orchestration modules
opportunity_scout.selection --> opportunity_scout.preferences / opportunity_scout.types
opportunity_scout.preferences --> opportunity_scout.types
opportunity_scout.preferences -X-> opportunity_scout.app / opportunity_scout.run
opportunity_scout.paid_verification --> opportunity_scout.github
opportunity_scout.paid_verification --> opportunity_scout.paid
opportunity_scout.sources --> opportunity_scout.github
opportunity_scout.scoring --> opportunity_scout.github
opportunity_scout.scoring --> opportunity_scout.strategic.readiness
opportunity_scout.strategic.competition --> opportunity_scout.github
opportunity_scout.strategic.competition --> opportunity_scout.paid_verification
opportunity_scout.strategic.competition --> opportunity_scout.strategic.claims
opportunity_scout.strategic.discovery --> opportunity_scout.github
opportunity_scout.strategic.discovery --> opportunity_scout.scoring
opportunity_scout.strategic.discovery --> opportunity_scout.sources
opportunity_scout.strategic.discovery --> opportunity_scout.strategic.readiness
opportunity_scout.strategic.verification --> opportunity_scout.sources
opportunity_scout.strategic.verification --> opportunity_scout.strategic.discovery
opportunity_scout.strategic.verification --> opportunity_scout.selection
opportunity_scout.strategic.verification -X-> opportunity_scout.app
opportunity_scout.strategic.readiness --> opportunity_scout.strategic.claims

package/domain modules -X-> opportunity_scout.py
package/domain modules -X-> opportunity_scout.app
```

New leaf modules should follow the same rule. The orchestration layer may compose domain modules; domain modules should not reach back into the orchestrator. `opportunity_scout.types` is deliberately dependency-light so policy, scoring, reporting, and orchestration can share domain contracts without creating circular imports.

## Preference parsing boundary

`ScoutPreferences` is a frozen dataclass with slots and tuple collections, independent
of credential-bearing `RunConfig`. `parse_scout_preferences()` validates a decoded
document without I/O; `load_scout_preferences(Path(...))` owns explicit local-file
reading with standard-library `tomllib`. Both return the same immutable model and
raise `ScoutPreferencesError` for invalid input. Missing files do not select defaults.
Diagnostics identify invalid fields without echoing configuration values or contents.

Version 1 is required. Omitted tables/fields receive deterministic generic defaults;
unknown keys, malformed types, unsupported versions, and invalid values fail closed.
The public [example](scout.example.toml) lists the runtime defaults with optional
`name` commented out; `name` currently has no runtime effect. The
[configuration reference](docs/CONFIGURATION.md) documents fields, bounds, examples,
and current CLI behavior. Historical configuration/cutover rationale remains in the
[completed migration record](docs/PRIVATE_DEPLOYMENT_MIGRATION.md).
Parsing does not perform discovery, filtering, scoring, delivery, or persistence.
Application assembly always loads preferences from default `scout.toml` in the
working directory or exactly explicit `--config PATH`, before loading state or
performing network/delivery activity. Missing or invalid configuration raises
`ScoutPreferencesError` without falling back to legacy defaults. `--state PATH` is independent of config and
defaults to `seen_bounties.json`; `RunConfig.state_path` reaches every state read and
both delivery and quiet-maintenance saves. State code remains branch-agnostic.

Immutable preferences are bound into narrow discovery callbacks. Combined-run
orchestration skips disabled lanes; paced prefetch requests include only enabled
paid Search and enabled strategic global Search. Disabling global Search preserves
curated strategic and paid sources. Disabled sources produce no coverage failures.
Configured repository sources add to the existing curated discovery list with
case-insensitive first-seen deduplication; exclusions win and remove curated requests.
They do not extend the built-in target-repository scoring set. After refresh or
aggregator resolution, the authoritative repository receives the existing +14
target-repo career bonus only when that resolved identity is a built-in target.
Search/platform
results are excluded before metadata/deep checks where their repository is known.
After source refresh and aggregator resolution, verification checks exclusions again
against the resolved upstream repository. Final paid/unpaid classification controls
lane acceptance before strategic repository-slot settlement, and queue assembly
filters again before deduplication and truncation. Preference rejection does not
weaken payment, readiness, competition, privacy, or coverage evidence requirements.

Language preferences match configured values case-insensitively against primary
repository language from the existing shared per-run metadata cache. Strategic discovery uses
already-fetched metadata to exclude direct-source candidates before bounded base and
adaptive inspection when the source repository identity is authoritative. Recognized
wrapper/aggregator candidates are not rejected by wrapper language because their
upstream repository is resolved later. Verification always re-applies the language
preference after refresh/aggregator resolution and metadata/archive checks, before
contribution-guide lookup, so the resolved upstream repository remains authoritative.
Final candidate language carries that metadata into the pure acceptance guard in both
discovery lanes and combined-run queue assembly, before repository-slot settlement
and final truncation. An empty list accepts all languages; explicit lists exclude
absent/null/empty primary language and the canonical `Unknown` display value.
Language policy adds neither metadata requests nor Search queries; inspection budget
constants and coverage accounting remain unchanged, while ineligible direct-source
candidates can avoid later deep verification work.

Effort preferences use pure `effort_accepted(EffortBucket, Sequence[EffortBucket])`
policy in the final-candidate acceptance guard. Exact membership uses the
unchanged estimator output; an empty effort list accepts nothing. Both discovery
lane adapters apply it after final candidate construction, before strategic
repository-slot settlement. Combined-run assembly repeats the guard before URL
deduplication and truncation. Preview effort and preview paid classification do not
prune on effort preferences: source refresh, aggregator resolution, and already
fetched strategic discussion may change the estimate. Existing preflight, ranking
bounds, source-failure breaker, adaptive inspection and verification budgets remain
in effect; effort preferences add no network work or scoring policy.

Score thresholds use pure `score_rejection()` in the final-candidate guard: paid candidates
use the inclusive cash threshold, unpaid candidates the inclusive career threshold,
regardless of discovery source. Both adapters and queue assembly apply eligibility
before repository-slot settlement, URL deduplication, and truncation. Strategic
verification also enforces final score eligibility for its injected verifier before
adding to its accepted set. A preview career upper bound cannot prove rejection
when refresh changes classification or scores; threshold pruning therefore follows
deep verification. Existing ranking upper bounds still govern repository settlement.
Inspection pools, adaptive budgets, worker caps, source-failure breakers, scoring,
and deterministic ordering remain unchanged. Low-scoring previews may now need
deep checks within that same bounded pool.

Combined-run queue assembly caps output at `preferences.max_results` (1–8), also
respecting any narrower injected `report_limit`; it does not shrink discovery or
verification budgets. Defaults remain 55 for each score threshold and eight results.
Name remains display metadata without current runtime use. Configuration guidance and deployment ownership/workflows are documented separately.

## GitHub integration contract

### REST identity and version

`opportunity_scout.github` is the canonical owner of GitHub REST request identity. GitHub JSON
requests use:

```http
Accept: application/vnd.github+json
User-Agent: OSSOpportunityScout
X-GitHub-Api-Version: 2022-11-28
```

The REST API version is intentionally pinned to `2022-11-28`. Version upgrades are explicit
compatibility work; the scanner does not automatically track GitHub's newest REST version.

### Authentication and workflow permissions

Upstream development CI grants only `contents: read` and has no scanner deployment
job or delivery secrets. The inactive private template uses workflow
`permissions: {}`; its normal `scout` job has only `contents: write`, while the
dependent recovery-cancellation job has only `actions: write`. The scanner token
therefore cannot mutate Actions state, and the cancellation token cannot write
repository contents. Neither job has Issues write. Instances own their permissions
and credentials; private-report credentials remain separate. Python state code is
branch-agnostic.

Host-repository reports remain a supported explicit opt-in. `GITHUB_TOKEN` and
`GITHUB_REPOSITORY` alone never enable them; a custom deployment must set
`GITHUB_REPORTS_ENABLED=true` and supply a GitHub credential with Issues write permission.

Private reports use a separate credential boundary:

```text
scanner GITHUB_TOKEN
-> scanner GitHub REST access; private caller may also use it for same-instance persistence

PRIVATE_GITHUB_REPORTS_TOKEN
-> private report repository metadata verification + issue creation only
```

Before a private report is created, the destination metadata is fetched with the private
credential and must report `private == true`; otherwise delivery fails closed. The private
report token is not exposed to discovery or verification code and should be scoped only to the
intended reports repository.

### Safe reads, rate limits, and mutations

Safe GitHub GETs use bounded retry with at most three attempts (two retries). Retry decisions
honor `Retry-After`, primary-rate-limit reset evidence, secondary rate limiting, temporary
`5xx` responses, and eligible timeout/connection failures. Ordinary `401`, non-rate-limit
`403`, and `404` responses are not blindly retried.

GitHub writes remain single-attempt. Report issue creation and host report issue updates are not
automatically retried because repeating a mutation can duplicate or otherwise compound side
effects.

GitHub diagnostics do not intentionally log scanner/private tokens, `Authorization` headers, or
full request objects. Safe-read failures are reported using a concise classification and the
non-token request URL.

### Pagination semantics

Pagination is evidence-sensitive. GitHub Search, curated repository discovery, and the paid
active-claim comment inspection remain intentionally bounded. Shared issue comments, paid
implementation-PR timelines, and strategic implementation-PR timelines require complete
evidence and therefore follow validated GitHub `Link` traversal. If complete collection
traversal cannot be verified, the caller fails closed rather than accepting partial evidence.

### Conditional requests and cache ownership

Conditional requests using `ETag` or `Last-Modified` were evaluated but are not currently
implemented. Per-run caches already eliminate important duplicate reads, while useful cross-run
conditional reuse would require persistent ownership of both validators and the prior response
bodies. That complexity is not currently justified by measured request volume.

Instance-owned seen-state is deduplication/lifecycle persistence, not an HTTP cache.
GitHub validators and response bodies must not be stored in seen-state. Conditional requests
can be reconsidered if polling or request volume materially increases.

OSS Opportunity Scout is a GitHub API integration developed by a participant in the GitHub
Developer Program. Program participation is not GitHub approval, certification, or endorsement.

## Coverage completeness and warnings

`coverage_status()` in `opportunity_scout/run.py` counts machine-classified strategic source,
comment, and implementation-PR timeline verification failures, paid active-claim comment
verification failures, and discovery failures. Human-readable reason text remains reporting-only;
`CoverageStatus.complete` requires zero combined failures. Ordinary policy rejections do not make
coverage incomplete.

`run_combined_scan()` requires completeness for both delivery-related state advancement and
quiet-run maintenance persistence. Warning thresholds control diagnostics independently:
one to four strategic verification failures do not trigger the default prominent warning,
but still prevent all state writes. Any recognized discovery or paid verification failure
continues to warn immediately. An incomplete quiet run below the warning threshold skips
delivery and maintenance and prints that state was not updated. Runs with candidates or a
coverage warning retain configured delivery behavior; successful delivery alone cannot admit
an incomplete state commit. `tests/test_run.py` and `tests/test_app.py` cover these boundaries.

## Invariants

- Shared comments and paid/strategic timelines require complete pagination. Collection traversal validates HTTPS GitHub API destinations, collection identity, unchanged query parameters, and successive page numbers; numeric repository aliases require matching repository metadata. Redirects are refused before credentials can be forwarded. A 1000-page safety ceiling fails closed for complete-evidence callers.
- Public bounty-platform HTML redirects stay within the same normalized HTTPS origin (scheme, hostname, and effective port). Cross-origin redirects, HTTPS downgrades, malformed ports, and userinfo targets are rejected before the replacement request is issued.
- Source-derived text is untrusted at delivery sinks. Telegram neutralizes `@username` syntax in outbound text even without `parse_mode`; Discord disables mention parsing for webhook payloads, and GitHub report rendering neutralizes `@user` / `@org/team` syntax at the completed-report boundary. None of these protections changes scanner selection, scoring, or persisted candidate/state data.
- Search remains one intentionally bounded page. Curated repository discovery retains its configured page/result limits and short-page termination. The separate paid active-claim check retains its existing at-most-30-comment request; issue-specific comment sorting is not guaranteed by the documented API.

- Paid candidates require explicit payment evidence and must represent open work rather than payout-history/leaderboard summaries.
- Strategic candidates must be refreshed and re-verified before they reach the final queue.
- Assigned, actively claimed, superseded, discussion-only, or already-implemented work must not be presented as ready work.
- Scanner misses are useful product feedback; audit paths should remain observable.
- Repository/network failures should degrade coverage explicitly rather than silently turning into positive verification.
- Strategic deep verification is rate-budgeted: inspect broadly, verify in rank order, and stop only when remaining candidates cannot displace the kept set under the known verification-score uplift bound. Final strategic effort may use the already-fetched trusted maintainer discussion to recognize implementation-history complexity; preview scoring stays source-only and this calibration must not add network fan-out.
- Comment-fetch failure must remain distinguishable from a real empty discussion thread; strategic verification fetches each issue comment thread once through the checked path and reuses that evidence for payment detection, readiness, competition, and final scoring. Failed implementation-PR timeline checks are also verification failures rather than evidence of no competition. Strategic verification does not use per-issue GitHub Search queries, preserving Search quota for discovery. Any recognized discovery/verification failure prevents seen-state advancement, independently of prominent warning thresholds.
- Seen-state loading fails closed for malformed JSON, malformed schema, unsupported versions, obsolete unversioned formats, or unreadable existing files; only a genuinely absent file means empty first-run state.
- Seen-state maintenance is bounded to 20 direct GitHub issue checks per successful run with a 30-day minimum recheck interval. Selection is deterministic: never-checked entries first, then oldest `last_checked_at`, then URL. `last_checked_at` records the maintenance attempt time, including not-found and failed checks, so one bad entry cannot monopolize later maintenance batches.
- Only direct lifecycle evidence of `closed` prunes a GitHub issue. `open`, ambiguous `404`/not-found, auth/rate-limit/server/network failures, malformed responses, and checker exceptions all retain the URL. Non-GitHub URLs remain seen and are excluded from GitHub maintenance until a platform-specific lifecycle policy exists.
- Successful maintenance and newly reported URLs are persisted as one state snapshot. Complete quiet runs may persist maintenance alone; incomplete combined coverage or failed delivery persists neither maintenance nor newly reported URLs. A later reopen of a previously confirmed-closed issue is intentionally eligible to surface again.
- `opportunity_scout.state` knows only the local state file, and Python state code contains no Git branch/worktree logic. Git transport belongs to the private instance workflow, which owns its state/history without an upstream state-branch contract.
- `.scout/recovery-required` is private-instance coordination metadata, not seen-state. A normal run must fetch the current private default branch and reject that marker before scanner execution. Persistence failure must establish it from current remote history without overwriting `seen_bounties.json`; only deliberate operator recovery removes it. Queued-run cancellation remains defense in depth rather than the recovery correctness barrier.
- GitHub API authentication and GitHub report publishing are separate concerns. `GITHUB_TOKEN` and `GITHUB_REPOSITORY` may be present for scanner API work, but host-repository report publishing requires explicit `GITHUB_REPORTS_ENABLED=true`. Reusable execution machinery must never publish ranked scout results merely because GitHub credentials and repository identity are available.
- Private GitHub reporting requires both `PRIVATE_GITHUB_REPORTS_REPOSITORY` and `PRIVATE_GITHUB_REPORTS_TOKEN`; neither reuses or replaces the scanner GitHub authentication context. Before each private report issue is created, GitHub repository metadata must be retrieved with the private credential and report `private: true`. Missing, malformed, public, unauthenticated, or failed verification is a hard no-publish result.
- Seen-state advances only after a configured delivery succeeds, and combined runs with incomplete discovery/verification coverage still do not advance it. An explicitly enabled GitHub report whose auto-close step fails remains a failed delivery for this transaction.
- Tests must cover scanner policy without live network access.
- Ruff, strict mypy, and 100% statement + branch coverage are repository-wide quality gates.

## Documentation maintenance

- `README.md` is the human-facing project overview and run/deployment entry point.
- `CONTRIBUTING.md` owns the human contributor workflow.
- `AGENTS.md` owns AI coding-agent execution rules.
- `ARCHITECTURE.md` owns shared current boundaries, flow, and invariants.
- `ROADMAP.md` owns forward-looking work and sequencing.
- `docs/PRIVATE_DEPLOYMENT_MIGRATION.md` is the completed migration/acceptance record: historical contracts, PR slices, recovery, provenance, and final ownership boundaries.
