# Architecture

## Goals

OSS Opportunity Scout has two lanes with different risk profiles:

- **Paid bounty lane:** conservative payment and competition verification protected by regression tests.
- **Strategic OSS lane:** broader discovery plus project-specific readiness, competition, scoring, and ranking.

The architecture should make those lanes easy to reason about without forcing contributors or AI tools to load the full scanner into context.

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
 GitHub issue / optional notifications
            |
            v
      seen_bounties.json
```

The important boundary is between **I/O** and **policy**. Network fetches gather evidence; pure functions should interpret that evidence whenever practical.

## Current modules

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `opportunity_scout.py` | Stable executable entry point that calls `bountyscout.app.main()` | Root shim only; no scanner policy or compatibility façade |
| `bountyscout/app.py` | Package-only executable/application assembly, environment wiring, and mixed paid/strategic verification adapter | Uses the canonical package GitHub transport and package-owned delivery callbacks |
| `bountyscout/run.py` | Combined scan lifecycle, queue assembly, coverage accounting, delivery aggregation, and transactional seen-state commit | Owns one combined run without importing `bountyscout.app` or `opportunity_scout.py`; delivery transports enter only through typed callbacks |
| `bountyscout/github.py` | Canonical GitHub JSON and Issues Search transport with one request identity, plus generic issue/timestamp parsing and keyed per-scan cache-fill primitives | No scanner policy; all normal GitHub API traffic uses `github_get()` with `OSSOpportunityScout`; injectable fetchers remain deterministic test seams |
| `bountyscout/paid.py` | Pure paid-opportunity basic eligibility and issue-level payment-signal recognition over already-fetched issue evidence | No network I/O; canonical owner of `MAX_COMMENTS`, `PAYMENT_TERM_RE`, `AMOUNT_RE`, `payment_signal()`, and `is_clean_candidate()` |
| `bountyscout/paid_verification.py` | Paid proposal/meta rejection, active-claim detection, and open implementation-PR competition verification | May perform GitHub-backed verification through injectable transport; does not own discovery, scoring, or delivery |
| `bountyscout/types.py` | Canonical static domain literals and mapping records shared across package-owned scanner code | Dependency-light typing vocabulary only; raw external JSON remains dynamic until validated |
| `bountyscout/state.py` | Canonical typed, versioned seen-state parsing, legacy migration, logical membership/mutation, and deterministic atomic persistence | Local-file state only; branch-agnostic and fail-closed for malformed or unsupported existing state |
| `bountyscout/reporting.py` | GitHub queue reports, compact reject/audit summaries, and length-safe notification rendering | Presentation-only; no network I/O or scanner policy decisions |
| `bountyscout/delivery.py` | Telegram, Discord, and generated GitHub report HTTP delivery | Transport only; does not render reports, orchestrate scanner state, or make policy/ranking decisions |
| `bountyscout/scoring.py` | Pure-ish effort estimation plus cash/career ranking over already-fetched evidence | No network I/O; owns scoring math and effort calibration, including trusted maintainer-history signals |
| `bountyscout/sources.py` | Curated GitHub issue pools, issue/comment fetches, contribution-guide lookup, bounty-platform adapters, and bounded adaptive inspection selection | Owns external source retrieval/parsing; does not rank final candidates or decide readiness |
| `bountyscout/strategic/claims.py` | Pure first-person ownership / implementation / PR-intent language detection | No network I/O and no dependency on `opportunity_scout.py` |
| `bountyscout/strategic/competition.py` | Active-claim, linked/timeline implementation-PR detection, and competition precedence | Uses package-owned paid verification plus strategic-only evidence |
| `bountyscout/strategic/discovery.py` | Strategic source-pool collection, near-miss audit diagnostics, adaptive inspection selection, and deterministic pre-verification ranking | Accepts narrow app adapters for paid predicates/signals; never imports `bountyscout.app` |
| `bountyscout/strategic/verification.py` | Ranked strategic deep-verification orchestration, bounded per-repo settlement, source-failure handling, and final strategic selection | Accepts typed app callbacks for mixed verification/preflight behavior; never imports `bountyscout.app` |
| `bountyscout/strategic/readiness.py` | Pure maintainer-readiness, triage, lifecycle, dashboard, and release-tracking policy | Interprets issue/comment evidence only; no network I/O or dependency on `opportunity_scout.py` |
| `seen_bounties.json` | Local runtime seen-state file | Version 2 is canonical; legacy URL lists load losslessly and rewrite as version 2 on the next successful save |
| `.github/workflows/oss-opportunity-scout.yml` | Scheduled scanner execution | Runtime workflow, not the quality gate |
| `.github/workflows/python-quality.yml` | Formatting, lint, map, typing, tests, coverage | Must stay fast enough for normal PR iteration |

## Dependency direction

```text
opportunity_scout.py --> bountyscout.app

bountyscout.app
    |-- bountyscout.delivery
    |-- bountyscout.github
    |-- bountyscout.paid
    |-- bountyscout.paid_verification
    |-- bountyscout.run
    |-- bountyscout.reporting
    |-- bountyscout.scoring
    |-- bountyscout.sources
    |-- bountyscout.strategic.claims
    |-- bountyscout.strategic.competition
    |-- bountyscout.strategic.discovery
    |-- bountyscout.strategic.verification
    |-- bountyscout.strategic.readiness

bountyscout.run --> bountyscout.reporting
bountyscout.run --> bountyscout.state
bountyscout.run -X-> bountyscout.app
bountyscout.run -X-> opportunity_scout.py
bountyscout.types -X-> package policy / orchestration modules
bountyscout.paid_verification --> bountyscout.github
bountyscout.paid_verification --> bountyscout.paid
bountyscout.sources --> bountyscout.github
bountyscout.scoring --> bountyscout.github
bountyscout.scoring --> bountyscout.strategic.readiness
bountyscout.strategic.competition --> bountyscout.github
bountyscout.strategic.competition --> bountyscout.paid_verification
bountyscout.strategic.competition --> bountyscout.strategic.claims
bountyscout.strategic.discovery --> bountyscout.github
bountyscout.strategic.discovery --> bountyscout.scoring
bountyscout.strategic.discovery --> bountyscout.sources
bountyscout.strategic.discovery --> bountyscout.strategic.readiness
bountyscout.strategic.verification --> bountyscout.sources
bountyscout.strategic.verification --> bountyscout.strategic.discovery
bountyscout.strategic.verification -X-> bountyscout.app
bountyscout.strategic.readiness --> bountyscout.strategic.claims

package/domain modules -X-> opportunity_scout.py
package/domain modules -X-> bountyscout.app
```

New leaf modules should follow the same rule. The orchestration layer may compose domain modules; domain modules should not reach back into the orchestrator. `bountyscout.types` is deliberately dependency-light so policy, scoring, reporting, and orchestration can share domain contracts without creating circular imports.

## Refactoring direction

Phases 3B through 4E moved reusable parsing, paid policy, verification, delivery, state, and transport into canonical package ownership. OSS Cleanup 1 removed the now-unused legacy root compatibility scanner; `opportunity_scout.py` remains the supported root runtime shim.

Historical generated queue reports from before the current auto-close lifecycle were cleaned once with `scripts/close_legacy_scan_reports.py` after explicit report-identity verification. Current generated reports are auto-closed during normal delivery, and there is no recurring cleanup service.

Phase 4C established package-owned strategic discovery, verification, and combined-run orchestration. Phase 4E completed package ownership of paid parsing, policy, verification, delivery, GitHub Search, and GitHub GET transport. OSS Cleanup 4 unified all GitHub JSON traffic under the canonical `OSSOpportunityScout` request identity while retaining injectable transport seams for deterministic tests. Production runtime now follows `opportunity_scout.py → bountyscout.app / bountyscout.run → package modules`.

## Invariants

- Paid candidates require explicit payment evidence and must represent open work rather than payout-history/leaderboard summaries.
- Strategic candidates must be refreshed and re-verified before they reach the final queue.
- Assigned, actively claimed, superseded, discussion-only, or already-implemented work must not be presented as ready work.
- Scanner misses are useful product feedback; audit paths should remain observable.
- Repository/network failures should degrade coverage explicitly rather than silently turning into positive verification.
- Strategic deep verification is rate-budgeted: inspect broadly, verify in rank order, and stop only when remaining candidates cannot displace the kept set under the known verification-score uplift bound. Final strategic effort may use the already-fetched trusted maintainer discussion to recognize implementation-history complexity; preview scoring stays source-only and this calibration must not add network fan-out.
- Comment-fetch failure must remain distinguishable from a real empty discussion thread; strategic verification fetches each issue comment thread once through the checked path and reuses that evidence for payment detection, readiness, competition, and final scoring. Failed implementation-PR timeline checks are also verification failures rather than evidence of no competition. Strategic verification does not use per-issue GitHub Search queries, preserving Search quota for discovery. Incomplete verification runs warn prominently and do not advance seen-state.
- Seen-state loading fails closed for malformed JSON, malformed schema, unsupported versions, or unreadable existing files; only a genuinely absent file means empty first-run state. Legacy list entries remain logically seen and migrate with unknown lifecycle timestamps represented as `null`.
- Seen-state maintenance is bounded to 20 direct GitHub issue checks per successful run with a 30-day minimum recheck interval. Selection is deterministic: never-checked entries first, then oldest `last_checked_at`, then URL. `last_checked_at` records the maintenance attempt time, including not-found and failed checks, so one bad entry cannot monopolize later hourly batches.
- Only direct lifecycle evidence of `closed` prunes a GitHub issue. `open`, ambiguous `404`/not-found, auth/rate-limit/server/network failures, malformed responses, and checker exceptions all retain the URL. Non-GitHub URLs remain seen and are excluded from GitHub maintenance until a platform-specific lifecycle policy exists.
- Successful maintenance and newly reported URLs are persisted as one state snapshot. Complete quiet runs may persist maintenance alone; incomplete combined coverage or failed delivery persists neither maintenance nor newly reported URLs. A later reopen of a previously confirmed-closed issue is intentionally eligible to surface again.
- `bountyscout.state` knows only the local state file. Scheduled production persistence remains the workflow's `scout-state` responsibility, and Python state code contains no Git branch/worktree logic.
- Seen-state advances only after a configured delivery succeeds, and combined runs with incomplete discovery/verification coverage still do not advance it. A GitHub report whose auto-close step fails remains a failed delivery for this transaction.
- Tests must cover scanner policy without live network access.
- Ruff, strict mypy, and 100% statement + branch coverage are repository-wide quality gates.

## Documentation maintenance

- `AGENTS.md` owns coding-agent and contributor implementation rules.
- `ARCHITECTURE.md` owns boundaries, flow, and invariants.
- `CODEBASE_MAP.md` is generated from the AST and owns symbol navigation.
- `README.md` stays user-facing and should not become an internal design dump.
