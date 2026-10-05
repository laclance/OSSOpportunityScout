# Private instance execution template

The public [composite action](../action.yml) and generic, manual-only
[private workflow template](../examples/private-instance/scout.yml) are distribution
assets for independent private instances; they do not create an instance or migrate
history. Local scans require `scout.toml` in the working directory or explicit
`--config PATH`. See the
[completed migration record](PRIVATE_DEPLOYMENT_MIGRATION.md) for historical
implementation and acceptance details.

## Ownership and pins

An independent **private** instance repository owns its `.github/workflows/scout.yml`,
triggers, any future schedules, concurrency, secrets, `scout.toml`,
`seen_bounties.json`, Git state/history, delivery configuration, and scanner pin.
Upstream owns scanner code, development CI, reusable execution, and generic examples.
Forking is optional for scanner-code customization; use a deliberate full SHA from
upstream or a customized fork. Never run an upstream persistent workflow that reads
private instance state remotely.

The template pins checkout, Python setup, artifact upload, and scanner execution
to full commit SHAs. The distributed template contains a deliberately reviewed full
scanner SHA. Before adopting or upgrading it, review the selected upstream commit
and its checks. Branches and moving tags must not silently upgrade the scanner.
Changing the scanner pin is an instance-owned deployment change; a release may
identify a candidate SHA, but the instance owner decides whether to adopt it.

The action executes `opportunity_scout.py` and its package from `github.action_path`,
using isolated Python 3.12 with that source directory explicitly inserted into the
import path. The caller's working directory resolves config/state paths, but cannot
replace scanner modules through its checkout or `PYTHONPATH`. The scanner needs
only the standard library. The public action performs no checkout, state transport,
Git commit, scheduling, or recovery upload; those belong to the private caller.
[GitHub action context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context)
and [immutable action pins](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/find-and-customize-actions).

For future instance setup, copy the workflow to `.github/workflows/scout.yml`, copy
the [generic config](../scout.example.toml) to `scout.toml`, and supply deliberately
seeded version-2 state at `seen_bounties.json` on the private default branch. A new
instance with no history may deliberately copy the
[empty state example](../examples/seen_bounties.example.json) to `seen_bounties.json`.
An existing instance must preserve its trustworthy history; never substitute this
empty example for existing state. The template rejects missing files and symlinks;
the scanner validates config and existing state before discovery or delivery.
The distribution assets do not perform instance setup or state migration.

## Private instance acceptance checklist

The original migrated instance completed these gates with operational evidence kept
private. Every new independent instance should establish equivalent evidence for
itself; recovery or offline checks alone do not complete operational acceptance.
Resolve the instance repository, personal preferences, delivery destinations, and
secure provisioning of the separate report credential before dependent actions.
Keep the following evidence in private storage or the confirmed private instance:

| Gate | Private evidence required |
| --- | --- |
| Historical recovery | Newer sources checked, selected source/commit/path, original snapshot bytes and checksum, canonical version-2 parser/scanner SHA, entry count, and provenance establishing trust |
| Instance ownership | GitHub metadata explicitly confirming repository privacy and independence; default branch owns personal config, seeded state, manual-only workflow, full scanner SHA, and persistence/history |
| Configuration and pin | Personal config validated with the pinned parser; deliberate scanner SHA reviewed against source and passing checks; remote seed verified byte for byte against the recovered snapshot |
| Credentials and delivery | Explicit intended channels/destinations; scanner/persistence credential separate from report credential; report-repository metadata verified private with the report credential; host reporting disabled |
| First manual scan | Run/attempt, scanner pin, transaction base, coverage result, intended delivery evidence, and resulting state; delivery and persistence outcomes recorded separately |
| Remote persistence | Private remote state retrieved after the run, canonically parsed, and compared with the exact resulting snapshot and delivered entries; normal state-only commit or justified unchanged-state result |
| Subsequent deduplication | A later manually dispatched scan reads the persisted state and does not repeat previously reported URLs; distinguish a quiet run from a run that actually rechecks those candidates |
| Controlled transaction checks | Observed serialization and current-state reads after admission; deliberately stale write rejected without remote overwrite; exact recovery artifact uploaded privately with three-day retention; durable recovery marker established from current remote history and blocks later scans until deliberate recovery removes it |

Use controlled checks without report delivery for serialization, stale writes, and
artifact handling. Record which checks exercised GitHub Actions and which were
offline. A controlled artifact check proves preservation and restoration mechanics;
the separate delivery/persistence/deduplication gates still require real scan
evidence. Do not add fabricated opportunity entries to the production state for a
check or treat incomplete coverage as permission to advance it.

If a manual scan delivers but state persistence does not complete, stop new
dispatches immediately. The current template distinguishes two recovery modes. When
the scanner saved the resulting state locally but remote Git persistence fails, the
workflow preserves those exact bytes in the private short-retention recovery
artifact before establishing `.scout/recovery-required`. When delivery succeeded
but the scanner's local state save fails, the action emits a narrow recovery signal;
the workflow establishes the same durable marker but does **not** treat the local
file as an exact post-delivery snapshot. That case requires reconstruction from
trustworthy private evidence.

The marker remains the correctness barrier: every later run reads the current branch
and fails before scanner execution while it exists. Queued-run cancellation remains
defense in depth and operator convenience; its failure does not remove the marker
barrier. Follow [recovery before rerunning](#recover-before-rerunning) and do not
dispatch the deduplication scan until remote restoration and marker removal are
verified. Missing recovery evidence, incomplete coverage, ambiguous delivery, and
candidates not rechecked remain explicit gaps rather than successful acceptance
claims.

Public tracker/PR updates contain only non-sensitive status and public source/check
identifiers. Keep personal repository identities, preferences, secrets, state
contents/counts, opportunity URLs, run IDs, recovery hashes, and operational logs
private. The original migrated instance completed these gates; operational evidence
remains private. Every new independent instance must prove equivalent gates for
itself.

## Action inputs and credentials

| Input | Required | Meaning |
| --- | --- | --- |
| `config-path` | Yes | Explicit version-1 TOML path |
| `state-path` | Yes | Existing seen-state path in this template |
| `github-token` | Yes | Scanner GitHub discovery/verification credential |
| `private-reports-repository` | No | Private GitHub delivery repository, `owner/name` |
| `private-reports-token` | No | Separate credential for private report metadata and issue creation |
| `telegram-bot-token` / `telegram-chat-id` | No | Optional Telegram delivery configuration |
| `discord-webhook-url` | No | Optional Discord delivery configuration |

Paths can be relative to the caller workspace or absolute. The state parent must
exist. Missing, unreadable, malformed, or unsupported explicit config fails closed,
as does unreadable/malformed/unsupported existing state. Only absent state means
first run in the raw scanner/action; this template instead requires deliberately
seeded state. Local invocations require `scout.toml` by default or explicit
`--config PATH`. There is no legacy fallback.
See the [configuration reference](CONFIGURATION.md).

The template sets workflow-level `permissions: {}`. The normal `scout` job has
only `contents: write`, which covers public scanner REST access, same-instance
state persistence, and the failure-only durable recovery-marker commit. The pinned
scanner therefore receives no Actions write capability through `github.token`.

A tiny dependent `cancel_queued` job has only `actions: write` and runs only when
the scan job publishes a persistence-failure recovery handoff. It performs
defense-in-depth queue cancellation and then emits the terminal recovery failure.
It has no Contents or Issues write permission. Because concurrency is workflow-level,
the whole run remains in `scout-seen-state` until this follow-up job completes.
Checkout retains only the scan job's Contents credential for normal Git transport.

Private reports receive `PRIVATE_GITHUB_REPORTS_REPOSITORY` and
`PRIVATE_GITHUB_REPORTS_TOKEN` from separately configured instance secrets. Give
that token access only to the intended reports repository and the permissions
needed to read its metadata and create issues. Never reuse it for scanner discovery
or state persistence. Before report creation, the scanner must retrieve metadata
with that token and receive `private: true`; failure cannot count as delivery.
Optional Telegram/Discord values are likewise supplied explicitly from instance
secrets. Neither TOML nor public template files contain credentials or personal data.
The action disables host-repository reporting, including any inherited opt-in.
The CLI retains its separate explicit host-report option for custom deployments.

## Complete scan/state transaction

Workflow-level concurrency uses the constant `scout-seen-state` group,
`cancel-in-progress: false`, and `queue: max`. Every workflow writing this shared
state must use that same group, across branch and workflow names. The lock covers
private/default-branch checks, checkout, state reads, the durable recovery gate,
discovery/verification, delivery, local state save, remote persistence, recovery
upload, durable marker establishment, queued-run cancellation, and final failure.
Do not move state reads or the recovery gate into an earlier job or key concurrency
by workflow/ref. The queue is bounded at 100 pending runs; queue overflow cancels
additional runs. Waiting order follows admission to the group rather than dispatch
order.
[GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

After serialization begins, checkout selects the current private default branch,
and the state-read step fetches it again, detaches at that current head, and records
the transaction base SHA. It deliberately avoids the dispatch event's stale SHA.
Before scanner execution, the next step checks `.scout/recovery-required`. Any
file or symlink at that path fails the run before discovery or delivery. Absence is
the only normal-run state. The marker is private-instance coordination metadata,
not seen-state, and no scout step automatically removes it.

Only after that gate passes does the action read state and run once. Existing
verification, coverage, aggregate-delivery, and bounded lifecycle maintenance
invariants decide whether the local state changes; the workflow does not reinterpret
those decisions.

Persistence fetches the branch without resetting or replacing the resulting state.
It rejects any remote head different from the recorded base, even if only an
unrelated instance file changed. An unchanged state creates no commit. A changed
state alone is committed and pushed normally; a race after fetch fails the ordinary
non-forced push. There is no force push, automatic rebase/merge, reset to an older
snapshot, retry of delivery, or automatic replay. Default-branch rules must permit
the instance bot's normal state commit; a rejected push triggers recovery.

## Recover before rerunning

Remote persistence failure uses this ordering while the failed run still owns the
shared concurrency group:

```text
persistence failure
    -> preserve exact recovery snapshot
    -> establish .scout/recovery-required from current remote history
    -> cancel queued runs as defense in depth
    -> fail

later run
    -> fetches current private default branch
    -> sees .scout/recovery-required
    -> fails before scanner discovery or delivery

operator recovery
    -> restore or reconcile state
    -> remove marker deliberately in the recovery commit
    -> verify remote state
    -> scanning resumes
```

Any persistence-step failure, including fetch, stale-head, commit, or push failure,
first attempts to upload the exact resulting `seen_bounties.json` as
`scout-state-recovery-<run-id>-<run-attempt>`. The upload runs only in the confirmed
private caller repository, has **three-day retention**, and fails if the file is
missing. It uploads no checkout, config, report, marker, or secret files. This is a
recovery artifact, not the primary state backend.

Next, the workflow fetches the **current** private default branch into a separate
temporary Git worktree. It never bases the recovery marker on the stale scan
checkout or on a local state commit whose push failed. If
`.scout/recovery-required` is absent, the workflow adds only that marker and pushes
the marker commit normally. It never writes `seen_bounties.json`, force-pushes,
rebases, or resets newer branch history. If the branch advanced during marker
creation, the ordinary push fails visibly rather than overwriting that history.
An already-existing marker already satisfies the barrier.

After the marker attempt, the `scout` job publishes only the recovery outcomes
needed by its dependent follow-up. The separate `cancel_queued` job receives a
token with only `actions: write`, queries the `scout-seen-state` concurrency group,
and requests cancellation for every other queued member. This cancellation is
defense in depth and operator convenience, not the correctness barrier. If its API
lookup or a cancellation request fails while the marker was established, later runs
are still blocked by the marker. If marker establishment itself fails, the final
diagnostic requires all scout runs to remain stopped until the operator establishes
recovery protection and completes recovery.

The follow-up job explicitly fails after the cancellation attempt, so the workflow
still ends red for every persistence-failure recovery path. Workflow-level
concurrency keeps the shared lock held until that follow-up finishes. This makes no
exactly-once delivery claim.

Deliberate recovery is:

1. Stop new dispatches and verify no other scout transaction is active. Cancel any
   queued runs that remain; do not use GitHub's rerun button as recovery.
2. Download the private recovery artifact before expiration when it exists. Record
   its run ID, attempt, action pin, and transaction base SHA privately, preserve its
   original bytes, and validate it with the canonical state parser from the same
   pinned scanner.
3. Inspect the current private default-branch state and history. If another writer
   advanced state, reconcile deliberately against that history; never overwrite a
   newer snapshot blindly or reverse confirmed lifecycle pruning.
4. Restore the artifact snapshot or the explicitly reconciled
   `seen_bounties.json`. If the artifact is unavailable, upload failed, the job was
   cancelled/timed out, or local saving failed after delivery, reconstruct the
   post-delivery state from trustworthy private evidence first.
5. Remove `.scout/recovery-required` **only as an explicit operator recovery
   action**. No scout run removes it automatically.
6. Commit the recovered/reconciled `seen_bounties.json` and marker removal
   deliberately on the current private default branch, recording recovery
   provenance privately. Use a normal push; never force-push the stale transaction.
7. Fetch the remote branch again and verify the intended state is present and the
   recovery marker is absent.
8. Only then allow scans again.

Successful delivery and Git persistence are separate operations. If coverage was
incomplete, the scanner intentionally leaves newly delivered URLs uncommitted; the
exact resulting snapshot cannot manufacture coverage or guarantee deduplication for
those URLs. Likewise a partially successful or ambiguous delivery cannot be repaired
by blindly replaying it. This workflow makes no exactly-once delivery claim.

## Offline validation

With development dependencies installed, run:

```bash
python -m unittest -v tests.test_private_instance_workflow tests.test_workflow_configuration
make quality
```

The tests parse the action/template as YAML, validate execution pins, concurrency,
permissions, credential wiring, recovery conditions/retention, durable-gate ordering,
queued-run cancellation, and Bash syntax. They execute the actual embedded shell
snippets against temporary local Git repositories, covering current-state reads
after queuing, marker-absent admission, marker-present pre-scan failure, causal
transaction classification, missing or symlinked files, unchanged state, byte-exact
persistence, stale heads, fetch/commit/push failures, a race after fetch, exact-state
remote-persistence recovery, reconstruction-mode local-save recovery, marker creation
from current remote history without state overwrite or force push, marker-creation
failure, cancellation failure with the durable gate still blocking later runs, and
explicit operator marker removal. Action checks prove source/import isolation,
quoted paths, ordinary failure propagation, the typed post-delivery local-save
recovery signal, and invalid config failing before state/network activity. They do
not claim a live private artifact upload, live queue cancellation, or operational
delivery was verified.
