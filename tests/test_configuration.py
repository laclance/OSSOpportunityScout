from __future__ import annotations

import io
import os
import runpy
import sys
import tempfile
import unittest
from contextlib import ExitStack, chdir, redirect_stdout
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from opportunity_scout import (
    app,
    delivery,
    github,
    paid,
    paid_verification,
    preferences,
    run,
    selection,
    state,
)
from opportunity_scout.strategic import discovery
from opportunity_scout.types import (
    Candidate,
    RepositoryMetadata,
    SearchBatch,
    SourceFailureReason,
)
from tests.helpers import candidate, issue
from tests.test_run import FIXED_TIME, dependencies


class SourceControlTests(unittest.TestCase):
    def test_configured_sources_make_only_enabled_requests_with_and_without_prefetch(self) -> None:
        for authenticated in (False, True):
            for paid_enabled in (False, True):
                for strategic_enabled in (False, True):
                    for global_enabled in (False, True):
                        with (
                            self.subTest(
                                prefetch=authenticated,
                                paid=paid_enabled,
                                strategic=strategic_enabled,
                                global_search=global_enabled,
                            ),
                            tempfile.TemporaryDirectory() as directory,
                        ):
                            config_path = Path(directory, "scout.toml")
                            config_path.write_text(
                                "version = 1\n[lanes]\n"
                                f"paid = {str(paid_enabled).lower()}\n"
                                f"strategic = {str(strategic_enabled).lower()}\n"
                                "[discovery]\n"
                                f"global_search = {str(global_enabled).lower()}\n"
                                'repositories = ["extra/repo", "BASE/REPO", "extra/repo"]\n',
                                encoding="utf-8",
                            )
                            env = (
                                {"GITHUB_TOKEN": "scan-token", "GITHUB_REPOSITORY": "host/repo"}
                                if authenticated
                                else {}
                            )
                            with (
                                patch.dict(os.environ, env, clear=True),
                                patch.object(app, "TARGET_REPOS", ["base/repo"]),
                                patch.object(app, "PAID_DISCOVERY_QUERIES", ["paid-q"]),
                                patch.object(app, "STRATEGIC_GLOBAL_QUERIES", ["global-q"]),
                                patch.object(
                                    github, "search_github", return_value={"items": []}
                                ) as search,
                                patch.object(
                                    app, "target_repo_issue_pool", return_value=([], None)
                                ) as pools,
                                patch.object(
                                    app, "platform_paid_refs", return_value={}
                                ) as platforms,
                                patch.object(app, "sleep") as sleeper,
                                patch.object(delivery, "send_telegram_notification") as telegram,
                                patch.object(delivery, "create_github_issue") as host_report,
                                patch.object(
                                    delivery, "create_private_github_issue"
                                ) as private_report,
                                redirect_stdout(io.StringIO()),
                            ):
                                app.main(
                                    [
                                        "--config",
                                        str(config_path),
                                        "--state",
                                        str(Path(directory, "state.json")),
                                    ]
                                )
                            expected_queries = (["paid-q"] if paid_enabled else []) + (
                                ["global-q"] if strategic_enabled and global_enabled else []
                            )
                            self.assertEqual(
                                [call.args[0] for call in search.call_args_list], expected_queries
                            )
                            self.assertEqual(platforms.call_count, int(paid_enabled))
                            self.assertEqual(
                                [call.args[0] for call in pools.call_args_list],
                                ["base/repo", "extra/repo"] if strategic_enabled else [],
                            )
                            self.assertEqual(
                                sleeper.call_count, int(authenticated and len(expected_queries) > 1)
                            )
                            telegram.assert_not_called()
                            host_report.assert_not_called()
                            private_report.assert_not_called()

    def test_repository_sources_are_additive_case_insensitive_and_exclusions_win(self) -> None:
        config = preferences.ScoutPreferences(
            repositories=("EXAMPLE/PROJECT", "extra/repo", "extra/repo", "blocked/repo"),
            exclude_repositories=("BLOCKED/REPO",),
        )
        self.assertEqual(
            selection.strategic_repositories(["example/project", "blocked/repo"], config),
            ["example/project", "extra/repo"],
        )
        self.assertFalse(selection.repository_excluded(None, config))
        self.assertIsNone(selection.candidate_rejection(candidate(), config))
        self.assertEqual(
            selection.candidate_rejection(candidate(repo="Blocked/Repo"), config),
            "repository excluded by configuration",
        )
        for is_paid in (False, True):
            self.assertEqual(
                selection.candidate_rejection(
                    candidate(paid=is_paid),
                    preferences.ScoutPreferences(paid=False, strategic=False),
                ),
                "final candidate lane disabled by configuration",
            )

    def test_configured_repository_source_does_not_extend_scoring_targets(self) -> None:
        configured = preferences.ScoutPreferences(repositories=("extra/repo",))
        self.assertEqual(
            selection.strategic_repositories(["base/repo"], configured),
            ["base/repo", "extra/repo"],
        )
        metadata = RepositoryMetadata(stargazers_count=0, pushed_at=None, language="Rust")
        with patch.object(app, "TARGET_REPOS", ["base/repo"]):
            configured_candidate = app.build_candidate(
                issue(html_url="https://github.com/extra/repo/issues/42", comments=0),
                "strategic",
                None,
                metadata,
                None,
                [],
            )
            target_candidate = app.build_candidate(
                issue(html_url="https://github.com/base/repo/issues/42", comments=0),
                "strategic",
                None,
                metadata,
                None,
                [],
            )

        self.assertNotIn("target repo bonus", configured_candidate["career_reasons"])
        self.assertIn("target repo bonus", target_candidate["career_reasons"])
        self.assertEqual(
            target_candidate["career_score"],
            configured_candidate["career_score"] + 14,
        )

    def test_global_control_discards_prefetched_results_and_does_not_search(self) -> None:
        blocked = issue(html_url="https://github.com/blocked/repo/issues/1")
        batches: list[list[SearchBatch] | None] = [None, [("ignored", {"items": [blocked]})]]
        for global_results in batches:
            with (
                self.subTest(global_results=global_results),
                patch.object(app, "TARGET_REPOS", ["blocked/repo"]),
                patch.object(app, "strategic_global_search_results") as global_search,
                patch.object(app, "target_repo_issue_pool") as pools,
                patch.object(app, "fetch_repo_metadata") as meta,
            ):
                result = app.discover_strategic(
                    None,
                    set(),
                    set(),
                    {},
                    {},
                    global_results,
                    scout_preferences=preferences.ScoutPreferences(
                        global_search=False, exclude_repositories=("BLOCKED/REPO",)
                    ),
                )
            self.assertEqual(result, ([], {}, [], []))
            global_search.assert_not_called()
            pools.assert_not_called()
            meta.assert_not_called()

    def test_search_and_platform_exclusions_skip_downstream_requests_in_both_lanes(self) -> None:
        blocked = issue(html_url="https://github.com/blocked/repo/issues/1")
        excluded = preferences.ScoutPreferences(exclude_repositories=("BLOCKED/REPO",))
        with (
            patch.object(
                app,
                "platform_paid_refs",
                return_value={"https://github.com/blocked/repo/issues/2": "paid"},
            ),
            patch.object(app, "issue_from_github_url") as platform_issue,
            patch.object(app, "verify") as verify,
            patch.object(app, "fetch_repo_metadata") as meta,
            patch.object(app, "TARGET_REPOS", []),
        ):
            paid_result = app.discover_paid(
                None, set(), {}, {}, [("paid", {"items": [blocked]})], scout_preferences=excluded
            )
            strategic_result = app.discover_strategic(
                None,
                set(),
                set(),
                {},
                {},
                [("global", {"items": [blocked]})],
                scout_preferences=excluded,
            )
        self.assertEqual(paid_result, ([], {}, []))
        self.assertEqual(strategic_result, ([], {}, [], []))
        platform_issue.assert_not_called()
        verify.assert_not_called()
        meta.assert_not_called()


class ResolvedPreferenceTests(unittest.TestCase):
    def test_exclusion_after_source_refresh_and_aggregator_resolution(self) -> None:
        blocked = issue(html_url="https://github.com/blocked/repo/issues/7")
        wrapper = issue(
            body="TARGET_REPOSITORY: blocked/repo\nORIGINAL_ISSUE_URL: https://github.com/blocked/repo/issues/7"
        )
        for require_paid in (False, True):
            for aggregator in (False, True):
                with (
                    self.subTest(paid=require_paid, aggregator=aggregator),
                    patch.object(
                        app,
                        "refresh_issue",
                        side_effect=[(wrapper, None), (blocked, None)]
                        if aggregator
                        else [(blocked, None)],
                    ),
                    patch.object(app, "issue_from_github_url", return_value=blocked) as upstream,
                    patch.object(github, "issue_comments_checked") as comments,
                    patch.object(app, "fetch_repo_metadata") as meta,
                    patch.object(app, "contribution_guide") as guide,
                    patch.object(paid, "payment_signal") as payment,
                ):
                    result = app.verify(
                        issue(),
                        None,
                        {},
                        {},
                        require_paid=require_paid,
                        scout_preferences=preferences.ScoutPreferences(
                            exclude_repositories=("Blocked/Repo",)
                        ),
                    )
                self.assertEqual(result, (None, "repository excluded by configuration"))
                self.assertEqual(upstream.call_count, int(aggregator))
                comments.assert_not_called()
                meta.assert_not_called()
                guide.assert_not_called()
                payment.assert_not_called()

    def test_resolved_repository_identity_controls_target_bonus(self) -> None:
        metadata = RepositoryMetadata(stargazers_count=0, pushed_at=None, language="Rust")
        cases = (
            ("ordinary/repo", ("aggregator/jobs",), False),
            ("base/repo", (), True),
        )
        for upstream_repo, configured_repositories, expected_bonus in cases:
            wrapper = issue(
                html_url="https://github.com/aggregator/jobs/issues/4",
                title="[READY FOR ENGINEERING] upstream task",
                body=(
                    f"TARGET_REPOSITORY: {upstream_repo}\n"
                    f"ORIGINAL_ISSUE_URL: https://github.com/{upstream_repo}/issues/42"
                ),
                comments=0,
            )
            upstream = issue(
                html_url=f"https://github.com/{upstream_repo}/issues/42",
                comments=0,
            )
            with (
                self.subTest(upstream_repo=upstream_repo),
                patch.object(app, "TARGET_REPOS", ["base/repo"]),
                patch.object(app, "refresh_issue", side_effect=[(wrapper, None), (upstream, None)]),
                patch.object(app, "issue_from_github_url", return_value=upstream),
                patch.object(app, "strategic_basic_candidate", return_value=True),
                patch.object(paid, "payment_signal", return_value=None),
                patch.object(app, "supplemental_payment_signal", return_value=None),
                patch.object(github, "issue_comments_checked", return_value=([], None)),
                patch.object(app, "strategic_rejection", return_value=None),
                patch.object(app, "fetch_repo_metadata", return_value=metadata),
                patch.object(app, "contribution_guide", return_value=None),
            ):
                resolved, reason = app.verify(
                    wrapper,
                    None,
                    {},
                    {},
                    scout_preferences=preferences.ScoutPreferences(
                        repositories=configured_repositories
                    ),
                )

            self.assertIsNone(reason)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(
                "target repo bonus" in resolved["career_reasons"],
                expected_bonus,
            )

    def test_final_lane_uses_refreshed_payment_evidence(self) -> None:
        for final_paid in (False, True):
            fresh = issue(body="Bounty $500" if final_paid else "", comments=0)
            config = preferences.ScoutPreferences(paid=not final_paid, strategic=final_paid)
            with (
                self.subTest(final_paid=final_paid),
                patch.object(app, "refresh_issue", return_value=(fresh, None)),
                patch.object(github, "issue_comments_checked", return_value=([], None)),
                patch.object(app, "strategic_rejection", return_value=None),
                patch.object(
                    paid_verification,
                    "candidate_rejection_reason",
                    return_value=(None, "Bounty $500"),
                ),
                patch.object(app, "fetch_repo_metadata") as meta,
            ):
                result = app.verify(
                    issue(body="" if final_paid else "Bounty $500"),
                    None,
                    {},
                    {},
                    scout_preferences=config,
                )
            self.assertEqual(result, (None, "final candidate lane disabled by configuration"))
            meta.assert_not_called()

    def test_rejected_resolved_results_do_not_settle_repository_slots(self) -> None:
        rows = []
        outcomes: list[tuple[Candidate | None, str | None]] = []
        for index in range(5):
            source = issue(html_url=f"https://github.com/example/project/issues/{index + 1}")
            rows.append((99 - index, 99 - index, 0, source))
            final = candidate(
                url=source["html_url"],
                priority_score=99 - index,
                career_score=99 - index,
                paid=index == 1,
                repo="blocked/repo" if index == 0 else "example/project",
            )
            outcomes.append((final, None))
        with (
            patch.object(app, "TARGET_REPOS", []),
            patch.object(
                discovery,
                "select_strategic_candidates",
                return_value=discovery.StrategicDiscoverySelection({"example/project": rows}, []),
            ),
            patch.object(app, "strategic_preflight_rejection", return_value=None),
            patch.object(app, "verify", side_effect=outcomes) as verify,
        ):
            found, rejects, _, _ = app.discover_strategic(
                None,
                set(),
                set(),
                {},
                {},
                [],
                scout_preferences=preferences.ScoutPreferences(
                    paid=False, exclude_repositories=("BLOCKED/REPO",)
                ),
            )
        self.assertEqual([item["url"] for item in found], [row[3]["html_url"] for row in rows[2:]])
        self.assertEqual(verify.call_count, 5)
        self.assertEqual(
            rejects,
            {
                "repository excluded by configuration": 1,
                "final candidate lane disabled by configuration": 1,
            },
        )

    def test_paid_resolved_exclusion_and_configured_cash_threshold(self) -> None:
        blocked, allowed = issue(), issue(html_url="https://github.com/example/project/issues/43")
        with (
            patch.object(app, "platform_paid_refs", return_value={}),
            patch.object(
                app,
                "verify",
                side_effect=[
                    (candidate(paid=True, repo="blocked/repo", cash_score=90), None),
                    (candidate(paid=True, url=allowed["html_url"], cash_score=70), None),
                ],
            ),
        ):
            found, rejects, _ = app.discover_paid(
                None,
                set(),
                {},
                {},
                [("paid", {"items": [blocked, allowed]})],
                scout_preferences=preferences.ScoutPreferences(
                    exclude_repositories=("blocked/repo",), min_cash_score=100
                ),
            )
        self.assertEqual(found, [])
        self.assertEqual(
            rejects,
            {
                "repository excluded by configuration": 1,
                "cash score 70/100 below paid threshold 100/100": 1,
            },
        )


class InvocationTests(unittest.TestCase):
    def test_invalid_default_and_explicit_config_stop_before_state_network_and_delivery(
        self,
    ) -> None:
        documents: tuple[bytes | None, ...] = (
            None,
            b"invalid TOML",
            b"version = 2",
            b"version = 1\n[lanes]\npaid = 0",
            b"version = 1\nsecret = 'never print this'",
            b"version = 1\n# \xff",
        )
        for explicit in (False, True):
            for document in documents:
                with (
                    self.subTest(explicit=explicit, document=document),
                    tempfile.TemporaryDirectory() as directory,
                    chdir(directory),
                    ExitStack() as stack,
                ):
                    path = Path("invalid.toml" if explicit else "scout.toml")
                    # A valid alternate file must never become an implicit fallback.
                    Path("scout.toml" if explicit else "scout.example.toml").write_text(
                        "version = 1\n", encoding="utf-8"
                    )
                    if document is not None:
                        path.write_bytes(document)
                    selected_state = Path("custom.json")
                    selected_state.write_bytes(b"corrupt state must not be read")
                    mocks = [
                        stack.enter_context(patch.object(owner, name))
                        for owner, name in (
                            (run, "RunConfig"),
                            (run, "run_combined_scan"),
                            (state, "load_seen_state"),
                            (state, "save_seen_state"),
                            (github, "github_get"),
                            (github, "search_github"),
                            (github, "issue_lifecycle"),
                            (app, "platform_paid_refs"),
                            (delivery, "send_telegram_notification"),
                            (delivery, "send_discord_notification"),
                            (delivery, "create_github_issue"),
                            (delivery, "create_private_github_issue"),
                        )
                    ]
                    arguments = ["--state", str(selected_state)]
                    if explicit:
                        arguments.extend(["--config", str(path)])
                    with self.assertRaises(preferences.ScoutPreferencesError):
                        app.main(arguments)
                    for mock in mocks:
                        mock.assert_not_called()
                    self.assertEqual(selected_state.read_bytes(), b"corrupt state must not be read")

    def test_unreadable_default_and_explicit_config_fail_before_assembly(self) -> None:
        for arguments in ([], ["--config", "unreadable.toml"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(Path, "open", side_effect=PermissionError("unreadable")),
                patch.object(run, "RunConfig") as config,
                patch.object(run, "run_combined_scan") as combined,
            ):
                with self.assertRaises(preferences.ScoutPreferencesError):
                    app.main(arguments)
                config.assert_not_called()
                combined.assert_not_called()

    def test_explicit_config_overrides_missing_or_invalid_default_and_keeps_state_path(
        self,
    ) -> None:
        for default in (None, "invalid TOML", "version = 1\n[lanes]\npaid = false\n"):
            with (
                self.subTest(default=default),
                tempfile.TemporaryDirectory() as directory,
                chdir(directory),
                patch.object(run, "run_combined_scan") as combined,
                patch.dict(os.environ, {}, clear=True),
            ):
                if default is not None:
                    Path("scout.toml").write_text(default, encoding="utf-8")
                path = Path("profiles/chosen.toml")
                path.parent.mkdir()
                path.write_text("version = 1\n[preferences]\nmax_results = 3\n", encoding="utf-8")
                app.main(["--config", str(path), "--state", "selected.json"])
                config = combined.call_args.args[0]
                self.assertEqual(config.preferences, preferences.load_scout_preferences(path))
                self.assertEqual(config.state_path, Path("selected.json"))

    def test_help_needs_no_configuration_or_scan(self) -> None:
        with (
            patch.object(preferences, "load_scout_preferences") as load,
            patch.object(run, "run_combined_scan") as combined,
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as result:
                app.main(["--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertIn("default: scout.toml", output.getvalue())
            load.assert_not_called()
            combined.assert_not_called()

    def test_configured_command_line_assembly_thresholds_and_result_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.toml")
            path.write_text(
                'version = 1\n[discovery]\nlanguages = ["Python"]\n[preferences]\neffort = ["1–3h"]\nmin_cash_score = 100\nmin_career_score = 100\nmax_results = 1\n',
                encoding="utf-8",
            )
            items = [
                candidate(
                    url=f"https://github.com/example/project/issues/{n}",
                    language="Python",
                    career_score=100,
                )
                for n in range(9)
            ]
            with (
                patch.dict(
                    os.environ,
                    {"TELEGRAM_BOT_TOKEN": "delivery-token", "TELEGRAM_CHAT_ID": "chat"},
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "opportunity_scout.py",
                        "--config",
                        str(path),
                        "--state",
                        str(Path(directory, "selected.json")),
                    ],
                ),
                patch.object(app, "discover_paid", return_value=([], {}, [])) as paid_scan,
                patch.object(
                    app, "discover_strategic", return_value=(items, {}, [], [])
                ) as strategic_scan,
                patch.object(delivery, "send_telegram_notification", return_value=True),
            ):
                app.main()
            config = preferences.load_scout_preferences(path)
            self.assertEqual(paid_scan.call_args.kwargs, {"scout_preferences": config})
            self.assertEqual(strategic_scan.call_args.kwargs, {"scout_preferences": config})
            self.assertEqual(len(state.load_seen_state(Path(directory, "selected.json")).urls()), 1)

    def test_default_executable_and_state_only_invocation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for arguments in ([], ["--state", "custom.json"]):
            with (
                self.subTest(arguments=arguments),
                tempfile.TemporaryDirectory() as directory,
                chdir(directory),
                patch.object(sys, "argv", ["opportunity_scout.py", *arguments]),
                patch.object(run, "run_combined_scan") as combined,
                patch.dict(os.environ, {}, clear=True),
            ):
                Path("scout.toml").write_text(
                    "version = 1\n[lanes]\npaid = false\n[preferences]\nmax_results = 2\n",
                    encoding="utf-8",
                )
                runpy.run_path(str(root / "opportunity_scout.py"), run_name="__main__")
                expected = preferences.load_scout_preferences(Path("scout.toml"))
            config, callbacks, _ = combined.call_args.args
            self.assertEqual(config.preferences, expected)
            self.assertEqual(
                config.state_path, Path("custom.json" if arguments else "seen_bounties.json")
            )
            for callback, original in (
                (callbacks.discover_paid, app.discover_paid),
                (callbacks.discover_strategic, app.discover_strategic),
                (callbacks.prefetch_discovery_searches, app.prefetch_discovery_searches),
            ):
                self.assertIsInstance(callback, partial)
                assert isinstance(callback, partial)
                self.assertIs(callback.func, original)
                self.assertEqual(callback.keywords, {"scout_preferences": expected})


class SelectedStateTests(unittest.TestCase):
    def test_disabled_sources_are_complete_and_allow_custom_path_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected_path = Path(directory, "selected.json")
            state.save_seen_state(
                state.SeenState.from_urls(["https://github.com/example/project/issues/1"]),
                selected_path,
            )
            config = run.RunConfig(
                "scan-token",
                "host/repo",
                None,
                None,
                None,
                state_path=selected_path,
                preferences=preferences.ScoutPreferences(paid=False, strategic=False),
            )
            callbacks = dependencies()
            with (
                patch.object(app, "discover_paid") as paid_scan,
                patch.object(app, "discover_strategic") as strategic_scan,
                patch.object(app, "prefetch_discovery_searches") as prefetch,
            ):
                callbacks = replace(
                    callbacks,
                    discover_paid=paid_scan,
                    discover_strategic=strategic_scan,
                    prefetch_discovery_searches=prefetch,
                    issue_lifecycle=lambda _url: "closed",
                )
                result = run.run_combined_scan(config, callbacks, FIXED_TIME)
            self.assertTrue(result.coverage.complete)
            self.assertTrue(result.state_saved)
            self.assertEqual(state.load_seen_state(selected_path).urls(), set())
            paid_scan.assert_not_called()
            strategic_scan.assert_not_called()
            prefetch.assert_not_called()

    def test_configured_incomplete_coverage_and_failed_delivery_preserve_custom_state(self) -> None:
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete), tempfile.TemporaryDirectory() as directory:
                selected_path = Path(directory, "selected.json")
                state.save_seen_state(
                    state.SeenState.from_urls(["https://github.com/example/project/issues/1"]),
                    selected_path,
                )
                original = selected_path.read_bytes()
                config = run.RunConfig(
                    None,
                    None,
                    "token",
                    "chat",
                    None,
                    state_path=selected_path,
                    preferences=preferences.ScoutPreferences(paid=False, global_search=False),
                )
                callbacks = replace(
                    dependencies(),
                    discover_strategic=lambda *_args: (
                        [candidate()],
                        {SourceFailureReason("source refresh failed"): 1} if incomplete else {},
                        [],
                        [],
                    ),
                    send_telegram=lambda *_args: incomplete,
                )
                result = run.run_combined_scan(config, callbacks, FIXED_TIME)
                self.assertFalse(result.state_saved)
                self.assertEqual(result.coverage.complete, not incomplete)
                self.assertEqual(selected_path.read_bytes(), original)

    def test_custom_state_controls_deduplication_delivery_commit_and_quiet_maintenance(
        self,
    ) -> None:
        old_url = "https://github.com/example/project/issues/1"
        new_url = "https://github.com/example/project/issues/2"
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            legacy_path = Path("seen_bounties.json")
            legacy_path.write_text("legacy state must never be read", encoding="utf-8")
            selected_path = Path("selected.json")
            state.save_seen_state(state.SeenState.from_urls([old_url]), selected_path)
            config = run.RunConfig(
                None, None, "delivery-token", "chat", None, state_path=selected_path
            )
            seen_inputs: list[set[str]] = []

            def paid_scan(
                _token: str | None,
                seen: set[str],
                _repos: dict[str, RepositoryMetadata],
                _guides: dict[str, str | None],
                _search: object,
            ) -> run.PaidDiscoveryResult:
                seen_inputs.append(seen)
                return [candidate(paid=True, cash_score=80, url=new_url)], {}, []

            callbacks = replace(
                dependencies(),
                discover_paid=paid_scan,
                send_telegram=lambda *_args: True,
                issue_lifecycle=lambda _url: "closed",
            )
            result = run.run_combined_scan(config, callbacks, FIXED_TIME)
            self.assertTrue(result.state_saved)
            self.assertEqual(seen_inputs, [{old_url}])
            self.assertEqual(state.load_seen_state(selected_path).urls(), {new_url})
            quiet = replace(dependencies(), issue_lifecycle=lambda _url: "closed")
            result = run.run_combined_scan(config, quiet, FIXED_TIME)
            self.assertTrue(result.state_saved)
            self.assertEqual(state.load_seen_state(selected_path).urls(), set())
            self.assertEqual(
                legacy_path.read_text(encoding="utf-8"), "legacy state must never be read"
            )

    def test_custom_state_corruption_and_save_failure_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected_path = Path(directory, "selected.json")
            selected_path.write_text("corrupt", encoding="utf-8")
            config = run.RunConfig(None, None, "token", "chat", None, state_path=selected_path)
            with (
                patch.object(app, "discover_paid") as paid_scan,
                patch.object(delivery, "send_telegram_notification") as send,
            ):
                with self.assertRaises(state.SeenStateLoadError):
                    app.main(
                        [
                            "--config",
                            str(Path(__file__).resolve().parents[1] / "scout.example.toml"),
                            "--state",
                            str(selected_path),
                        ]
                    )
            paid_scan.assert_not_called()
            send.assert_not_called()
            selected_path.unlink()
            broken_path = Path(directory, "missing-parent", "state.json")
            callbacks = replace(
                dependencies(),
                discover_paid=lambda *_args: ([candidate()], {}, []),
                send_telegram=lambda *_args: True,
            )
            with self.assertRaises(state.SeenStateSaveError):
                run.run_combined_scan(
                    replace(config, state_path=broken_path), callbacks, FIXED_TIME
                )
            self.assertFalse(broken_path.exists())

    def test_final_queue_preferences_filter_before_limit_across_discovery_sources(self) -> None:
        blocked = candidate(repo="blocked/repo", priority_score=100)
        disabled = candidate(paid=True, priority_score=99, url="paid")
        allowed = candidate(priority_score=80, url="eligible")
        callbacks = replace(
            dependencies(),
            discover_strategic=lambda *_args: ([blocked, disabled, allowed], {}, [], []),
        )
        config = run.RunConfig(
            None,
            None,
            None,
            None,
            None,
            preferences=preferences.ScoutPreferences(
                paid=False, exclude_repositories=("blocked/repo",)
            ),
        )
        with patch.object(state, "load_seen_state", return_value=state.SeenState()):
            result = run.run_combined_scan(config, callbacks, FIXED_TIME, report_limit=1)
        self.assertEqual(result.queue, (allowed,))
        self.assertTrue(result.coverage.complete)
