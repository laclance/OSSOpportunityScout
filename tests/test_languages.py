from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
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
from opportunity_scout.strategic import discovery
from opportunity_scout.types import (
    Candidate,
    CandidateLane,
    GitHubIssue,
    RepositoryMetadata,
    SourceFailureReason,
)
from tests.helpers import candidate, issue
from tests.test_run import FIXED_TIME, dependencies


class LanguagePolicyTests(unittest.TestCase):
    def test_primary_language_matching_and_unknown_policy(self) -> None:
        cases = (
            (None, False),
            ("", False),
            ("Unknown", False),
            ("UNKNOWN", False),
            ("Go", True),
            ("gO", True),
            ("Python", True),
            ("Rust", False),
        )
        for language, matches in cases:
            for configured, expected in (
                ((), True),
                (("GO", "python", "GO"), matches),
                (("Unknown",), False),
            ):
                config = preferences.ScoutPreferences(languages=configured)
                with self.subTest(language=language, configured=configured):
                    self.assertEqual(selection.language_accepted(language, config), expected)
                    for is_paid in (False, True):
                        final = candidate(
                            paid=is_paid, cash_score=80, language=language or "Unknown"
                        )
                        self.assertEqual(
                            selection.candidate_rejection(final, config),
                            None if expected else "repository language excluded by configuration",
                        )


class ResolvedLanguageTests(unittest.TestCase):
    def test_verification_uses_cached_primary_metadata_in_both_lanes(self) -> None:
        metadata: list[RepositoryMetadata] = [
            {"stargazers_count": 2000},
            {"stargazers_count": 2000, "language": None},
            {"stargazers_count": 2000, "language": ""},
            {"stargazers_count": 2000, "language": "Unknown"},
            {"stargazers_count": 2000, "language": "gO"},
            {"stargazers_count": 2000, "language": "Rust"},
        ]
        for is_paid in (False, True):
            for meta in metadata:
                for languages in ((), ("GO",)):
                    accepted = not languages or meta.get("language") == "gO"
                    fresh = issue(
                        title="Python issue in a Go module",
                        body="Bounty $500" if is_paid else "Python and Go",
                        labels=["Python", "Go", "help wanted"],
                        comments=0,
                    )
                    repo_cache: dict[str, RepositoryMetadata] = {}
                    guide_cache: dict[str, str | None] = {}
                    with (
                        self.subTest(paid=is_paid, meta=meta, languages=languages),
                        patch.object(app, "refresh_issue", return_value=(fresh, None)),
                        patch.object(app, "strategic_rejection", return_value=None),
                        patch.object(
                            paid_verification,
                            "candidate_rejection_reason",
                            return_value=(None, "$500"),
                        ),
                        patch.object(app, "fetch_repo_metadata", return_value=meta) as fetch,
                        patch.object(app, "contribution_guide", return_value=None) as guide,
                    ):
                        for _ in range(2):
                            final, reason = app.verify(
                                issue(),
                                None,
                                repo_cache,
                                guide_cache,
                                require_paid=is_paid,
                                scout_preferences=preferences.ScoutPreferences(languages=languages),
                            )
                            self.assertEqual(
                                reason,
                                None
                                if accepted
                                else "repository language excluded by configuration",
                            )
                            if accepted:
                                assert final is not None
                                self.assertEqual(
                                    final["language"], meta.get("language") or "Unknown"
                                )
                                self.assertEqual(final["paid"], is_paid)
                            else:
                                self.assertIsNone(final)
                    fetch.assert_called_once_with("example/project", None)
                    self.assertEqual(guide.call_count, int(accepted))
                    self.assertIs(repo_cache["example/project"], meta)

    def test_refreshed_and_aggregator_repositories_control_language(self) -> None:
        for aggregator in (False, True):
            for final_language in ("Go", "Rust"):
                fresh = issue(html_url="https://github.com/upstream/repo/issues/7", comments=0)
                source = issue(
                    body="TARGET_REPOSITORY: upstream/repo\nORIGINAL_ISSUE_URL: https://github.com/upstream/repo/issues/7"
                    if aggregator
                    else "Go source",
                )
                cache: dict[str, RepositoryMetadata] = {
                    "example/project": {"language": "Rust" if final_language == "Go" else "Go"},
                    "upstream/repo": {"language": final_language},
                }
                with (
                    self.subTest(aggregator=aggregator, language=final_language),
                    patch.object(
                        app,
                        "refresh_issue",
                        side_effect=[(source, None), (fresh, None)]
                        if aggregator
                        else [(fresh, None)],
                    ),
                    patch.object(app, "issue_from_github_url", return_value=fresh) as upstream,
                    patch.object(app, "strategic_rejection", return_value=None),
                    patch.object(app, "fetch_repo_metadata") as fetch,
                    patch.object(app, "contribution_guide", return_value=None),
                ):
                    final, reason = app.verify(
                        source,
                        None,
                        cache,
                        {},
                        scout_preferences=preferences.ScoutPreferences(languages=("go",)),
                    )
                self.assertEqual(upstream.call_count, int(aggregator))
                fetch.assert_not_called()
                if final_language == "Go":
                    assert final is not None
                    self.assertEqual(
                        (final["repo"], final["language"], reason), ("upstream/repo", "Go", None)
                    )
                else:
                    self.assertEqual(
                        (final, reason), (None, "repository language excluded by configuration")
                    )

    def test_language_rejections_do_not_settle_strategic_repository_slots(self) -> None:
        rows = []
        outcomes: list[tuple[Candidate | None, str | None]] = []
        for index in range(6):
            source = issue(html_url=f"https://github.com/example/project/issues/{index + 1}")
            score = 100 - index
            rows.append((score, score, 0, source))
            outcomes.append(
                (
                    candidate(
                        url=source["html_url"],
                        priority_score=score,
                        career_score=score,
                        language="Rust" if index < 3 else "Go",
                        paid=index == 0,
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
                scout_preferences=preferences.ScoutPreferences(languages=("gO",)),
            )
        self.assertEqual(
            [final["url"] for final in found], [row[3]["html_url"] for row in rows[3:]]
        )
        self.assertEqual(verify.call_count, 6)
        self.assertEqual(rejects, {"repository language excluded by configuration": 3})


class LanguageOrchestrationTests(unittest.TestCase):
    def test_strategic_prefilter_defers_wrapper_language_to_resolved_upstream(self) -> None:
        wrapper = issue(
            html_url="https://github.com/aggregator/jobs/issues/4",
            title="[READY FOR ENGINEERING] upstream task",
            body=(
                "TARGET_REPOSITORY https://github.com/upstream/repo\n"
                "ORIGINAL_ISSUE_URL https://github.com/upstream/repo/issues/7"
            ),
            labels=[{"name": "help wanted"}],
            comments=0,
        )
        resolved = candidate(
            repo="upstream/repo",
            issue_number=7,
            url="https://github.com/upstream/repo/issues/7",
            language="Go",
            career_score=90,
            priority_score=90,
        )
        metadata_calls: list[str] = []

        def fetch(repo: str, _token: str | None) -> RepositoryMetadata:
            metadata_calls.append(repo)
            return {"language": "Rust", "archived": False}

        def build(
            source: GitHubIssue,
            _lane: CandidateLane,
            _signal: str | None,
            meta: RepositoryMetadata,
            _guide: str | None,
        ) -> Candidate:
            repo, number = github.issue_repo_and_number(source)
            return candidate(
                repo=repo,
                issue_number=number,
                url=source["html_url"],
                language=meta.get("language") or "Unknown",
                career_score=90,
                priority_score=90,
            )

        with (
            patch.object(app, "TARGET_REPOS", []),
            patch.object(app, "strategic_basic_candidate", return_value=True),
            patch.object(app, "fetch_repo_metadata", side_effect=fetch),
            patch.object(app, "build_candidate", side_effect=build),
            patch.object(app, "strategic_preflight_rejection", return_value=None),
            patch.object(app, "verify", return_value=(resolved, None)) as verify,
        ):
            found, rejected, examples, audit = app.discover_strategic(
                None,
                set(),
                set(),
                {},
                {},
                [("global", {"items": [wrapper]})],
                scout_preferences=preferences.ScoutPreferences(languages=("Go",)),
            )

        self.assertEqual(found, [resolved])
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])
        self.assertEqual(audit, [])
        self.assertEqual(metadata_calls, ["aggregator/jobs"])
        verify.assert_called_once()

    def test_configured_languages_reuse_metadata_without_search_fanout(self) -> None:
        metadata: dict[str, RepositoryMetadata] = {
            "example/go": {"language": "Go"},
            "example/rust": {"language": "Rust"},
            "example/unknown": {"stargazers_count": 1000},
        }
        paid_items = [
            issue(
                html_url=f"https://github.com/{repo}/issues/{number}",
                body="Bounty $500",
                comments=0,
            )
            for repo in metadata
            for number in (1, 2)
        ]
        strategic_items = [
            issue(
                html_url=f"https://github.com/{repo}/issues/{number}",
                labels=["help wanted"],
                comments=0,
            )
            for repo in metadata
            for number in (3, 4)
        ]

        def build(
            source: GitHubIssue,
            lane: CandidateLane,
            _signal: str | None,
            meta: RepositoryMetadata,
            _guide: str | None,
            _comments: object = None,
        ) -> Candidate:
            repo, number = github.issue_repo_and_number(source)
            return candidate(
                repo=repo,
                issue_number=number,
                url=source["html_url"],
                paid=lane == "paid",
                cash_score=90 if lane == "paid" else 0,
                language=meta.get("language") or "Unknown",
            )

        for authenticated in (False, True):
            for languages in ((), ("gO",), ("go", "RUST", "Python")):
                with (
                    self.subTest(prefetch=authenticated, languages=languages),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    config_path = Path(directory, "config.toml")
                    config_path.write_text(
                        f"version = 1\n[discovery]\nlanguages = {list(languages)!r}\n",
                        encoding="utf-8",
                    )
                    state_path = Path(directory, "state.json")
                    env = {"TELEGRAM_BOT_TOKEN": "delivery", "TELEGRAM_CHAT_ID": "chat"}
                    if authenticated:
                        env.update(GITHUB_TOKEN="scanner", GITHUB_REPOSITORY="host/repo")
                    with (
                        patch.dict(os.environ, env, clear=True),
                        patch.object(app, "TARGET_REPOS", []),
                        patch.object(app, "PAID_DISCOVERY_QUERIES", ["paid-q"]),
                        patch.object(app, "STRATEGIC_GLOBAL_QUERIES", ["global-q"]),
                        patch.object(
                            github,
                            "search_github",
                            side_effect=lambda query, _token, per_page=15: {
                                "items": paid_items if query == "paid-q" else strategic_items
                            },
                        ) as search,
                        patch.object(app, "platform_paid_refs", return_value=sources.PlatformDiscoveryResult(refs={}, failures=())),
                        patch.object(
                            app, "refresh_issue", side_effect=lambda source, _token: (source, None)
                        ) as refresh,
                        patch.object(app, "strategic_preflight_rejection", return_value=None),
                        patch.object(app, "strategic_rejection", return_value=None),
                        patch.object(
                            paid_verification,
                            "candidate_rejection_reason",
                            return_value=(None, "$500"),
                        ) as paid_checks,
                        patch.object(
                            app,
                            "fetch_repo_metadata",
                            side_effect=lambda repo, _token: metadata[repo],
                        ) as fetch,
                        patch.object(app, "contribution_guide", return_value=None) as guide,
                        patch.object(app, "build_candidate", side_effect=build),
                        patch.object(app, "sleep"),
                        patch.object(delivery, "send_telegram_notification", return_value=True),
                        redirect_stdout(io.StringIO()),
                    ):
                        app.main(["--config", str(config_path), "--state", str(state_path)])
                    self.assertEqual(
                        [call.args[0] for call in search.call_args_list], ["paid-q", "global-q"]
                    )
                    self.assertEqual(fetch.call_count, 3)
                    self.assertEqual({call.args[0] for call in fetch.call_args_list}, set(metadata))
                    expected_refreshes = 12 if not languages else 8 if len(languages) == 1 else 10
                    self.assertEqual(refresh.call_count, expected_refreshes)
                    self.assertEqual(paid_checks.call_count, 6)
                    accepted_repos = (
                        set(metadata)
                        if not languages
                        else {"example/go"}
                        if len(languages) == 1
                        else {"example/go", "example/rust"}
                    )
                    self.assertEqual(guide.call_count, len(accepted_repos))
                    seen = state.load_seen_state(state_path).urls()
                    expected = {
                        source["html_url"]
                        for source in paid_items + strategic_items
                        if github.issue_repo_and_number(source)[0] in accepted_repos
                    }
                    # The unchanged eight-result cap still applies for the empty list.
                    if not languages:
                        expected = {
                            source["html_url"] for source in paid_items + strategic_items[:2]
                        }
                    self.assertEqual(seen, expected)

    def test_filtering_precedes_queue_limit_and_preserves_coverage_delivery_state(self) -> None:
        blocked = candidate(paid=True, priority_score=100, url="blocked", language="Rust")
        unknown = candidate(priority_score=99, url="unknown", language="Unknown")
        allowed = candidate(priority_score=80, url="allowed", language="gO")
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
                    preferences=preferences.ScoutPreferences(languages=("GO",)),
                )
                strategic_rejects: dict[str, int] = {}
                if not complete:
                    strategic_rejects[SourceFailureReason("source refresh failed")] = 1
                callbacks = replace(
                    dependencies(),
                    discover_paid=lambda *_args: ([blocked], {}, []),
                    discover_strategic=lambda *_args: (
                        [unknown, allowed],
                        strategic_rejects,
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
                    {"allowed"} if complete and delivered else set(),
                )
