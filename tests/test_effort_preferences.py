from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from opportunity_scout import (
    app,
    delivery,
    github,
    paid_verification,
    preferences,
    run,
    selection,
    state,
)
from opportunity_scout.strategic import discovery
from opportunity_scout.types import Candidate, EffortBucket, GitHubComment, RepositoryMetadata
from tests.helpers import candidate, comment, issue
from tests.test_run import FIXED_TIME, dependencies


EFFORT_REJECTION = "final effort estimate excluded by configuration"
BUCKETS: tuple[EffortBucket, ...] = ("<1h", "1–3h", "3–6h", "6–12h", "1d+")
METADATA: RepositoryMetadata = {
    "language": "Go",
    "stargazers_count": 100000,
    "pushed_at": "2026-10-03T00:00:00Z",
}


class EffortPolicyTests(unittest.TestCase):
    def test_exact_bucket_boundaries_in_both_lanes(self) -> None:
        for final_effort in BUCKETS:
            for allowed in ((), BUCKETS, *((bucket,) for bucket in BUCKETS), ("3–6h", "3–6h")):
                config = preferences.ScoutPreferences(effort=allowed)
                with self.subTest(final=final_effort, allowed=allowed):
                    expected = final_effort in allowed
                    self.assertEqual(selection.effort_accepted(final_effort, allowed), expected)
                    for is_paid in (False, True):
                        self.assertEqual(
                            selection.candidate_rejection(
                                candidate(paid=is_paid, effort=final_effort), config
                            ),
                            None if expected else EFFORT_REJECTION,
                        )
        self.assertEqual(preferences.ScoutPreferences().effort, BUCKETS)


class FinalEstimateTests(unittest.TestCase):
    def test_paid_acceptance_uses_refreshed_estimate_in_both_directions(self) -> None:
        small = issue(body="Bounty $500", comments=0)
        moderate = issue(title="Reduce response buffering", body="Bounty $500", comments=0)
        for source, fresh in ((small, moderate), (moderate, small)):
            for allowed in (("1–3h",), ("3–6h",), ()):
                with (
                    self.subTest(source=source["title"], allowed=allowed),
                    patch.object(app, "platform_paid_refs", return_value={}),
                    patch.object(app, "refresh_issue", return_value=(fresh, None)) as refresh,
                    patch.object(
                        paid_verification, "candidate_rejection_reason", return_value=(None, "$500")
                    ) as paid_checks,
                    patch.object(app, "fetch_repo_metadata", return_value=METADATA) as metadata,
                    patch.object(app, "contribution_guide", return_value=None),
                    redirect_stdout(io.StringIO()),
                ):
                    found, rejects, examples = app.discover_paid(
                        None,
                        set(),
                        {},
                        {},
                        [("paid", {"items": [source]})],
                        scout_preferences=preferences.ScoutPreferences(effort=allowed),
                    )
                final = app.build_candidate(fresh, "paid", "$500", METADATA, None)
                accepted = final["effort"] in allowed
                self.assertEqual(found, [final] if accepted else [])
                self.assertEqual(rejects, {} if accepted else {EFFORT_REJECTION: 1})
                self.assertEqual(len(examples), int(not accepted))
                self.assertEqual(refresh.call_count, 1)
                self.assertEqual(paid_checks.call_count, 1)
                self.assertEqual(metadata.call_count, 1)

    def test_strategic_discussion_changes_effort_after_preview(self) -> None:
        source = issue(
            title="Reduce response buffering",
            body="Parse the response more directly to reduce memory use.",
            labels=["help wanted"],
        )
        comments: list[GitHubComment] = [
            comment(
                author_association="MEMBER",
                body="The previous implementation mishandles cancellation and needs a regression test plus an API-level benchmark.",
            )
        ]
        preview = app.build_candidate(source, "strategic", None, METADATA, None)
        final = app.build_candidate(source, "strategic", None, METADATA, None, comments)
        self.assertEqual(preview["effort"], "3–6h")
        self.assertEqual(final["effort"], "6–12h")
        self.assertGreaterEqual(final["career_score"], app.STRATEGIC_MIN_CAREER_SCORE)
        for allowed in (("3–6h",), ("6–12h",), ()):
            with (
                self.subTest(allowed=allowed),
                patch.object(app, "TARGET_REPOS", []),
                patch.object(app, "refresh_issue", return_value=(source, None)) as refresh,
                patch.object(app, "strategic_preflight_rejection", return_value=None),
                patch.object(app, "strategic_rejection", return_value=None),
                patch.object(
                    github, "issue_comments_checked", return_value=(comments, None)
                ) as checked,
                patch.object(app, "contribution_guide", return_value=None),
                redirect_stdout(io.StringIO()),
            ):
                found, rejects, _, _ = app.discover_strategic(
                    None,
                    set(),
                    set(),
                    {"example/project": METADATA},
                    {},
                    [("global", {"items": [source]})],
                    scout_preferences=preferences.ScoutPreferences(effort=allowed),
                )
            accepted = final["effort"] in allowed
            self.assertEqual(found, [final] if accepted else [])
            self.assertEqual(rejects, {} if accepted else {EFFORT_REJECTION: 1})
            refresh.assert_called_once()
            checked.assert_called_once()

    def test_strategic_refresh_changes_effort_and_paid_classification(self) -> None:
        for final_paid in (False, True):
            source = issue(
                title="Reduce response buffering",
                body="" if final_paid else "Bounty $500",
                comments=0,
                labels=["help wanted"],
            )
            fresh = issue(
                body="Bounty $500" if final_paid else "", comments=0, labels=["help wanted"]
            )
            preview = app.build_candidate(
                source,
                "strategic" if final_paid else "paid",
                None if final_paid else "$500",
                METADATA,
                None,
            )
            self.assertEqual(preview["effort"], "3–6h")
            for allowed in (("1–3h",), ("3–6h",)):
                with (
                    self.subTest(final_paid=final_paid, allowed=allowed),
                    patch.object(app, "TARGET_REPOS", []),
                    patch.object(app, "refresh_issue", return_value=(fresh, None)) as refresh,
                    patch.object(app, "strategic_preflight_rejection", return_value=None),
                    patch.object(app, "strategic_rejection", return_value=None),
                    patch.object(
                        paid_verification, "candidate_rejection_reason", return_value=(None, "$500")
                    ) as paid_checks,
                    patch.object(app, "contribution_guide", return_value=None),
                    redirect_stdout(io.StringIO()),
                ):
                    found, rejects, _, _ = app.discover_strategic(
                        None,
                        set(),
                        set(),
                        {"example/project": METADATA},
                        {},
                        [("global", {"items": [source]})],
                        scout_preferences=preferences.ScoutPreferences(effort=allowed),
                    )
                accepted = allowed == ("1–3h",)
                self.assertEqual(len(found), int(accepted))
                self.assertEqual(rejects, {} if accepted else {EFFORT_REJECTION: 1})
                if accepted:
                    self.assertEqual((found[0]["paid"], found[0]["effort"]), (final_paid, "1–3h"))
                refresh.assert_called_once()
                self.assertEqual(paid_checks.call_count, int(final_paid))

    def test_rejected_estimates_do_not_settle_repository_slots(self) -> None:
        rows = []
        outcomes: list[tuple[Candidate | None, str | None]] = []
        for index in range(7):
            source = issue(html_url=f"https://github.com/example/project/issues/{index + 1}")
            score = 100 - index if index < 6 else 60
            rows.append((score, score, 0, source))
            outcomes.append(
                (
                    candidate(
                        url=source["html_url"],
                        priority_score=score,
                        career_score=score,
                        effort="1d+" if index < 3 else "1–3h",
                        paid=index % 2 == 0,
                    ),
                    None,
                )
            )
        with (
            patch.object(app, "TARGET_REPOS", []),
            patch.object(
                discovery,
                "select_strategic_candidates",
                return_value=discovery.StrategicDiscoverySelection({"example/project": rows}, []),
            ),
            patch.object(app, "strategic_preflight_rejection", return_value=None),
            patch.object(app, "verify", side_effect=outcomes) as verify,
            redirect_stdout(io.StringIO()),
        ):
            found, rejects, _, _ = app.discover_strategic(
                None,
                set(),
                set(),
                {},
                {},
                [],
                scout_preferences=preferences.ScoutPreferences(effort=("1–3h",)),
            )
        self.assertEqual(
            [final["url"] for final in found], [row[3]["html_url"] for row in rows[3:6]]
        )
        self.assertEqual(rejects, {EFFORT_REJECTION: 3})
        self.assertEqual(verify.call_count, 6)


class EffortOrchestrationTests(unittest.TestCase):
    def test_filter_precedes_deduplication_and_queue_limit_with_transaction_guards(self) -> None:
        blocked = candidate(paid=True, priority_score=100, url="duplicate", effort="1d+")
        allowed = candidate(priority_score=80, url="duplicate", effort="1–3h")
        lower = candidate(priority_score=79, url="lower", effort="1–3h")
        for complete, delivered in ((True, True), (False, True), (True, False)):
            with (
                self.subTest(complete=complete, delivered=delivered),
                tempfile.TemporaryDirectory() as directory,
            ):
                state_path = Path(directory, "state.json")
                config = run.RunConfig(
                    None,
                    None,
                    "delivery",
                    "chat",
                    None,
                    state_path=state_path,
                    preferences=preferences.ScoutPreferences(effort=("1–3h",)),
                )
                callbacks = replace(
                    dependencies(),
                    discover_paid=lambda *_args: ([blocked], {}, []),
                    discover_strategic=lambda *_args: (
                        [allowed, lower],
                        {} if complete else {"could not refresh source issue": 1},
                        [],
                        [],
                    ),
                    send_telegram=lambda *_args: delivered,
                )
                with redirect_stdout(io.StringIO()):
                    result = run.run_combined_scan(config, callbacks, FIXED_TIME, report_limit=1)
                self.assertEqual(result.queue, (allowed,))
                self.assertEqual(result.coverage.complete, complete)
                self.assertEqual(result.state_saved, complete and delivered)
                self.assertEqual(
                    state.load_seen_state(state_path).urls(),
                    {"duplicate"} if complete and delivered else set(),
                )

    def test_explicit_effort_and_legacy_defaults_keep_request_budgets(self) -> None:
        paid_source = issue(
            html_url="https://github.com/example/project/issues/1", body="Bounty $500", comments=0
        )
        strategic_source = issue(
            html_url="https://github.com/example/project/issues/2",
            labels=["help wanted"],
            comments=0,
        )
        for authenticated in (False, True):
            for allowed in (None, BUCKETS, ("1–3h",), ("1d+",), ()):
                with (
                    self.subTest(prefetch=authenticated, allowed=allowed),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    config_path = Path(directory, "config.toml")
                    config_path.write_text(
                        f"version = 1\n[preferences]\neffort = {list(allowed or ())!r}\n",
                        encoding="utf-8",
                    )
                    state_path = Path(directory, "state.json")
                    env = {"TELEGRAM_BOT_TOKEN": "delivery", "TELEGRAM_CHAT_ID": "chat"}
                    if authenticated:
                        env.update(GITHUB_TOKEN="scanner", GITHUB_REPOSITORY="host/repo")
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict(os.environ, env, clear=True))
                        stack.enter_context(patch.object(app, "TARGET_REPOS", []))
                        stack.enter_context(patch.object(app, "PAID_DISCOVERY_QUERIES", ["paid-q"]))
                        stack.enter_context(
                            patch.object(app, "STRATEGIC_GLOBAL_QUERIES", ["global-q"])
                        )
                        search = stack.enter_context(
                            patch.object(
                                github,
                                "search_github",
                                side_effect=lambda query, _token, per_page=15: {
                                    "items": [
                                        paid_source if query == "paid-q" else strategic_source
                                    ]
                                },
                            )
                        )
                        stack.enter_context(
                            patch.object(app, "platform_paid_refs", return_value={})
                        )
                        refresh = stack.enter_context(
                            patch.object(
                                app,
                                "refresh_issue",
                                side_effect=lambda source, _token: (source, None),
                            )
                        )
                        stack.enter_context(
                            patch.object(app, "strategic_preflight_rejection", return_value=None)
                        )
                        stack.enter_context(
                            patch.object(app, "strategic_rejection", return_value=None)
                        )
                        paid_checks = stack.enter_context(
                            patch.object(
                                paid_verification,
                                "candidate_rejection_reason",
                                return_value=(None, "$500"),
                            )
                        )
                        metadata = stack.enter_context(
                            patch.object(app, "fetch_repo_metadata", return_value=METADATA)
                        )
                        guide = stack.enter_context(
                            patch.object(app, "contribution_guide", return_value=None)
                        )
                        stack.enter_context(patch.object(app, "sleep"))
                        telegram = stack.enter_context(
                            patch.object(delivery, "send_telegram_notification", return_value=True)
                        )
                        host_report = stack.enter_context(
                            patch.object(delivery, "create_github_issue")
                        )
                        private_report = stack.enter_context(
                            patch.object(delivery, "create_private_github_issue")
                        )
                        stack.enter_context(redirect_stdout(io.StringIO()))
                        arguments = ["--state", str(state_path)]
                        if allowed is not None:
                            arguments.extend(["--config", str(config_path)])
                        app.main(arguments)
                    self.assertEqual(
                        [call.args[0] for call in search.call_args_list], ["paid-q", "global-q"]
                    )
                    self.assertEqual(refresh.call_count, 2)
                    self.assertEqual(paid_checks.call_count, 1)
                    self.assertEqual(metadata.call_count, 1)
                    self.assertEqual(guide.call_count, 1)
                    accepted = allowed is None or "1–3h" in allowed
                    self.assertEqual(telegram.call_count, int(accepted))
                    host_report.assert_not_called()
                    private_report.assert_not_called()
                    self.assertEqual(
                        state.load_seen_state(state_path).urls(),
                        {paid_source["html_url"], strategic_source["html_url"]}
                        if accepted
                        else set(),
                    )
