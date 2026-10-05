from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import ExitStack, chdir, redirect_stdout
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
    sources,
    state,
)
from opportunity_scout.strategic import discovery, verification
from opportunity_scout.types import Candidate, IssueRow, RepositoryMetadata, SourceFailureReason
from tests.helpers import candidate, issue
from tests.test_run import FIXED_TIME, dependencies


METADATA: RepositoryMetadata = {
    "language": "Go",
    "stargazers_count": 100000,
    "pushed_at": "2026-10-03T00:00:00Z",
}


class ScorePolicyTests(unittest.TestCase):
    def test_inclusive_threshold_boundaries_follow_final_classification(self) -> None:
        for is_paid in (False, True):
            for minimum in (0, 55, 100):
                for score in {0, 100, max(0, minimum - 1), minimum, min(100, minimum + 1)}:
                    for other_score in (0, 100):
                        with self.subTest(
                            paid=is_paid, minimum=minimum, score=score, other=other_score
                        ):
                            final = candidate(
                                paid=is_paid,
                                cash_score=score if is_paid else other_score,
                                career_score=other_score if is_paid else score,
                            )
                            config = preferences.ScoutPreferences(
                                min_cash_score=minimum if is_paid else 100,
                                min_career_score=100 if is_paid else minimum,
                            )
                            self.assertEqual(
                                selection.candidate_rejection(final, config) is None,
                                score >= minimum,
                            )

    def test_strategic_verifier_uses_final_scores_for_both_classifications(self) -> None:
        source = issue()
        for is_paid in (False, True):
            for score in (79, 80):
                final = candidate(
                    paid=is_paid,
                    cash_score=score if is_paid else 0,
                    career_score=0 if is_paid else score,
                )
                with self.subTest(paid=is_paid, score=score), redirect_stdout(io.StringIO()):
                    result = verification.verify_strategic_selection(
                        discovery.StrategicDiscoverySelection(
                            {"example/project": [(20, 20, 0, source)]}, []
                        ),
                        lambda _: (final, None),
                        lambda _: None,
                        min_cash_score=80,
                        min_career_score=80,
                    )
                self.assertEqual(result.candidates, [final] if score == 80 else [])
                self.assertEqual(result.network_checked_rows, 1)
                self.assertEqual(sum(result.rejected.values()), int(score < 80))


class RefreshedScoreTests(unittest.TestCase):
    def test_both_discovery_adapters_apply_threshold_for_final_lane(self) -> None:
        source = issue()
        for is_paid in (False, True):
            for score in (79, 80):
                final = candidate(
                    paid=is_paid,
                    cash_score=score if is_paid else 0,
                    career_score=0 if is_paid else score,
                )
                config = preferences.ScoutPreferences(min_cash_score=80, min_career_score=80)
                with (
                    self.subTest(paid=is_paid, score=score),
                    patch.object(
                        app,
                        "platform_paid_refs",
                        return_value=sources.PlatformDiscoveryResult(refs={}, failures=()),
                    ),
                    patch.object(
                        discovery,
                        "select_strategic_candidates",
                        return_value=(
                            discovery.StrategicDiscoverySelection(
                                {"example/project": [(20, 20, 0, source)]}, []
                            )
                        ),
                    ),
                    patch.object(app, "strategic_preflight_rejection", return_value=None),
                    patch.object(app, "verify", return_value=(final, None)),
                    redirect_stdout(io.StringIO()),
                ):
                    paid_result = app.discover_paid(
                        None,
                        set(),
                        {},
                        {},
                        [("paid", {"items": [source]})],
                        scout_preferences=config,
                    )
                    strategic_result = app.discover_strategic(
                        None, set(), set(), {}, {}, [], scout_preferences=config
                    )
                for found, rejects in (paid_result[:2], strategic_result[:2]):
                    self.assertEqual(found, [final] if score == 80 else [])
                    self.assertEqual(sum(rejects.values()), int(score < 80))

    def test_paid_refresh_and_resolved_upstream_scores_control_acceptance(self) -> None:
        small = issue(body="Bounty $2", comments=0)
        large = issue(body="Bounty $5000", comments=0)
        for source, fresh in ((small, large), (large, small)):
            for wrapped in (False, True):
                final = app.build_candidate(fresh, "paid", fresh["body"], METADATA, None)
                preview = app.build_candidate(source, "paid", source["body"], METADATA, None)
                self.assertNotEqual(final["cash_score"], preview["cash_score"])
                wrapper = issue(
                    body=(
                        "TARGET_REPOSITORY: example/project\n"
                        "ORIGINAL_ISSUE_URL: https://github.com/example/project/issues/42"
                    )
                )
                for minimum in (final["cash_score"], final["cash_score"] + 1):
                    with (
                        self.subTest(source=source["body"], wrapped=wrapped, minimum=minimum),
                        patch.object(
                            app,
                            "platform_paid_refs",
                            return_value=sources.PlatformDiscoveryResult(refs={}, failures=()),
                        ),
                        patch.object(
                            app,
                            "refresh_issue",
                            side_effect=(
                                [(wrapper, None), (fresh, None)] if wrapped else [(fresh, None)]
                            ),
                        ) as refresh,
                        patch.object(app, "issue_from_github_url", return_value=fresh) as upstream,
                        patch.object(
                            paid_verification,
                            "candidate_rejection_reason",
                            return_value=(None, fresh["body"]),
                        ) as checks,
                        patch.object(app, "fetch_repo_metadata", return_value=METADATA),
                        patch.object(app, "contribution_guide", return_value=None),
                        redirect_stdout(io.StringIO()),
                    ):
                        found, rejects, _ = app.discover_paid(
                            None,
                            set(),
                            {},
                            {},
                            [("paid", {"items": [source]})],
                            scout_preferences=preferences.ScoutPreferences(
                                min_cash_score=minimum, min_career_score=100
                            ),
                        )
                    self.assertEqual(found, [final] if minimum == final["cash_score"] else [])
                    self.assertEqual(sum(rejects.values()), int(minimum > final["cash_score"]))
                    self.assertEqual(refresh.call_count, 2 if wrapped else 1)
                    self.assertEqual(upstream.call_count, int(wrapped))
                    checks.assert_called_once()

    def test_strategic_refresh_changes_classification_and_threshold_in_both_directions(
        self,
    ) -> None:
        for final_paid in (False, True):
            source = issue(body="" if final_paid else "Bounty $500", comments=0)
            fresh = issue(body="Bounty $500" if final_paid else "", comments=0)
            final = app.build_candidate(
                fresh,
                "paid" if final_paid else "strategic",
                "$500" if final_paid else None,
                METADATA,
                None,
            )
            score = final["cash_score"] if final_paid else final["career_score"]
            for minimum in (score, score + 1):
                with (
                    self.subTest(paid=final_paid, minimum=minimum),
                    patch.object(
                        discovery,
                        "select_strategic_candidates",
                        return_value=(
                            discovery.StrategicDiscoverySelection(
                                {"example/project": [(20, 20, 0, source)]}, []
                            )
                        ),
                    ),
                    patch.object(app, "refresh_issue", return_value=(fresh, None)) as refresh,
                    patch.object(app, "strategic_preflight_rejection", return_value=None),
                    patch.object(app, "strategic_rejection", return_value=None),
                    patch.object(
                        paid_verification, "candidate_rejection_reason", return_value=(None, "$500")
                    ) as paid_checks,
                    patch.object(github, "issue_comments_checked", return_value=([], None)),
                    patch.object(app, "contribution_guide", return_value=None),
                    redirect_stdout(io.StringIO()),
                ):
                    found, rejects, _, _ = app.discover_strategic(
                        None,
                        set(),
                        set(),
                        {"example/project": METADATA},
                        {},
                        [],
                        scout_preferences=preferences.ScoutPreferences(
                            min_cash_score=minimum if final_paid else 100,
                            min_career_score=100 if final_paid else minimum,
                        ),
                    )
                self.assertEqual(found, [final] if minimum == score else [])
                self.assertEqual(sum(rejects.values()), int(minimum > score))
                refresh.assert_called_once()
                self.assertEqual(paid_checks.call_count, int(final_paid))

    def test_threshold_rejections_do_not_settle_repository_slots(self) -> None:
        rows: list[IssueRow] = []
        outcomes: list[tuple[Candidate | None, str | None]] = []
        for index in range(7):
            source = issue(html_url=f"https://github.com/example/project/issues/{index + 1}")
            priority = 100 - index if index < 6 else 40
            rows.append((priority, priority, 0, source))
            score = 79 if index < 3 else 80
            is_paid = index % 2 == 0
            outcomes.append(
                (
                    candidate(
                        url=source["html_url"],
                        paid=is_paid,
                        priority_score=priority,
                        cash_score=score if is_paid else 0,
                        career_score=0 if is_paid else score,
                    ),
                    None,
                )
            )
        with (
            patch.object(
                discovery,
                "select_strategic_candidates",
                return_value=(discovery.StrategicDiscoverySelection({"example/project": rows}, [])),
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
                scout_preferences=preferences.ScoutPreferences(
                    min_cash_score=80, min_career_score=80
                ),
            )
        self.assertEqual(
            [final["url"] for final in found], [row[3]["html_url"] for row in rows[3:6]]
        )
        self.assertEqual(sum(rejects.values()), 3)
        self.assertEqual(verify.call_count, 6)


class ScoreOrchestrationTests(unittest.TestCase):
    def test_filter_precedes_url_deduplication_and_every_result_limit(self) -> None:
        eligible = [candidate(url=f"url-{n}", priority_score=90 - n) for n in range(12)]
        blocked = [
            candidate(paid=True, url="url-0", cash_score=54, priority_score=100),
            candidate(url="blocked", career_score=54, priority_score=100),
        ]
        for maximum in range(1, 9):
            for complete, delivered in ((True, True), (False, True), (True, False)):
                with (
                    self.subTest(maximum=maximum, complete=complete, delivered=delivered),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    path = Path(directory, "state.json")
                    config = run.RunConfig(
                        None,
                        None,
                        "delivery",
                        "chat",
                        None,
                        state_path=path,
                        preferences=preferences.ScoutPreferences(max_results=maximum),
                    )
                    strategic_rejects: dict[str, int] = {}
                    if not complete:
                        strategic_rejects[SourceFailureReason("source refresh failed")] = 1
                    callbacks = replace(
                        dependencies(),
                        discover_paid=lambda *_: (blocked, {}, []),
                        discover_strategic=lambda *_: (
                            list(reversed(eligible)),
                            strategic_rejects,
                            [],
                            [],
                        ),
                        send_telegram=lambda *_: delivered,
                    )
                    with redirect_stdout(io.StringIO()):
                        result = run.run_combined_scan(config, callbacks, FIXED_TIME)
                    self.assertEqual(result.queue, tuple(eligible[:maximum]))
                    self.assertEqual(result.coverage.complete, complete)
                    self.assertEqual(result.state_saved, complete and delivered)
                    self.assertEqual(
                        state.load_seen_state(path).urls(),
                        {item["url"] for item in eligible[:maximum]}
                        if complete and delivered
                        else set(),
                    )

    def test_configured_thresholds_limits_and_default_invocation_preserve_request_budgets(
        self,
    ) -> None:
        paid_sources = [
            issue(
                html_url=f"https://github.com/example/project/issues/{n}",
                body="Bounty $500",
                comments=0,
            )
            for n in range(1, 7)
        ]
        strategic_sources = [
            issue(
                html_url=f"https://github.com/example/project/issues/{n}",
                labels=["help wanted"],
                comments=0,
            )
            for n in range(7, 10)
        ]
        finals = [
            app.build_candidate(source, "paid", "$500", METADATA, None) for source in paid_sources
        ] + [
            app.build_candidate(source, "strategic", None, METADATA, None)
            for source in strategic_sources
        ]
        cases = (
            None,
            preferences.ScoutPreferences(),
            preferences.ScoutPreferences(max_results=1),
            preferences.ScoutPreferences(min_cash_score=100, min_career_score=0),
            preferences.ScoutPreferences(min_cash_score=0, min_career_score=100),
            preferences.ScoutPreferences(min_cash_score=100, min_career_score=100),
        )
        for authenticated in (False, True):
            for configured in cases:
                with (
                    self.subTest(prefetch=authenticated, config=configured),
                    tempfile.TemporaryDirectory() as directory,
                    chdir(directory),
                ):
                    path = Path("scout.toml")
                    path.write_text(
                        "version = 1\n"
                        if configured is None
                        else (
                            "version = 1\n[preferences]\n"
                            f"min_cash_score = {configured.min_cash_score}\n"
                            f"min_career_score = {configured.min_career_score}\n"
                            f"max_results = {configured.max_results}\n"
                        ),
                        encoding="utf-8",
                    )
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
                                side_effect=(
                                    lambda query, _token, per_page=15: {
                                        "items": paid_sources
                                        if query == "paid-q"
                                        else strategic_sources
                                    }
                                ),
                            )
                        )
                        platforms = stack.enter_context(
                            patch.object(
                                app,
                                "platform_paid_refs",
                                return_value=sources.PlatformDiscoveryResult(
                                    refs={},
                                    failures=(),
                                ),
                            )
                        )
                        refresh = stack.enter_context(
                            patch.object(
                                app, "refresh_issue", side_effect=lambda item, _: (item, None)
                            )
                        )
                        stack.enter_context(
                            patch.object(app, "strategic_preflight_rejection", return_value=None)
                        )
                        stack.enter_context(
                            patch.object(app, "strategic_rejection", return_value=None)
                        )
                        comments = stack.enter_context(
                            patch.object(github, "issue_comments_checked", return_value=([], None))
                        )
                        checks = stack.enter_context(
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
                        sleeper = stack.enter_context(patch.object(app, "sleep"))
                        telegram = stack.enter_context(
                            patch.object(delivery, "send_telegram_notification", return_value=True)
                        )
                        host = stack.enter_context(patch.object(delivery, "create_github_issue"))
                        private = stack.enter_context(
                            patch.object(delivery, "create_private_github_issue")
                        )
                        stack.enter_context(redirect_stdout(io.StringIO()))
                        app.main([] if configured is None else ["--config", str(path)])
                    config = configured or preferences.ScoutPreferences()
                    expected = run.assemble_queue(
                        [],
                        [
                            item
                            for item in finals
                            if selection.candidate_rejection(item, config) is None
                        ],
                        limit=config.max_results,
                    )
                    self.assertEqual(
                        state.load_seen_state().urls(), {item["url"] for item in expected}
                    )
                    self.assertEqual(telegram.call_count, int(bool(expected)))
                    self.assertEqual(
                        [call.args[0] for call in search.call_args_list], ["paid-q", "global-q"]
                    )
                    self.assertEqual(refresh.call_count, 9)
                    self.assertEqual(checks.call_count, 6)
                    self.assertEqual(comments.call_count, 3)
                    platforms.assert_called_once()
                    metadata.assert_called_once()
                    guide.assert_called_once()
                    self.assertEqual(sleeper.call_count, int(authenticated))
                    host.assert_not_called()
                    private.assert_not_called()
