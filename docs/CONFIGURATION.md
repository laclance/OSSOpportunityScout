# Scout configuration

Configuration is opt-in during migration. From the scanner source directory, with
Python 3.12 (or a supported CI-tested version) and dependencies installed:

```bash
python opportunity_scout.py --config scout.example.toml --state seen_bounties.json
python opportunity_scout.py --config /path/to/private-instance/scout.toml --state /path/to/private-instance/seen_bounties.json
```

These commands run the scout using separately configured credentials and delivery
channels. Relative paths resolve from the working directory, including the state
path; they do not resolve from the config file's directory.

`--config PATH` explicitly loads a UTF-8 version-1 TOML file before state loading,
discovery, or delivery. Missing, unreadable, malformed, or invalid config fails the
run without falling back to defaults. There is no automatic config-file discovery.
Legacy invocation ignores `scout.toml`, even if it exists or is invalid:

```bash
python opportunity_scout.py
python opportunity_scout.py --state /path/to/private-instance/seen_bounties.json
```

`--state PATH` works independently of `--config` and defaults to
`seen_bounties.json`. Every state read, successful-delivery commit, and complete
quiet-run maintenance write uses this path. Its parent directory must already
exist; the scout does not create directories. Only absent state means a first run.
Malformed, unreadable, or unsupported existing state fails closed. Never replace
historical instance state with the empty public example during migration.

## Version-1 fields

Only `version` is required. Omitted tables and fields use the defaults below;
empty tables are valid. Types are strict: booleans are not integers, numeric
strings and floats do not satisfy integer fields, and arrays require strings.
Unknown keys or tables fail validation, including credentials, delivery settings,
state paths, and scanner correctness controls.

| Field | Default | Valid values and meaning |
| --- | --- | --- |
| `version` | Required | Integer `1` only. |
| `name` | Absent (`None`) | Optional non-empty string. Stored in preferences; currently has no runtime effect on reports, selection, state, or delivery. |
| `lanes.paid` | `true` | Boolean. Enables paid discovery and acceptance of final paid candidates. |
| `lanes.strategic` | `true` | Boolean. Enables strategic discovery and acceptance of final unpaid candidates. |
| `discovery.languages` | `[]` | Array of non-empty strings matching the resolved upstream repository's primary language, case-insensitively. Empty accepts all languages, including unknown. An explicit list excludes absent, null, empty, and `Unknown` metadata, even if `"Unknown"` is listed. |
| `discovery.repositories` | `[]` | Array of `owner/repository` identifiers. Adds strategic sources to the built-in curated list; it is not an allowlist and does not add paid sources. |
| `discovery.exclude_repositories` | `[]` | Array of `owner/repository` identifiers. Case-insensitive exclusions take precedence over targets across both lanes, including resolved upstream repositories. |
| `discovery.global_search` | `true` | Boolean. Enables strategic global searches. `false` retains curated strategic sources and paid discovery when their lanes are enabled. |
| `preferences.effort` | `["<1h", "1–3h", "3–6h", "6–12h", "1d+"]` | Exact membership in the existing final estimate buckets across both lanes. Empty accepts no candidates. Use en dashes (`–`), not hyphens, in range labels. |
| `preferences.min_career_score` | `55` | Integer 0–100, inclusive. Final unpaid candidates need a career score at least this high, regardless of discovery source. |
| `preferences.min_cash_score` | `55` | Integer 0–100, inclusive. Final paid candidates need a cash score at least this high, regardless of discovery source. |
| `preferences.max_results` | `8` | Integer 1–8, inclusive. Caps the final ranked queue; it does not reduce discovery or verification budgets. |

Surrounding whitespace is trimmed from `name` and array entries; whitespace-only
strings are invalid. The parser preserves array order, case, and duplicates.
Repository targets are combined with curated defaults in first-seen order and
deduplicated case-insensitively. Language and effort duplicates do not change
membership. There are no parser-imposed string-length or array-size bounds.

Repository identifiers are not URLs: owners start and end with an ASCII letter or
digit and may contain internal hyphens; repository names use ASCII letters,
digits, underscores, dots, or hyphens and cannot be `.` or `..`.

Both lanes may be disabled. Disabled sources make no discovery requests and do
not count as incomplete coverage; bounded state lifecycle maintenance can still
run and may make GitHub requests. An empty effort list is likewise not an offline
mode: discovery and verification still run.

Language filtering uses cached primary-language metadata, not issue text, labels,
or wrapper-repository language, and adds no per-language searches or metadata
reads. Refreshed evidence and already-fetched strategic discussion determine final
effort; preview estimates do not filter acceptance. Refreshed paid/unpaid
classification chooses the lane and score threshold. Eligibility is applied
before strategic repository-slot settlement, URL deduplication, and final queue
truncation. Scoring, ranking, estimation, verification safeguards, and request
budgets remain scanner policy.

## Generic examples

The complete [scout.example.toml](../scout.example.toml) lists the defaults. The
smallest valid file is equivalent to those defaults:

```toml
version = 1
```

For a strategic-only queue in Python or Go, with short final effort estimates and
at most three results:

```toml
version = 1
name = "Short strategic work"

[lanes]
paid = false

[discovery]
languages = ["Python", "Go"]
global_search = false

[preferences]
effort = ["<1h", "1–3h", "3–6h"]
min_career_score = 65
max_results = 3
```

`strategic` remains `true`, and curated sources remain enabled. `name` is only
stored metadata. Copy a chosen example into your private instance's `scout.toml`
and select it explicitly with `--config`.

## Credentials, delivery, and ownership

Preferences contain no credentials or report destinations. Configure scanner
authentication and delivery through the environment as described in
[README](../README.md#github-credentials-and-delivery). `GITHUB_TOKEN` and
`GITHUB_REPOSITORY` never imply permission to publish reports. Host GitHub reports
default off; private reports require their own repository and credential, and
metadata retrieved with that credential must explicitly confirm `private: true`.
The private-report credential is never used for scanner discovery.

The public upstream distributes code, documentation, and generic examples. An
independent private instance owns its actual workflow, configuration/state,
triggers/schedules, concurrency, secrets, delivery configuration, persistence and
history, and scanner pin. A fork is optional for code customization; default
instances consume pinned upstream code directly, while customized instances may
consume a pinned fork. Public workflows must not reach into private repositories
for state. The execution template, historical migration, and mandatory-config
cutover belong to Slices 5, 6, and 7 respectively in the
[migration tracker](PRIVATE_DEPLOYMENT_MIGRATION.md).

## Offline validation

Run from the scanner source directory. This parser-only check validates the
complete example without reading state, scanning, or delivering:

```bash
python -c 'from pathlib import Path; from opportunity_scout.preferences import load_scout_preferences; load_scout_preferences(Path("scout.example.toml")); print("Configuration valid")'
```

To check your chosen file, replace `scout.example.toml` in that check with its
path. Validation confirms the schema, not repository existence, available
opportunities, credentials, or delivery readiness.

The existing offline regressions verify configured and legacy invocation, custom
state paths, and preference composition using fake network and delivery results:

```bash
python opportunity_scout.py --help
python -m unittest tests.test_preferences tests.test_configuration tests.test_languages tests.test_effort_preferences tests.test_score_preferences
```

The scan commands above are invocation examples, not offline validation commands.
