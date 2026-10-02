from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bountyscout.state as state_module
from bountyscout.state import (
    SeenState,
    SeenStateLoadError,
    SeenStateSaveError,
    load_seen_state,
    parse_seen_state,
    save_seen_state,
)
from bountyscout.types import IssueLifecycleStatus


FIXED_REPORTED_AT = "2026-10-02T08:30:00Z"
FIXED_CHECKED_AT = "2026-10-02T10:00:00+02:00"
URL_A = "https://github.com/example/project/issues/1"
URL_B = "https://github.com/example/project/issues/2"
URL_C = "https://github.com/example/project/issues/3"
URL_D = "https://github.com/example/project/issues/4"
NON_GITHUB_URL = "https://example.com/tasks/1"


def current_document(
    *,
    version: object = 2,
    seen: object | None = None,
) -> dict[str, object]:
    return {
        "version": version,
        "seen": (
            {
                URL_A: {
                    "reported_at": FIXED_REPORTED_AT,
                    "last_checked_at": FIXED_CHECKED_AT,
                }
            }
            if seen is None
            else seen
        ),
    }


class SeenStateParsingTests(unittest.TestCase):
    def test_missing_file_is_empty_first_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = load_seen_state(Path(temp_dir) / "missing.json")

        self.assertEqual(loaded.urls(), set())

    def test_empty_legacy_list_loads(self) -> None:
        loaded = parse_seen_state([])
        self.assertEqual(loaded.urls(), set())

    def test_populated_legacy_list_preserves_all_urls_without_fake_timestamps(self) -> None:
        loaded = parse_seen_state([URL_B, URL_A])

        self.assertEqual(loaded.urls(), {URL_A, URL_B})
        self.assertEqual(loaded.record(URL_A), state_module.SeenEntry())
        self.assertEqual(loaded.record(URL_B), state_module.SeenEntry())

    def test_legacy_duplicate_urls_collapse_to_one_logical_record(self) -> None:
        loaded = parse_seen_state([URL_A, URL_A])
        self.assertEqual(loaded.urls(), {URL_A})

    def test_current_versioned_format_loads(self) -> None:
        loaded = parse_seen_state(current_document())

        self.assertTrue(loaded.contains(URL_A))
        self.assertEqual(
            loaded.record(URL_A),
            state_module.SeenEntry(
                reported_at=FIXED_REPORTED_AT,
                last_checked_at=FIXED_CHECKED_AT,
            ),
        )

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "Unsupported seen-state version"):
            parse_seen_state(current_document(version=3))

    def test_malformed_top_level_structures_fail_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "top level"):
            parse_seen_state("not-state")

        malformed = current_document()
        malformed["extra"] = True
        with self.assertRaisesRegex(SeenStateLoadError, "unexpected top-level fields"):
            parse_seen_state(malformed)

    def test_malformed_version_field_fails_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "missing the version"):
            parse_seen_state({"seen": {}})

        with self.assertRaisesRegex(SeenStateLoadError, "version must be an integer"):
            parse_seen_state(current_document(version=True))

    def test_malformed_seen_field_fails_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "'seen' field must be an object"):
            parse_seen_state(current_document(seen=[]))

    def test_malformed_entries_fail_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "must be an object"):
            parse_seen_state(current_document(seen={URL_A: []}))

        with self.assertRaisesRegex(SeenStateLoadError, "malformed fields"):
            parse_seen_state(current_document(seen={URL_A: {"reported_at": None}}))

    def test_malformed_url_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "URL keys"):
            parse_seen_state([42])

        with self.assertRaisesRegex(SeenStateLoadError, "URL keys"):
            parse_seen_state(
                current_document(seen={" ": {"reported_at": None, "last_checked_at": None}})
            )

        with self.assertRaisesRegex(SeenStateLoadError, "URL keys"):
            parse_seen_state(
                {
                    "version": 2,
                    "seen": {
                        42: {
                            "reported_at": None,
                            "last_checked_at": None,
                        }
                    },
                }
            )

    def test_malformed_timestamps_fail_closed(self) -> None:
        malformed_values: list[object] = [
            42,
            "",
            "not-a-date",
            "2026-10-02T10:00:00",
        ]
        for value in malformed_values:
            with self.subTest(value=value):
                with self.assertRaises(SeenStateLoadError):
                    parse_seen_state(
                        current_document(
                            seen={
                                URL_A: {
                                    "reported_at": value,
                                    "last_checked_at": None,
                                }
                            }
                        )
                    )

    def test_malformed_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(SeenStateLoadError, "Malformed JSON"):
                load_seen_state(path)

    def test_unreadable_existing_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(SeenStateLoadError, "Could not read seen-state"):
                load_seen_state(Path(temp_dir))


class SeenStateLogicTests(unittest.TestCase):
    def test_seen_and_unseen_membership(self) -> None:
        seen = SeenState.from_urls([URL_A])
        self.assertTrue(seen.contains(URL_A))
        self.assertIn(URL_A, seen)
        self.assertFalse(seen.contains(URL_B))
        self.assertNotIn(URL_B, seen)

    def test_mark_reported_adds_once_and_preserves_original_report_time(self) -> None:
        seen = SeenState()
        seen.mark_reported(URL_A, reported_at=FIXED_REPORTED_AT)
        seen.mark_reported(URL_A, reported_at=FIXED_CHECKED_AT)

        self.assertEqual(seen.urls(), {URL_A})
        self.assertEqual(
            seen.record(URL_A),
            state_module.SeenEntry(reported_at=FIXED_REPORTED_AT),
        )

    def test_mark_reported_many_handles_empty_and_populated_inputs(self) -> None:
        seen = SeenState()
        seen.mark_reported_many([])
        seen.mark_reported_many([URL_B, URL_A], reported_at=FIXED_REPORTED_AT)

        self.assertEqual(seen.urls(), {URL_A, URL_B})
        self.assertEqual(seen.record(URL_B), state_module.SeenEntry(reported_at=FIXED_REPORTED_AT))
        self.assertIsNone(seen.record("https://github.com/example/project/issues/999"))

    def test_saving_migrated_legacy_state_emits_only_versioned_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            migrated = parse_seen_state([URL_B, URL_A])
            save_seen_state(migrated, path)
            saved: object = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved,
            {
                "version": 2,
                "seen": {
                    URL_A: {"reported_at": None, "last_checked_at": None},
                    URL_B: {"reported_at": None, "last_checked_at": None},
                },
            },
        )

    def test_save_reload_round_trip_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            original = SeenState()
            original.mark_reported(URL_A, reported_at=FIXED_REPORTED_AT)
            save_seen_state(original, path)
            loaded = load_seen_state(path)

        self.assertEqual(loaded.urls(), original.urls())
        self.assertEqual(loaded.record(URL_A), original.record(URL_A))

    def test_save_output_is_deterministic_readable_and_newline_terminated(self) -> None:
        expected = (
            "{\n"
            '  "version": 2,\n'
            '  "seen": {\n'
            f'    "{URL_A}": {{\n'
            '      "reported_at": null,\n'
            '      "last_checked_at": null\n'
            "    },\n"
            f'    "{URL_B}": {{\n'
            '      "reported_at": null,\n'
            '      "last_checked_at": null\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.json"
            second_path = Path(temp_dir) / "second.json"
            save_seen_state(SeenState.from_urls([URL_B, URL_A]), first_path)
            save_seen_state(SeenState.from_urls([URL_A, URL_B]), second_path)

            first = first_path.read_text(encoding="utf-8")
            second = second_path.read_text(encoding="utf-8")

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)

    def test_atomic_save_failure_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("old-state\n", encoding="utf-8")
            with patch("bountyscout.state.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(SeenStateSaveError, "Could not save seen-state"):
                    save_seen_state(SeenState.from_urls([URL_A]), path)

            self.assertEqual(path.read_text(encoding="utf-8"), "old-state\n")


class SeenStateRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
        self.stale = (self.now - timedelta(days=31)).isoformat()
        self.older = (self.now - timedelta(days=60)).isoformat()
        self.recent = (self.now - timedelta(days=10)).isoformat()

    def test_selection_prioritizes_unknown_then_oldest_and_excludes_ineligible_urls(self) -> None:
        seen = SeenState(
            {
                URL_B: state_module.SeenEntry(),
                URL_A: state_module.SeenEntry(),
                URL_C: state_module.SeenEntry(last_checked_at=self.stale),
                URL_D: state_module.SeenEntry(last_checked_at=self.recent),
                NON_GITHUB_URL: state_module.SeenEntry(),
            }
        )

        self.assertEqual(
            state_module.select_revalidation_batch(seen, self.now, limit=3),
            [URL_A, URL_B, URL_C],
        )
        self.assertEqual(state_module.select_revalidation_batch(seen, self.now, limit=0), [])

    def test_selection_orders_checked_entries_oldest_first(self) -> None:
        seen = SeenState(
            {
                URL_A: state_module.SeenEntry(last_checked_at=self.stale),
                URL_B: state_module.SeenEntry(last_checked_at=self.older),
            }
        )
        self.assertEqual(
            state_module.select_revalidation_batch(seen, self.now),
            [URL_B, URL_A],
        )

    def test_eligibility_handles_unknown_stale_and_recent_entries(self) -> None:
        self.assertTrue(state_module.eligible_for_revalidation(state_module.SeenEntry(), self.now))
        self.assertTrue(
            state_module.eligible_for_revalidation(
                state_module.SeenEntry(last_checked_at=self.stale),
                self.now,
            )
        )
        self.assertFalse(
            state_module.eligible_for_revalidation(
                state_module.SeenEntry(last_checked_at=self.recent),
                self.now,
            )
        )

    def test_mark_checked_remove_and_copy_do_not_expose_or_mutate_original(self) -> None:
        original = SeenState.from_urls([URL_A])
        copied = original.copy()

        self.assertTrue(copied.mark_checked(URL_A, checked_at=FIXED_CHECKED_AT))
        self.assertFalse(copied.mark_checked(URL_B, checked_at=FIXED_CHECKED_AT))
        self.assertFalse(copied.remove(URL_B))
        self.assertTrue(copied.remove(URL_A))

        self.assertTrue(original.contains(URL_A))
        self.assertEqual(original.record(URL_A), state_module.SeenEntry())

    def test_apply_results_prunes_only_closed_and_updates_attempt_timestamps(self) -> None:
        seen = SeenState.from_urls([URL_A, URL_B, URL_C, URL_D])
        expected_checked = "2026-10-02T10:00:00Z"

        state_module.apply_revalidation_result(seen, URL_A, "open", self.now)
        state_module.apply_revalidation_result(seen, URL_B, "not_found", self.now)
        state_module.apply_revalidation_result(seen, URL_C, "failed", self.now)
        state_module.apply_revalidation_result(seen, URL_D, "closed", self.now)

        record_a = seen.record(URL_A)
        record_b = seen.record(URL_B)
        record_c = seen.record(URL_C)
        assert record_a is not None
        assert record_b is not None
        assert record_c is not None
        self.assertEqual(record_a.last_checked_at, expected_checked)
        self.assertEqual(record_b.last_checked_at, expected_checked)
        self.assertEqual(record_c.last_checked_at, expected_checked)
        self.assertFalse(seen.contains(URL_D))
        state_module.apply_revalidation_result(seen, URL_D, "failed", self.now)
        self.assertFalse(seen.contains(URL_D))

    def test_maintenance_walks_unknown_entries_progressively(self) -> None:
        seen = SeenState.from_urls([URL_B, URL_A])
        first = state_module.maintain_seen_state(
            seen,
            self.now,
            lambda _url: "open",
            limit=1,
        )
        second_batch = state_module.select_revalidation_batch(
            first.state,
            self.now,
            limit=1,
        )

        self.assertEqual(first.checked_urls, (URL_A,))
        self.assertEqual(second_batch, [URL_B])
        original_record = seen.record(URL_A)
        assert original_record is not None
        self.assertIsNone(original_record.last_checked_at)

    def test_maintenance_prunes_closed_retains_failures_and_catches_checker_exceptions(
        self,
    ) -> None:
        seen = SeenState.from_urls([URL_A, URL_B, URL_C, URL_D])
        statuses: dict[str, IssueLifecycleStatus] = {
            URL_A: "open",
            URL_B: "closed",
            URL_C: "not_found",
        }

        def checker(url: str) -> IssueLifecycleStatus:
            if url == URL_D:
                raise RuntimeError("transport exploded")
            return statuses[url]

        result = state_module.maintain_seen_state(
            seen,
            self.now,
            checker,
            limit=4,
        )

        self.assertEqual(result.checked_urls, (URL_A, URL_B, URL_C, URL_D))
        self.assertEqual(result.pruned_urls, (URL_B,))
        self.assertTrue(result.state.contains(URL_A))
        self.assertFalse(result.state.contains(URL_B))
        self.assertTrue(result.state.contains(URL_C))
        self.assertTrue(result.state.contains(URL_D))
        failed_record = result.state.record(URL_D)
        assert failed_record is not None
        self.assertIsNotNone(failed_record.last_checked_at)
        self.assertTrue(seen.contains(URL_B))

    def test_empty_or_non_github_state_produces_no_maintenance_changes(self) -> None:
        seen = SeenState.from_urls([NON_GITHUB_URL])
        result = state_module.maintain_seen_state(
            seen,
            self.now,
            lambda _url: "closed",
        )
        self.assertEqual(result.checked_urls, ())
        self.assertEqual(result.pruned_urls, ())
        self.assertTrue(result.state.contains(NON_GITHUB_URL))

    def test_large_state_never_exceeds_configured_check_limit(self) -> None:
        urls = [
            f"https://github.com/example/project/issues/{number}" for number in range(1, 10_051)
        ]
        seen = SeenState.from_urls(urls)
        calls: list[str] = []

        def checker(url: str) -> IssueLifecycleStatus:
            calls.append(url)
            return "open"

        result = state_module.maintain_seen_state(seen, self.now, checker)

        self.assertEqual(len(calls), state_module.SEEN_STATE_REVALIDATION_LIMIT)
        self.assertEqual(len(result.checked_urls), state_module.SEEN_STATE_REVALIDATION_LIMIT)

    def test_confirmed_closed_issue_can_be_discovered_again_if_reopened_later(self) -> None:
        seen = SeenState.from_urls([URL_A])
        maintained = state_module.maintain_seen_state(
            seen,
            self.now,
            lambda _url: "closed",
            limit=1,
        ).state

        self.assertFalse(maintained.contains(URL_A))
        self.assertNotIn(URL_A, maintained.urls())

    def test_maintained_serialization_remains_deterministic_v2(self) -> None:
        seen = SeenState.from_urls([URL_B, URL_A])
        maintained = state_module.maintain_seen_state(
            seen,
            self.now,
            lambda _url: "open",
            limit=1,
        ).state

        document = maintained.to_document()
        self.assertEqual(document["version"], 2)
        self.assertEqual(list(document["seen"]), [URL_A, URL_B])
        self.assertEqual(
            document["seen"][URL_A]["last_checked_at"],
            "2026-10-02T10:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
