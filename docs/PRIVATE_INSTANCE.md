# Private instance execution template

Slice 5 provides the public [composite action](../action.yml) and a generic,
manual-only [private workflow template](../examples/private-instance/scout.yml).
These are distribution assets; they do not create an instance or migrate history.
Slice 6 operational acceptance is verified privately. Slice 7 retires the upstream
production workflow and requires configuration for local scans: `scout.toml` in the
working directory or explicit `--config PATH`. See the
[migration tracker](PRIVATE_DEPLOYMENT_MIGRATION.md).

## Ownership and pins

An independent **private** instance repository owns its `.github/workflows/scout.yml`,
triggers, any future schedules, concurrency, secrets, `scout.toml`,
`seen_bounties.json`, Git state/history, delivery configuration, and scanner pin.
Upstream owns scanner code, development CI, reusable execution, and generic examples.
Forking is optional for scanner-code customization; use a deliberate full SHA from
upstream or a customized fork. Never run an upstream persistent workflow that reads
private instance state remotely.

The template pins checkout, Python setup, artifact upload, and scanner execution
to full commit SHAs. Its scanner SHA selects the initial Slice 5 action implementation
commit. Before adopting or upgrading it, review that commit and its checks. Since
PR #40 is merged, an operator may deliberately replace it with the reviewed merge
commit's full SHA. A release can identify a SHA, but branches and moving tags must
not silently upgrade the scanner. Changing the pin is an instance-owned change.

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
empty example for migration state. The template rejects missing files and symlinks;
the scanner validates config and existing state before discovery or delivery.
No setup or personal migration is performed by Slice 5.

## Slice 6 private acceptance record

PR #40 merged the Slice 5 distribution assets. Slice 6 must establish the personal
instance independently; recovery or offline checks alone do not complete it.
Resolve the instance repository, personal preferences, delivery destinations, and
secure provisioning of the separate report credential before dependent actions.
Continue independent historical recovery checks while these details are pending.
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
| Controlled transaction checks | Observed serialization and current-state reads after admission; deliberately stale write rejected without remote overwrite; exact recovery artifact uploaded privately with three-day retention, downloaded, parsed, and restored or reconciled before another scan |

Use controlled checks without report delivery for serialization, stale writes, and
artifact handling. Record which checks exercised GitHub Actions and which were
offline. A controlled artifact check proves preservation and restoration mechanics;
the separate delivery/persistence/deduplication gates still require real scan
evidence. Do not add fabricated opportunity entries to the production state for a
check or treat incomplete coverage as permission to advance it.

If a manual scan delivers but persistence fails, stop dispatches and cancel queued
writers immediately, then follow [recovery before rerunning](#recover-before-rerunning).
Do not dispatch the deduplication scan until remote restoration is verified.
Missing recovery evidence, incomplete coverage, ambiguous delivery, and candidates
not rechecked remain explicit gaps rather than successful acceptance claims.

Public tracker/PR updates contain only non-sensitive status and public source/check
identifiers. Keep personal repository identities, preferences, secrets, state
contents/counts, opportunity URLs, run IDs, recovery hashes, and operational logs
private. These gates are verified for the migrated instance and PR #41 is merged;
operational evidence remains private. Future instances must prove their own gates.

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

The template sets workflow-level `permissions: {}` and grants only `contents: write`
to the scan transaction job. Its normal `github.token` handles public scanner REST
access and same-instance state pushes. It grants no Issues write, Actions write,
or cross-repository persistence credential. Checkout retains only this instance
credential for the normal Git push.

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
private/default-branch checks, checkout, state reads, discovery/verification,
delivery, local state save, remote persistence, recovery upload, and final failure.
Do not move state reads into an earlier job or key concurrency by workflow/ref.
The queue is bounded at 100 pending runs; queue overflow cancels additional runs.
Waiting order follows admission to the group rather than dispatch order.
[GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

After serialization begins, checkout selects the current private default branch,
and the state-read step fetches it again, detaches at that current head, and records
the transaction base SHA. It deliberately avoids the dispatch event's stale SHA.
The action reads this state and runs once. Existing verification, coverage,
aggregate-delivery, and bounded lifecycle maintenance invariants decide whether
the local state changes; the workflow does not reinterpret those decisions.

Persistence fetches the branch without resetting or replacing the resulting state.
It rejects any remote head different from the recorded base, even if only an
unrelated instance file changed. An unchanged state creates no commit. A changed
state alone is committed and pushed normally; a race after fetch fails the ordinary
non-forced push. There is no force push, automatic rebase/merge, reset to an older
snapshot, retry of delivery, or automatic replay. Default-branch rules must permit
the instance bot's normal state commit; a rejected push triggers recovery.

## Recover before rerunning

Any persistence-step failure, including fetch, stale-head, commit, or push failure,
first attempts to upload the exact resulting `seen_bounties.json` as
`scout-state-recovery-<run-id>-<run-attempt>`. The upload runs only in the confirmed
private caller repository, has **three-day retention**, and fails if the file is
missing. It uploads no checkout, config, report, or secret files. This is a recovery
artifact, not the primary state backend. After the upload attempt, the workflow
explicitly fails even when upload succeeded. If upload also fails, its final error
requires reconstruction rather than treating a rerun as safe.

Before rerunning a failed scan transaction:

1. Stop further dispatches and cancel pending runs sharing this state; wait until
   no transaction is active. Concurrency serializes runs but does not block queued
   runs after a failure. Do not use GitHub's rerun button as recovery.
2. Download the recovery artifact privately before expiration. Record its run ID,
   attempt, action pin, and transaction base SHA from the private run. Keep its
   original bytes as the recovery snapshot; validate it with the canonical state
   parser from the same pinned scanner.
3. Inspect current private branch/state history. If another writer advanced state,
   reconcile deliberately against that history, preserving both delivered entries
   and valid lifecycle maintenance. Do not overwrite a newer snapshot blindly or
   union entries in a way that reverses confirmed lifecycle pruning.
4. Commit the recovered state (or the explicitly reconciled snapshot) to the
   current private default branch with a normal push, recording recovery provenance
   privately. Verify remote state contains already delivered opportunities before
   allowing another manual scan. Never force-push the stale transaction commit.
5. If the artifact is unavailable, upload failed, the job was cancelled/timed out,
   or local saving failed after delivery, reconstruct post-delivery state from
   trustworthy private evidence first. An immediate rerun may repeat delivery.

Successful delivery and Git persistence are separate operations. If coverage was
incomplete, the scanner intentionally leaves newly delivered URLs uncommitted;
the exact resulting snapshot cannot manufacture coverage or guarantee deduplication
for those URLs. Likewise a partially successful/ambiguous delivery cannot be
repaired by blindly replaying it. This workflow makes no exactly-once delivery claim.

## Offline validation

With development dependencies installed, run:

```bash
python -m unittest -v tests.test_private_instance_workflow tests.test_workflow_configuration
make quality
```

The tests parse the action/template as YAML, validate execution pins, concurrency,
permissions, credential wiring, recovery conditions/retention, ordering, and Bash
syntax. They execute the actual embedded shell snippets against temporary local
Git repositories, covering current-state reads after queuing, missing/symlinked
files, unchanged state, byte-exact persistence, stale heads, fetch/commit/push
failures, and a race after fetch. Action checks prove source/import isolation,
quoted paths, failure propagation, and invalid config failing before state/network
activity. Recovery checks prove workflow failure follows the upload attempt and
both successful/failed recovery outcomes require operator action. They do not
claim a live private artifact upload or operational delivery was verified.
