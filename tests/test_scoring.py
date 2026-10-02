from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from bountyscout import reporting
from bountyscout import scoring
from bountyscout.types import Candidate, GitHubComment, GitHubIssue, RepositoryMetadata
import bountyscout.app as scout


AMOUNT_RE = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"

CHAIN_LOVE_3969_BODY = """At current main, actionButtons is stored as a JSON array of Markdown links.
Examples include references/offers/mcpservers.csv where Website and Docs can point to the
same destination.

Add a validator rule for actionButtons:
1. Parse the JSON array.
2. Parse each Markdown-link item into label + destination.
3. Normalize trivial URL spelling differences for comparison.
4. Reject duplicate normalized destination URLs inside the same cell.
5. Error output should name the file, row/slug, repeated URL, and colliding labels.

Existing rows can be corrected in bounded follow-up PRs after maintainers decide whether each
repeated destination is redundant or whether a better official docs/product URL exists.
"""

TAILSCALE_19694_BODY = """### What are you trying to do?

The "Comparison to GUI version" section of the [[Tailscaled-on-macOS](https://github.com/tailscale/tailscale/wiki/Tailscaled-on-macOS)](https://github.com/tailscale/tailscale/wiki/Tailscaled-on-macOS) wiki page already mentions that when running `tailscaled`, "MagicDNS works, but you need to set `100.100.100.100` as your DNS server yourself. It doesn't change your DNS config." — but it doesn't explain *how* to do that.

macOS has a little-known feature where files dropped into `/etc/resolver/` configure per-domain DNS resolvers. Each filename is a DNS domain, and the contents tell `mDNSResponder` which nameserver to use for that domain. The following steps are sufficient to make MagicDNS work with `tailscaled` on macOS:

1. Go to **System Settings → Network → Wi-Fi → Details… → DNS** and add `100.100.100.100` to the DNS servers table.
2. Run the following commands in your terminal:

```sh
sudo mkdir -p /etc/resolver
sudo sh -c 'echo "nameserver 100.100.100.100" > /etc/resolver/ts.net'
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```

This tells `mDNSResponder`: "for anything ending in `.ts.net`, ask `100.100.100.100` instead of the default resolver." Without it, `.ts.net` hostnames silently fail — the system asks the default nameserver (e.g. `1.1.1.1`), which has no knowledge of private tailnet nodes.

### How should we solve this?

By adding the above instructions to the "Tailscaled-on-macOS" wiki page, ideally as a short section underneath or expanding the existing MagicDNS bullet point in "Comparison to GUI version".

### What is the impact of not solving this?

Users running `tailscaled` who enable MagicDNS will find that `.ts.net` hostnames don't resolve, with no obvious explanation. Moreover, the note "...but you need to set `100.100.100.100` as your DNS server yourself..." means the user needs to figure out how to do this in MacOS. 

They'll need to dig through issues and forum posts to discover the `/etc/resolver/` workaround. This could facilitate the process for users that would like to try `tailscaled` in MacOS without being advanced users with networking themselves.

### Anything else?

[tailscaled on macOS](https://github.com/tailscale/tailscale/wiki/Tailscaled-on-macOS)
[Three ways to run Tailscale on macOS](https://tailscale.com/docs/concepts/macos-variants)"""


def issue(**overrides: Any) -> GitHubIssue:
    item: dict[str, Any] = {
        "html_url": "https://github.com/example/project/issues/42",
        "title": "Network regression",
        "body": "",
        "labels": [],
        "comments": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    item.update(overrides)
    return cast(GitHubIssue, item)


def repo_meta(**overrides: Any) -> RepositoryMetadata:
    data: dict[str, Any] = {
        "stargazers_count": 5000,
        "language": "Go",
        "pushed_at": datetime.now(timezone.utc).isoformat(),
    }
    data.update(overrides)
    return cast(RepositoryMetadata, data)


class EffortCalibrationTests(unittest.TestCase):
    def test_type_feature_dialect_no_longer_falls_into_quick_bug_bucket(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Add response metadata header",
                body="Expose one additional response metadata option.",
                labels=[{"name": "type/feature"}],
            )
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertIn("feature/enhancement scope", estimate.reasons)

    def test_cross_component_feature_is_large_scope(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Allow separate buckets for chunks and index",
                labels=[{"name": "type/feature"}],
                body=(
                    "Storage configuration currently uses one bucket for chunks and index. "
                    "The compactor needs a separate index bucket while the chunk bucket remains "
                    "locked. The schema and configuration need to support both stores."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertEqual(
            estimate.reasons,
            ("feature spans multiple runtime/configuration components",),
        )

    def test_feature_with_multiple_file_refs_is_cross_component(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Add scoped feature flag",
                labels=[{"name": "type/feature"}],
                body="pkg/a.go pkg/b.go pkg/c.go",
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertIn("multiple runtime/configuration components", estimate.reasons[0])

    def test_compatibility_sensitive_persisted_state_is_large_scope(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Replace legacy member ID digest",
                labels=[{"name": "type/feature"}],
                body=(
                    "A direct change is backward-incompatible with existing deployments. "
                    "Member IDs are persisted in the WAL and snapshots and exchanged during "
                    "peer handshakes, so old clusters must still rejoin and recover."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertIn("backward-compatibility", estimate.reasons[0])

    def test_fenced_diagnostics_do_not_inflate_effort(self) -> None:
        fence = chr(96) * 3
        huge_dump = "x" * 18000
        estimate = scoring.estimate_effort_details(
            issue(
                title="Certain log configuration reliably triggers a segfault",
                body=(
                    "The crash is deterministic. It may be related to an upstream issue.\n"
                    + fence
                    + "\n"
                    + huge_dump
                    + "\n"
                    + fence
                ),
            )
        )
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("upstream/dependency investigation",))

    def test_comment_volume_is_competition_not_effort(self) -> None:
        item = issue(
            title="TCP mode leaks upstream connection",
            body="Deterministic leak: the upstream connection never closes.",
            comments=25,
        )
        self.assertEqual(scoring.estimate_effort(item), "1–3h")
        self.assertEqual(scoring.competition(item), "high")

    def test_stale_closed_pr_history_does_not_inflate_effort(self) -> None:
        item = issue(
            title="Reduce response buffering",
            body="Parse the response more directly to reduce memory use.",
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "The previous PR was closed because the contributor went inactive. "
                    "The implementation itself is straightforward."
                ),
            }
        ]
        estimate = scoring.estimate_effort_details(item, comments)
        self.assertEqual(estimate.bucket, "3–6h")

    def test_prior_implementation_correctness_evidence_can_raise_effort(self) -> None:
        item = issue(
            title="Reduce response buffering",
            body="Parse the response more directly to reduce memory use.",
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "The previous implementation still mishandles cancellation and leaves a "
                    "goroutine running. Add a regression test and an API-level benchmark."
                ),
            }
        ]
        estimate = scoring.estimate_effort_details(item, comments)
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertEqual(
            estimate.reasons,
            ("maintainer-confirmed implementation-history complexity",),
        )

    def test_untrusted_complexity_history_does_not_inflate_effort(self) -> None:
        item = issue(
            title="Reduce response buffering",
            body="Parse the response more directly to reduce memory use.",
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "NONE",
                "body": (
                    "The previous implementation breaks cancellation and needs a regression "
                    "test plus an API-level benchmark."
                ),
            }
        ]
        self.assertEqual(scoring.estimate_effort_details(item, comments).bucket, "3–6h")

    def test_technical_concerns_without_history_context_do_not_inflate_effort(self) -> None:
        item = issue(
            title="Reduce response buffering",
            body="Parse the response more directly to reduce memory use.",
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": "Cancellation needs a regression test and a benchmark.",
            }
        ]
        self.assertEqual(scoring.estimate_effort_details(item, comments).bucket, "3–6h")

    def test_existing_large_scope_stays_large_with_history_comments(self) -> None:
        item = issue(
            title="Architecture rewrite",
            body="Redesign the parser and transport architecture.",
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "The previous implementation also had cancellation and benchmark concerns."
                ),
            }
        ]
        self.assertEqual(scoring.estimate_effort_details(item, comments).bucket, "1d+")

    def test_effort_precedence_broad_feature_over_compatibility(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="FR: replace persisted member identifier",
                body=(
                    "The persisted state format must remain backwards compatible with "
                    "existing deployments."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertEqual(
            estimate.reasons,
            ("explicit broad feature/design scope",),
        )

    def test_effort_precedence_compatibility_over_environment(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Recover persisted state on hardware-specific installs",
                body=(
                    "Existing deployments must stay backwards compatible. "
                    "Reproduction requires a physical device."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertEqual(
            estimate.reasons,
            ("backward-compatibility or persisted-state risk",),
        )

    def test_effort_precedence_environment_over_cross_component_feature(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Add configurable storage layout",
                labels=[{"name": "type/feature"}],
                body=(
                    "The schema, storage configuration, and compactor behavior need updates. "
                    "Reproducing the failure requires a physical device."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertEqual(
            estimate.reasons,
            ("environment/reproduction-heavy investigation",),
        )

    def test_effort_precedence_broader_scope_over_trusted_history(self) -> None:
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "The previous implementation mishandles cancellation and needs a "
                    "regression test plus an API-level benchmark."
                ),
            }
        ]
        estimate = scoring.estimate_effort_details(
            issue(
                title="Add response metadata option",
                body="Expose one additional response metadata option.",
                labels=[{"name": "type/feature"}],
            ),
            comments,
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertEqual(estimate.reasons, ("feature/enhancement scope",))

    def test_effort_precedence_trusted_history_over_concurrency(self) -> None:
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "The previous implementation mishandles cancellation and needs a "
                    "regression test plus an API-level benchmark."
                ),
            }
        ]
        estimate = scoring.estimate_effort_details(
            issue(
                title="Manager data race after shutdown",
                body="The manager can hit a data race after shutdown.",
            ),
            comments,
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertEqual(
            estimate.reasons,
            ("maintainer-confirmed implementation-history complexity",),
        )

    def test_effort_precedence_concurrency_over_upstream_dependency(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Data race while refreshing dependency state",
                body=("The data race may be related to an upstream dependency during refresh."),
            )
        )
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(
            estimate.reasons,
            ("concurrency/lifecycle debugging risk",),
        )

    def test_effort_precedence_localized_todo_over_bounded_signal(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Fix deterministic cache header bug",
                body=(
                    "pkg/cache.go contains a TODO in the request handler. "
                    "The failure is deterministic and produces an incorrect header."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1–3h")
        self.assertEqual(
            estimate.reasons,
            ("localized TODO/code-path change",),
        )

    def test_prometheus_977_uses_verified_maintainer_history(self) -> None:
        item = issue(
            html_url="https://github.com/prometheus/client_golang/issues/977",
            title="Client API: don't read entire response into a buffer before parsing it",
            body=(
                "The API client reads the entire HTTP response into a []byte before decoding "
                "JSON. For larger responses the buffer gets expensive. Parse JSON from the "
                "response body as it comes in. Handling timeouts may be more complicated."
            ),
            labels=[
                {"name": "help wanted"},
                {"name": "low hanging fruit"},
                {"name": "keep-open"},
            ],
            comments=11,
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "Right now I don't expect to start on this. It's quite a big job. "
                    "Code-generating the API may help with such structural changes."
                ),
            },
            {
                "author_association": "MEMBER",
                "body": (
                    "We could introduce a new method, deprecate the older methods, and remove "
                    "them with v2 to preserve compatibility for external consumers."
                ),
            },
            {
                "author_association": "MEMBER",
                "body": (
                    "The first two JSON parser passes could be merged without major change, "
                    "but nested calls need the code rewritten in streaming style."
                ),
            },
            {
                "author_association": "NONE",
                "body": (
                    "I would keep httpClient.Do untouched and merge the two JSON unmarshal "
                    "passes into a single decode."
                ),
            },
        ]

        self.assertEqual(scoring.estimate_effort_details(item).bucket, "3–6h")
        estimate = scoring.estimate_effort_details(item, comments)
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertEqual(
            estimate.reasons,
            ("maintainer-confirmed implementation-history complexity",),
        )

    def test_strategic_candidate_uses_verified_comments_for_effort(self) -> None:
        item = issue(
            title="Reduce response buffering",
            body="Parse the response more directly to reduce memory use.",
        )
        comments: list[GitHubComment] = [
            {
                "author_association": "MEMBER",
                "body": (
                    "The previous implementation mishandles cancellation and needs a "
                    "regression test plus an API-level benchmark."
                ),
            }
        ]
        preview = scoring.build_candidate(
            item,
            "strategic",
            None,
            repo_meta(),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        verified = scoring.build_candidate(
            item,
            "strategic",
            None,
            repo_meta(),
            None,
            comments,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(preview["effort"], "3–6h")
        self.assertEqual(verified["effort"], "6–12h")

    def test_concurrency_bug_has_debugging_floor(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Manager logs after shutdown",
                body=(
                    "The manager continues work after Start returns and can trigger a data race "
                    "when a test writer has already closed."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("concurrency/lifecycle debugging risk",))

    def test_localized_todo_can_be_quick_without_generic_fix_keyword(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Refactor writeHeaders confirmation status",
                body=(
                    "exp/api/remote_headers.go contains a TODO in the writeHeaders method. "
                    "The method can use the confirmed state instead of parsing message type."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1–3h")
        self.assertEqual(estimate.reasons, ("localized TODO/code-path change",))

    def test_cross_service_generated_documentation_is_not_bounded_docs(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Fragmented gRPC services documentation",
                labels=[{"name": "kind/feature"}],
                body=(
                    "Documentation is fragmented across proto files and the website. "
                    "We want rich documentation for all gRPC services in one place. "
                    "These could potentially be generated docs and hosted on the website."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertEqual(
            estimate.reasons,
            ("cross-service documentation/generation scope",),
        )

    def test_docs_microfix_and_broader_docs_are_distinct(self) -> None:
        micro = issue(title="README typo", body="Fix spelling in the README.")
        broader = issue(
            title="docs: explain controller behavior",
            body="Document controller lifecycle and operational tradeoffs.",
        )
        self.assertEqual(scoring.estimate_effort(micro), "<1h")
        self.assertEqual(scoring.estimate_effort(broader), "1–3h")

    def test_documentation_feature_prefix_does_not_force_large_scope(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="FR: [documentation] explain existing DNS setup",
                body="Add the exact resolver setup steps to the existing documentation.",
                labels=[{"name": "fr"}],
            )
        )
        self.assertEqual(estimate.bucket, "1–3h")
        self.assertEqual(estimate.reasons, ("bounded documentation change",))

    def test_tailscale_19694_is_bounded_documentation(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title=(
                    "FR: [documentation] Explain how to configure MagicDNS manually for "
                    'tailscaled on macOS in the "Tailscaled-on-macOS"'
                ),
                body=TAILSCALE_19694_BODY,
                labels=[{"name": "fr"}],
            )
        )
        self.assertEqual(estimate.bucket, "1–3h")
        self.assertEqual(estimate.reasons, ("bounded documentation change",))

    def test_feature_request_documentation_title_can_remain_bounded(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(
                issue(
                    title="Feature request: update documentation for resolver setup",
                    body="Document the existing configuration and example commands.",
                )
            ),
            "1–3h",
        )

    def test_normal_feature_request_still_triggers_large_scope(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(
                issue(
                    title="FR: Add connection pool support",
                    body="Add connection pooling and lifecycle management to the client.",
                )
            ),
            "1d+",
        )

    def test_documentation_feature_with_architecture_scope_remains_large(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(
                issue(
                    title="FR: [documentation] redesign storage architecture guide",
                    body=(
                        "The work requires an architecture redesign spanning schema, storage, "
                        "configuration, and protocol behavior before the docs can be updated."
                    ),
                )
            ),
            "1d+",
        )

    def test_documentation_feature_with_runtime_parser_scope_remains_large(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(
                issue(
                    title="FR: [documentation] document configuration validation",
                    body=(
                        "Update the guide and implement parser logic plus runtime validation "
                        "behavior so the documented configuration is enforced."
                    ),
                )
            ),
            "1d+",
        )

    def test_chain_love_3969_is_not_a_documentation_microfix(self) -> None:
        item = issue(
            title="[DBIP] Reject duplicate actionButtons destinations within the same cell",
            body=CHAIN_LOVE_3969_BODY,
        )
        self.assertFalse(scoring.documentation_microfix(item))
        estimate = scoring.estimate_effort_details(item)
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("moderate implementation scope",))

    def test_incidental_docs_path_does_not_make_implementation_a_microfix(self) -> None:
        item = issue(
            title="Reject duplicate action button destinations",
            body=(
                "Update the validator for actionButtons. One example is docs/generated/table.md, "
                "but the task is to reject duplicate destinations at validation time."
            ),
        )
        self.assertFalse(scoring.documentation_microfix(item))
        self.assertNotEqual(scoring.estimate_effort(item), "<1h")

    def test_spelling_differences_inside_implementation_prose_do_not_trigger_microfix(self) -> None:
        item = issue(
            title="Normalize action button destinations",
            body=(
                "Parse the JSON array and normalize URL spelling differences before comparing "
                "destinations. Emit a structured validation error for duplicates."
            ),
        )
        self.assertFalse(scoring.documentation_microfix(item))
        self.assertNotEqual(scoring.estimate_effort(item), "<1h")

    def test_legitimate_tiny_documentation_fixes_remain_microfixes(self) -> None:
        cases = (
            issue(title="Fix typo in README"),
            issue(title="Correct spelling in docs"),
            issue(title="Fix broken documentation link"),
            issue(title="Repair broken image in documentation"),
        )
        for item in cases:
            with self.subTest(title=item["title"]):
                self.assertTrue(scoring.documentation_microfix(item))
                self.assertEqual(scoring.estimate_effort(item), "<1h")

    def test_docs_task_with_validator_or_parser_scope_is_not_a_microfix(self) -> None:
        item = issue(
            title="docs: fix spelling in actionButtons guide",
            body=(
                "Also add validator logic, parse the JSON payload, and emit structured validation "
                "errors so the documented rule is enforced at runtime."
            ),
        )
        self.assertFalse(scoring.documentation_microfix(item))
        self.assertNotEqual(scoring.estimate_effort(item), "<1h")

    def test_generic_clipboard_api_does_not_count_as_infrastructure_domain_fit(self) -> None:
        ui = scoring.build_candidate(
            issue(
                title="Show Copied only after clipboard write succeeds",
                body=(
                    "navigator.clipboard.writeText may reject when the clipboard API "
                    "is unavailable. Keep the UI usable and add a Vitest regression."
                ),
                labels=[{"name": "bug"}, {"name": "good first issue"}],
            ),
            "strategic",
            None,
            repo_meta(stargazers_count=18, language="TypeScript"),
            "guide",
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertNotIn("target infrastructure/domain fit", ui["career_reasons"])

        backend = scoring.build_candidate(
            issue(
                title="Validate REST API endpoint input",
                body="The REST API endpoint should reject malformed addresses.",
            ),
            "strategic",
            None,
            repo_meta(stargazers_count=18, language="TypeScript"),
            "guide",
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("target infrastructure/domain fit", backend["career_reasons"])

    def test_architecture_environment_metadata_is_not_design_scope(self) -> None:
        cloudflared = issue(
            title="QUIC Hijack() skips the status-written check that HTTP/2 enforces",
            body=(
                "HTTP/2 refuses Hijack when status has not been written, but QUIC does not. "
                "Both transports should enforce the same precondition. "
                "OS: Linux. Architecture: AMD64. Version: 2026.9.1."
            ),
        )
        estimate = scoring.estimate_effort_details(cloudflared)
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("moderate implementation scope",))

    def test_existing_broad_scope_signals_remain_conservative(self) -> None:
        cases = (
            (issue(title="Architecture rewrite"), "1d+"),
            (
                issue(
                    title="Intermittent ENI race",
                    body="We haven't been able to reproduce on demand; low-probability race.",
                ),
                "1d+",
            ),
            (
                issue(
                    title="Broad logger cleanup",
                    body="a.go b.go c.go d.go",
                ),
                "6–12h",
            ),
            (
                issue(
                    title="Watcher backlog",
                    body="Suggested fixes\n- first\n- second\n- third",
                ),
                "6–12h",
            ),
            (issue(title="Ordinary bug", body="normal report"), "3–6h"),
        )
        for item, expected in cases:
            with self.subTest(title=item["title"]):
                self.assertEqual(scoring.estimate_effort(item), expected)

    def test_feature_request_and_environment_heavy_signals_remain_large(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(issue(title="FR: Support ExternalName", body="small request")),
            "1d+",
        )
        self.assertEqual(
            scoring.estimate_effort(
                issue(
                    title="Android DNS regression",
                    body="dual SIM physical device reproduction",
                )
            ),
            "1d+",
        )

    def test_large_prose_is_distinct_from_large_fenced_dump(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(issue(title="Large design", body="a" * 13001)),
            "1d+",
        )
        self.assertEqual(
            scoring.estimate_effort(issue(title="Broad bug", body="a" * 7000)),
            "6–12h",
        )


class CompetitionVolumeTests(unittest.TestCase):
    @staticmethod
    def controller_runtime_3238_comments() -> list[GitHubComment]:
        comments: list[GitHubComment] = [
            {
                "body": "Not sure readiness should depend on metrics; the webhook server has a checker.",
                "user": {"login": "sbueringer"},
            },
            {
                "body": "Maybe a similar metrics checker is useful. What do maintainers think?",
                "user": {"login": "sbueringer"},
            },
        ]
        for lifecycle in ("stale", "rotten", "stale", "stale", "rotten"):
            comments.append(
                {
                    "body": (
                        "This bot triages un-triaged issues after periods of inactivity.\n"
                        f"/lifecycle {lifecycle}"
                    ),
                    "user": {"login": "k8s-triage-robot"},
                }
            )
        for command in (
            "/remove-lifecycle stale",
            "/remove-lifecycle rotten",
            "/remove-lifecycle stale",
            "/remove-lifecycle rotten",
            "/remove-lifecycle stale",
            "/remove-lifecycle rotten",
            "/remove-lifecycle rotten\n/lifecycle frozen",
        ):
            comments.append(
                {
                    "body": command,
                    "user": {"login": "camilamacedo86"},
                }
            )
        comments.append(
            {
                "body": "Could we please add the frozen label to keep this issue open?",
                "user": {"login": "camilamacedo86"},
            }
        )
        return comments

    def test_controller_runtime_3238_lifecycle_churn_is_not_high_competition(self) -> None:
        comments = self.controller_runtime_3238_comments()
        self.assertEqual(len(comments), 15)
        self.assertEqual(scoring.competition(issue(comments=15), comments), "low")

        candidate = scoring.build_candidate(
            issue(
                title="Add readiness check for metrics server",
                body="Metrics readiness on Kubernetes 1.33 may need a checker.",
                comments=15,
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            comments,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(candidate["competition"], "low")
        self.assertIn("low competition bonus", candidate["priority_reasons"])

    def test_kubernetes_lifecycle_bot_notices_are_ignored(self) -> None:
        for login in ("k8s-triage-robot", "k8s-ci-robot"):
            with self.subTest(login=login):
                self.assertFalse(
                    scoring.comment_contributes_to_competition(
                        {
                            "body": (
                                "This bot triages issues after 90d of inactivity. /lifecycle stale"
                            ),
                            "user": {"login": login},
                        }
                    )
                )

    def test_lifecycle_and_label_admin_commands_are_ignored(self) -> None:
        for body in (
            "/remove-lifecycle stale",
            "/remove-lifecycle rotten",
            "/lifecycle stale",
            "/lifecycle rotten",
            "/lifecycle frozen",
            "/label lifecycle/frozen",
            "/remove-label lifecycle/stale",
            "/remove-lifecycle rotten\n/lifecycle frozen",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    scoring.comment_contributes_to_competition(
                        {"body": body, "user": {"login": "human-maintainer"}}
                    )
                )

    def test_substantive_human_and_bot_implementation_evidence_still_counts(self) -> None:
        self.assertTrue(
            scoring.comment_contributes_to_competition(
                {
                    "body": "I tested this approach and think the server should expose StartedChecker.",
                    "user": {"login": "human-dev"},
                }
            )
        )
        self.assertTrue(
            scoring.comment_contributes_to_competition(
                {
                    "body": "Implementation PR #123 is ready for review.",
                    "user": {"login": "github-actions[bot]"},
                }
            )
        )

    def test_multiple_substantive_comments_still_reach_medium_and_high(self) -> None:
        medium: list[GitHubComment] = [
            {"body": f"Implementation discussion {index}", "user": {"login": f"dev-{index}"}}
            for index in range(4)
        ]
        high: list[GitHubComment] = [
            {"body": f"Implementation discussion {index}", "user": {"login": f"dev-{index}"}}
            for index in range(9)
        ]
        self.assertEqual(scoring.competition(issue(comments=4), medium), "medium")
        self.assertEqual(scoring.competition(issue(comments=9), high), "high")

    def test_raw_count_and_missing_metadata_fallback_remain_conservative(self) -> None:
        self.assertEqual(scoring.competition(issue(comments=15)), "high")
        self.assertEqual(scoring.competition(issue(comments=4), [{}, {}, {}, {}]), "medium")

    def test_paid_candidate_keeps_raw_comment_competition(self) -> None:
        candidate = scoring.build_candidate(
            issue(title="Paid task", comments=15),
            "paid",
            "confirmed bounty platform feed: $25",
            repo_meta(),
            None,
            self.controller_runtime_3238_comments(),
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(candidate["competition"], "high")

    def test_active_claim_still_rejects_even_when_lifecycle_noise_is_filtered(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        comments: list[GitHubComment] = [
            {
                "body": "This bot triages issues after inactivity. /lifecycle stale",
                "updated_at": recent,
                "user": {"login": "k8s-triage-robot"},
            },
            {
                "body": "I'm working on this now and will open a PR with tests.",
                "updated_at": recent,
                "user": {"login": "dev"},
            },
        ]
        self.assertEqual(scoring.competition(issue(comments=2), comments), "low")
        self.assertEqual(
            scout.strategic_competition_reason(issue(comments=2), "t", comments),
            "active claim by @dev",
        )


class ScoringRegressionTests(unittest.TestCase):
    def test_scoring_and_report_expose_effort_basis(self) -> None:
        item = issue(
            title="TCP mode leaks upstream connection",
            body="Deterministic leak: the upstream connection never closes.",
        )
        details = scoring.estimate_effort_details(item)
        self.assertEqual(details.bucket, "1–3h")

        result = scoring.build_candidate(
            item,
            "strategic",
            None,
            repo_meta(),
            "guide",
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        rendered = reporting.markdown_candidate(result, 1)
        self.assertIn("**Priority score:**", rendered)
        self.assertIn("**Effort basis:** bounded deterministic bug signal", rendered)
        self.assertIn(
            "**Priority basis:** 1–3h execution bonus, no visible competition bonus",
            rendered,
        )

    def test_candidate_exposes_effort_reasons(self) -> None:
        result = scoring.build_candidate(
            issue(
                title="TCP mode leaks upstream connection",
                body="Deterministic leak: the upstream connection never closes.",
            ),
            "strategic",
            None,
            repo_meta(),
            "guide",
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(result["effort"], "1–3h")
        self.assertEqual(
            result["effort_reasons"],
            ["bounded deterministic bug signal"],
        )
        self.assertGreater(result["priority_score"], result["career_score"])
        self.assertEqual(
            result["priority_reasons"],
            ["1–3h execution bonus", "no visible competition bonus"],
        )

    def test_strategic_priority_favors_execution_fit_when_career_is_nearly_equal(self) -> None:
        containerd_priority, containerd_reasons = scoring.strategic_priority_score(
            80,
            "1–3h",
            "none",
        )
        terraform_priority, terraform_reasons = scoring.strategic_priority_score(
            81,
            "3–6h",
            "medium",
        )

        self.assertEqual(containerd_priority, 93)
        self.assertEqual(terraform_priority, 79)
        self.assertGreater(containerd_priority, terraform_priority)
        self.assertEqual(
            containerd_reasons,
            ["1–3h execution bonus", "no visible competition bonus"],
        )
        self.assertEqual(
            terraform_reasons,
            ["3–6h execution bonus", "medium competition penalty"],
        )

    def test_strategic_priority_still_allows_large_career_gap_to_win(self) -> None:
        high_value, _ = scoring.strategic_priority_score(95, "6–12h", "low")
        easy_but_weaker, _ = scoring.strategic_priority_score(70, "1–3h", "none")
        self.assertGreater(high_value, easy_but_weaker)

    def test_strategic_priority_caps_extreme_adjustments(self) -> None:
        self.assertEqual(scoring.strategic_priority_score(100, "<1h", "none")[0], 100)
        self.assertEqual(scoring.strategic_priority_score(5, "1d+", "high")[0], 0)

    def test_paid_expected_value_uses_estimated_effort(self) -> None:
        result = scoring.build_candidate(
            issue(title="README typo", body="Fix spelling."),
            "paid",
            "confirmed bounty platform feed: $90",
            repo_meta(stargazers_count=100),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(result["effort"], "<1h")
        self.assertEqual(result["reward"], "$90")
        self.assertEqual(result["expected_hourly"], 120.0)
        self.assertGreater(result["cash_score"], 0)

    def test_strategic_freshness_and_maintainer_activity_still_score(self) -> None:
        now = datetime.now(timezone.utc)
        result = scoring.build_candidate(
            issue(
                title="Network regression",
                body="network regression",
                comments=1,
                created_at=(now - timedelta(days=900)).isoformat(),
                updated_at=(now - timedelta(days=120)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            [
                {
                    "created_at": (now - timedelta(days=10)).isoformat(),
                    "author_association": "MEMBER",
                }
            ],
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("recent maintainer activity", result["career_reasons"])
        self.assertNotIn("stale inactive backlog penalty", result["career_reasons"])

    def test_bot_only_activity_does_not_revive_old_issue(self) -> None:
        now = datetime.now(timezone.utc)
        bot_time = now - timedelta(days=12)
        result = scoring.build_candidate(
            issue(
                title='Make "Bump etcd Version in Kubernetes" part of the release process',
                body="Release process enhancement.",
                created_at=(now - timedelta(days=540)).isoformat(),
                updated_at=bot_time.isoformat(),
                comments=2,
                labels=[{"name": "stale"}],
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            [
                {
                    "created_at": (now - timedelta(days=525)).isoformat(),
                    "author_association": "MEMBER",
                    "user": {"login": "human-maintainer"},
                },
                {
                    "created_at": bot_time.isoformat(),
                    "updated_at": bot_time.isoformat(),
                    "author_association": "CONTRIBUTOR",
                    "user": {"login": "github-actions[bot]"},
                    "body": "This issue has been automatically marked as stale.",
                },
                {
                    "created_at": (bot_time - timedelta(days=30)).isoformat(),
                    "author_association": "CONTRIBUTOR",
                    "user": {"login": "stale[bot]"},
                    "body": "Older automated stale reminder.",
                },
            ],
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertNotIn("issue active in last 14d", result["career_reasons"])
        self.assertNotIn("recent active discussion", result["career_reasons"])
        self.assertIn("older inactive backlog penalty", result["career_reasons"])

    def test_inactive_old_issue_penalty_remains(self) -> None:
        now = datetime.now(timezone.utc)
        result = scoring.build_candidate(
            issue(
                title="Network bug",
                body="network regression",
                created_at=(now - timedelta(days=900)).isoformat(),
                updated_at=(now - timedelta(days=500)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("stale inactive backlog penalty", result["career_reasons"])

    def test_usd_like_amount_from_signal_preserves_behavior(self) -> None:
        cases: list[tuple[str | None, float | None]] = [
            ("reward $1", 1.0),
            ("reward $25", 25.0),
            ("reward $1,234.50", 1234.5),
            ("reward 25 USD", 25.0),
            ("reward 25 usd", 25.0),
            ("reward 25 USDC", 25.0),
            ("reward 25 USDT", 25.0),
            ("reward €25", None),
            (None, None),
        ]
        for signal, expected in cases:
            with self.subTest(signal=signal):
                self.assertEqual(scoring.usd_like_amount_from_signal(signal), expected)

    def test_payment_and_activity_helpers_preserve_public_behavior(self) -> None:
        self.assertEqual(scoring.payment_confidence(None), 0)
        self.assertEqual(
            scoring.payment_confidence("confirmed bounty platform feed"),
            100,
        )
        self.assertEqual(
            scoring.reward_text("confirmed bounty platform: $25", AMOUNT_RE),
            "$25",
        )
        self.assertIsNone(scoring.reward_text(None, AMOUNT_RE))
        self.assertEqual(scoring.effort_hours("6–12h"), 9.0)

        missing = scoring.repo_activity({"pushed_at": None})
        self.assertEqual(missing, "unknown")

    def test_owner_module_covers_effort_payment_and_activity_buckets(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Android split tunnel bug",
                body="### Steps to reproduce\n\n_No response_",
                labels=["OS-android"],
                comments=8,
            )
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertIn("platform-specific reproduction is missing", estimate.reasons)

        cases = [
            (None, 0),
            ("confirmed bounty platform feed (Opire)", 100),
            ("explicit bounty command: $1", 100),
            ("explicit /reward comment: $1", 98),
            ("explicit /bounty comment: $1", 98),
            ("bounty labels: $1", 95),
            ("named bounty platform + funding language", 90),
            ("payment term + amount: $1", 85),
        ]
        for signal, expected in cases:
            with self.subTest(signal=signal):
                self.assertEqual(scoring.payment_confidence(signal), expected)

        now = datetime.now(timezone.utc)
        self.assertEqual(scoring.repo_activity({}), "unknown")
        for days, prefix in (
            (2, "active in last 7d"),
            (20, "active in last 30d"),
            (60, "active in last 90d"),
            (120, "last push"),
        ):
            with self.subTest(days=days):
                activity = scoring.repo_activity(
                    {"pushed_at": (now - timedelta(days=days)).isoformat()}
                )
                self.assertTrue(activity.startswith(prefix))

    def test_owner_module_covers_cash_star_language_and_scope_branches(self) -> None:
        now = datetime.now(timezone.utc)
        inactive = (now - timedelta(days=120)).isoformat()

        non_usd = scoring.build_candidate(
            issue(title="Feature", body="", comments=4),
            "paid",
            "confirmed bounty platform feed (X): €25",
            repo_meta(stargazers_count=500, pushed_at=inactive, language="Rust"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertIsNone(non_usd["expected_hourly"])
        self.assertIn("reward not USD-comparable", non_usd["cash_reasons"])

        low_star = scoring.build_candidate(
            issue(title="Feature", body="plain", comments=4),
            "strategic",
            None,
            repo_meta(stargazers_count=50, pushed_at=inactive, language="Rust"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(low_star["stars"], 50)

        active_go = scoring.build_candidate(
            issue(title="Feature", body="network api regression", comments=0),
            "paid",
            "payment term + amount: $25",
            repo_meta(stargazers_count=1500, language="Go"),
            None,
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("established repo", active_go["cash_reasons"])
        self.assertIn("Go codebase", active_go["career_reasons"])
        self.assertIn("target repo bonus", active_go["career_reasons"])

        broad = scoring.build_candidate(
            issue(title="Architecture redesign", body="plain", comments=0),
            "strategic",
            None,
            repo_meta(stargazers_count=50, pushed_at=inactive, language="HCL"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("large-scope penalty", broad["career_reasons"])

    def test_owner_module_covers_clarity_and_documentation_cap_branches(self) -> None:
        detailed = scoring.build_candidate(
            issue(
                title="Network regression",
                body=(
                    "Root cause is in pkg/a.go. Steps to reproduce: run it. "
                    "Suggested fix: update pkg/b.go."
                ),
                comments=0,
            ),
            "strategic",
            None,
            repo_meta(language="TypeScript"),
            "guide",
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("clear implementation/reproduction detail", detailed["career_reasons"])

        partial = scoring.build_candidate(
            issue(title="Bug", body="Code path: pkg/a.go", comments=0),
            "strategic",
            None,
            repo_meta(language="Ruby"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("implementation detail available", partial["career_reasons"])

        docs = scoring.build_candidate(
            issue(
                title="README typo",
                body="Fix spelling in docs/guide.md.",
                comments=0,
            ),
            "strategic",
            None,
            repo_meta(language="Rust"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("documentation-only micro-fix cap", docs["career_reasons"])
        self.assertLessEqual(docs["career_score"], 45)

    def test_owner_module_covers_strategic_freshness_activity_branches(self) -> None:
        now = datetime.now(timezone.utc)

        def build(updated_days: int, comments: list[GitHubComment] | None = None) -> Candidate:
            return scoring.build_candidate(
                issue(
                    title="Network bug",
                    body="network regression",
                    comments=len(comments or []),
                    created_at=(now - timedelta(days=500)).isoformat(),
                    updated_at=(now - timedelta(days=updated_days)).isoformat(),
                ),
                "strategic",
                None,
                repo_meta(),
                None,
                comments,
                target_repos={"example/project"},
                amount_pattern=AMOUNT_RE,
            )

        fresh = build(3)
        middle = build(20)
        older = build(120)
        discussion = build(
            120,
            [
                {
                    "body": "Reproduced.",
                    "created_at": (now - timedelta(days=5)).isoformat(),
                    "author_association": "NONE",
                    "user": {"login": "dev"},
                }
            ],
        )
        maintained = build(
            120,
            [
                {
                    "created_at": (now - timedelta(days=20)).isoformat(),
                    "author_association": "MEMBER",
                    "user": {"login": "maintainer"},
                },
                {
                    "created_at": (now - timedelta(days=5)).isoformat(),
                    "author_association": "MEMBER",
                    "user": {"login": "maintainer"},
                },
            ],
        )

        self.assertIn("issue active in last 14d", fresh["career_reasons"])
        self.assertIn("issue active in last 60d", middle["career_reasons"])
        self.assertIn("issue active in last 180d", older["career_reasons"])
        self.assertIn("recent active discussion", discussion["career_reasons"])
        self.assertIn("recent maintainer activity", maintained["career_reasons"])

        inactive = scoring.build_candidate(
            issue(
                title="Network bug",
                body="network regression",
                comments=0,
                created_at=(now - timedelta(days=500)).isoformat(),
                updated_at=(now - timedelta(days=300)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("older inactive backlog penalty", inactive["career_reasons"])

    def test_owner_module_covers_remaining_branch_edges(self) -> None:
        now = datetime.now(timezone.utc)
        inactive = (now - timedelta(days=120)).isoformat()

        paid_low_star = scoring.build_candidate(
            issue(title="Feature", body="plain", comments=0),
            "paid",
            "payment term + amount: $25",
            repo_meta(stargazers_count=50, pushed_at=inactive, language="Rust"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(paid_low_star["stars"], 50)

        zero_star = scoring.build_candidate(
            issue(title="Feature", body="plain", comments=0),
            "strategic",
            None,
            repo_meta(stargazers_count=0, pushed_at=inactive, language="Rust"),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(zero_star["stars"], 0)

        older_second_comment = scoring.build_candidate(
            issue(
                title="Network bug",
                body="network regression",
                comments=2,
                created_at=(now - timedelta(days=500)).isoformat(),
                updated_at=(now - timedelta(days=120)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            [
                {
                    "created_at": (now - timedelta(days=5)).isoformat(),
                    "author_association": "MEMBER",
                    "user": {"login": "maintainer"},
                },
                {
                    "created_at": (now - timedelta(days=20)).isoformat(),
                    "author_association": "MEMBER",
                    "user": {"login": "maintainer"},
                },
            ],
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn(
            "recent maintainer activity",
            older_second_comment["career_reasons"],
        )

        missing_updated = scoring.build_candidate(
            issue(
                title="Network bug",
                body="network regression",
                comments=1,
                created_at=None,
                updated_at=None,
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            [{"body": "old", "created_at": "not-a-date"}],
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertNotIn(
            "issue active in last",
            " ".join(missing_updated["career_reasons"]),
        )

        young_inactive = scoring.build_candidate(
            issue(
                title="Network bug",
                body="network regression",
                comments=0,
                created_at=(now - timedelta(days=300)).isoformat(),
                updated_at=(now - timedelta(days=300)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertNotIn(
            "inactive backlog penalty",
            " ".join(young_inactive["career_reasons"]),
        )


if __name__ == "__main__":
    unittest.main()
