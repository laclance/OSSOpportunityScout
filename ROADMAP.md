# Roadmap

This file describes deliberate future improvements. It is not a description of the
current runtime architecture.

- `ARCHITECTURE.md` describes what exists today.
- `ROADMAP.md` describes planned work and sequencing.
- `CODEBASE_MAP.md` is generated symbol navigation.
- `AGENTS.md` contains implementation and contributor rules.

## Product direction

The supported runtime entry point is:

```bash
python opportunity_scout.py
```

Paid and strategic scanning are package-owned. The project originated from
`dev-kp-eloper/BountyScout` and subsequently underwent an OSS-independence cleanup
before standalone release preparation. The historical fork remains development
provenance rather than the definition of the current architecture.

## Phase 4A — strong domain typing

Keep raw transport/API data dynamic until it is validated, then use explicit internal
domain records through policy, scoring, reporting, and orchestration. Preserve current
dictionary runtime shapes during this phase; prefer standard-library typing tools such
as `TypedDict`, `Literal`, and `TypeAlias` over runtime model dependencies.

Broad result-object conversion is intentionally deferred.

## Phase 4B — state lifecycle

### Phase 4B.1 — canonical versioned state — complete

- `bountyscout.state` owns typed seen-state parsing, logical access, mutation, and persistence
- schema version 2 stores per-URL lifecycle fields without inventing legacy timestamps
- the historical JSON URL list remains loadable and migrates on the next successful save
- malformed, unreadable, or unsupported existing state fails closed instead of becoming empty
- `scout-state` remains authoritative scheduled-workflow persistence
- Python remains branch-agnostic and local execution still uses ordinary `seen_bounties.json`

### Phase 4B.2 — bounded retention and compaction — complete

- bounded direct issue revalidation uses a 20-call maximum per successful run
- entries are rechecked no more often than every 30 days, with unknown legacy timestamps treated as oldest
- deterministic ordering walks never-checked entries first, then oldest checked entries, then URL
- only confirmed closed GitHub issues are pruned; failures, 404s, and non-GitHub URLs remain seen
- complete quiet runs can compact state, while failed delivery or incomplete combined coverage persists no maintenance changes
- confirmed-closed entries may surface again if the issue is later reopened

### Phase 4B.3 — legacy generated-report cleanup — complete

- one-time cleanup requires explicit repository, open-state, non-PR, label, title/body, and automation-author identity signals
- the 28 historical pre-auto-close combined queue reports were closed as `not_planned`; paid-only alert artifacts were intentionally left untouched
- current generated reports remain owned by the normal auto-close delivery lifecycle; no recurring cleanup service exists

## Phase 4C — app decomposition

Further split cohesive orchestration boundaries out of `bountyscout.app` where that
improves readability and testability.

### Phase 4C.1 — strategic discovery orchestration — complete

- `bountyscout.strategic.discovery` owns strategic source-pool collection, near-miss auditing, adaptive inspection selection, and deterministic pre-verification ranking
- `bountyscout.app` passes narrow paid-lane callbacks between package-owned components
- discovery queries, request budgets, ordering, and cache lifetime remain unchanged

### Phase 4C.2 — strategic verification orchestration — complete

- `bountyscout.strategic.verification` owns ranked deep-verification orchestration, bounded per-repository settlement, source-failure handling, and final strategic selection
- `bountyscout.app.verify()` remains the mixed paid/strategic verification adapter and is supplied as a narrow callback
- verification request budgets, source-failure semantics, upper-bound pruning, repo-slot settlement, and final per-repo ordering remain unchanged

### Phase 4C.3 — combined run lifecycle — complete

- `bountyscout.run` owns combined discovery coordination, final queue assembly, coverage accounting, delivery aggregation, and the transactional seen-state commit
- `bountyscout.app.main()` is now a thin environment/callback assembly layer
- delivery/state semantics, Search budgets, cache lifetime, report lifecycle, and paid-lane behavior remain unchanged
- paid operations enter the run layer only through narrow typed callbacks

**Phase 4C is complete.** The remaining `bountyscout.app` responsibilities are intentional application seams rather than another decomposition target.

## Phase 4D — effort estimator decomposition — complete

`estimate_effort_details()` is now an explicit ordered decision flow over a private
immutable effort context and cohesive private rule helpers.

- effort buckets, emitted reasons, regex semantics, thresholds, and first-match precedence remain unchanged
- trusted maintainer-comment evidence and documentation-specific behavior remain unchanged
- no network or request behavior changed

Any scoring behavior change discovered during decomposition belongs in a separate
regression-backed change.

**Phase 4D is complete.**

## Phase 4E — paid-scanner independence — complete

Move reusable paid-scanner behavior into canonical package ownership so production
scanning no longer depends on a root compatibility module.

### Phase 4E.1 — package-owned USD-like reward parsing — complete

- canonical USD-like amount parsing moved to `bountyscout.scoring`
- `bountyscout.scoring` owns the parsing behavior directly
- scoring behavior remained unchanged

### Phase 4E.2 — package-owned paid eligibility policy — complete

- `bountyscout.paid` owns basic paid eligibility and issue-level payment-signal recognition
- combined scanning consumes the package policy directly
- request and paid-lane behavior remained unchanged

### Phase 4E.3 — package-owned GitHub Search transport — complete

- canonical GitHub Issues Search request construction and normalization live in `bountyscout.github`
- combined paid and strategic discovery consume package Search directly
- queries, ordering, pacing, and request budgets remained unchanged

### Phase 4E.4 — package-owned paid rejection/competition verification — complete

- paid rejection precedence and paid-lane competition checks moved to `bountyscout.paid_verification`
- strategic competition consumes package verification directly
- network/request behavior remained unchanged

### Phase 4E.5 — package-owned delivery transports — complete

- Telegram, Discord, and GitHub report delivery moved to `bountyscout.delivery`
- the combined app consumes package delivery directly
- HTTP payloads, timeouts, request identity, auto-close behavior, and state semantics remained unchanged

### Phase 4E.6 — remove final package dependency on legacy root scanner — complete

- the package-owned GitHub GET transport is the final replacement for the legacy root-scanner request path
- the combined app consumes package transport directly
- production package code has zero legacy root-scanner dependencies

**Phase 4E is complete.** All reusable behavior needed by production scanning is
package-owned.

## OSS independence cleanup

### Cleanup 1 — remove legacy root scanner — complete

- removed the obsolete root compatibility scanner after Phase 4E package ownership completed
- removed tests that existed only for the historical façade
- kept `python opportunity_scout.py` as the supported root runtime entry point
- preserved package behavior, coverage, and quality gates

### Cleanup 2 — rewrite delivery transport — complete

- independently re-authored Telegram, Discord, and generated GitHub report delivery
- preserved payloads, timeouts, report lifecycle, and delivery/state semantics

### Cleanup 3 — restructure paid eligibility — complete

- restructured paid eligibility and payment-signal policy under package ownership
- preserved paid discovery and rejection behavior

### Cleanup 4 — unify GitHub transport identity — complete

- unified normal GitHub JSON traffic under the `OSSOpportunityScout` request identity
- retained deterministic injectable transport seams for tests

### Cleanup 5 — re-author scheduled workflow — complete

- replaced the inherited scheduled workflow with an independently authored OSS Opportunity Scout workflow
- preserved the hourly schedule, manual dispatch, secrets, permissions, state persistence, and runtime command

### Cleanup 6 — update standalone branding and provenance — complete

- removed stale fork/compatibility terminology from current-project documentation
- documented historical origin and the clean-snapshot standalone-release boundary in `PROVENANCE.md`
- made local repository configuration examples portable
- changed no Python, workflow behavior, licensing, repository identity, or Git history

## Deferred result-object work

After mapping-based domain contracts are stable, consider immutable result objects or
dataclasses where they improve invariants for:

- verification outcomes
- scoring results
- effort results
- source fetch results
- rejection results

Do not convert mapping-heavy runtime paths merely for stylistic consistency.

## Deferred state-file relocation

Do not move `seen_bounties.json` into a data/state directory until the state schema,
migration, retention, and compaction semantics are stable.

Scheduled production persistence currently uses the `scout-state` branch; relocation
must account for that workflow explicitly.

## Deferred architecture enforcement

If dependency-direction drift becomes recurring, consider lightweight CI checks for
invariants such as:

- strategic policy must not import `bountyscout.app`
- package/domain modules must not import root `opportunity_scout.py`
- transport must not depend on high-level scanner policy

Prefer simple checks with clear maintenance value over custom architecture tooling.
