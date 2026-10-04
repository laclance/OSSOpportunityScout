from __future__ import annotations

import tempfile
import tomllib
import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import date
from pathlib import Path
from typing import get_args
from unittest.mock import patch

from opportunity_scout.preferences import (
    ScoutPreferences,
    ScoutPreferencesError,
    load_scout_preferences,
    parse_scout_preferences,
)
from opportunity_scout.run import RunConfig
from opportunity_scout.types import EffortBucket


class ScoutPreferencesParsingTests(unittest.TestCase):
    def test_required_version_with_omitted_or_empty_tables_uses_agreed_defaults(self) -> None:
        for document in (
            {"version": 1},
            {"version": 1, "lanes": {}, "discovery": {}, "preferences": {}},
        ):
            with self.subTest(document=document):
                self.assertEqual(parse_scout_preferences(document), ScoutPreferences())
        defaults = ScoutPreferences()
        self.assertEqual(defaults.version, 1)
        self.assertIsNone(defaults.name)
        self.assertTrue(defaults.paid)
        self.assertTrue(defaults.strategic)
        self.assertTrue(defaults.global_search)
        self.assertEqual(defaults.languages, ())
        self.assertEqual(defaults.repositories, ())
        self.assertEqual(defaults.exclude_repositories, ())
        self.assertEqual(defaults.effort, get_args(EffortBucket))
        self.assertEqual((defaults.min_career_score, defaults.min_cash_score), (55, 55))
        self.assertEqual(defaults.max_results, 8)

    def test_all_fields_parse_without_resolving_runtime_policy(self) -> None:
        parsed = parse_scout_preferences(
            tomllib.loads("""
version = 1
name = " Weekend scout "
[lanes]
paid = false
strategic = false
[discovery]
languages = [" Python ", "Go", "PYTHON"]
repositories = [" Example/Backend ", "Other/repo.name_2-3"]
exclude_repositories = ["example/backend"]
global_search = false
[preferences]
effort = ["1d+", "<1h"]
min_career_score = 100
min_cash_score = 0
max_results = 1
""")
        )
        self.assertEqual(
            parsed,
            ScoutPreferences(
                name="Weekend scout",
                paid=False,
                strategic=False,
                languages=("Python", "Go", "PYTHON"),
                repositories=("Example/Backend", "Other/repo.name_2-3"),
                exclude_repositories=("example/backend",),
                global_search=False,
                effort=("1d+", "<1h"),
                min_career_score=100,
                min_cash_score=0,
                max_results=1,
            ),
        )

    def test_optional_fields_default_independently(self) -> None:
        parsed = parse_scout_preferences(
            {"version": 1, "lanes": {"paid": False}, "preferences": {"min_cash_score": 100}}
        )
        self.assertEqual(parsed, ScoutPreferences(paid=False, min_cash_score=100))

    def test_empty_effort_and_other_lists_are_valid(self) -> None:
        self.assertEqual(
            parse_scout_preferences({"version": 1, "preferences": {"effort": []}}),
            ScoutPreferences(effort=()),
        )

    def test_model_is_frozen_and_retains_no_mutable_input(self) -> None:
        languages = ["Python"]
        repositories = ["example/project"]
        exclusions = ["other/project"]
        effort = ["<1h"]
        document = {
            "version": 1,
            "discovery": {
                "languages": languages,
                "repositories": repositories,
                "exclude_repositories": exclusions,
            },
            "preferences": {"effort": effort},
        }
        parsed = parse_scout_preferences(document)
        original_hash = hash(parsed)
        for values in (languages, repositories, exclusions, effort):
            values.clear()
        document.clear()
        self.assertEqual(parsed.languages, ("Python",))
        self.assertEqual(parsed.repositories, ("example/project",))
        self.assertEqual(parsed.exclude_repositories, ("other/project",))
        self.assertEqual(parsed.effort, ("<1h",))
        self.assertEqual(hash(parsed), original_hash)
        with self.assertRaises(FrozenInstanceError):
            setattr(parsed, "max_results", 1)
        self.assertFalse(hasattr(parsed, "__dict__"))
        self.assertTrue(
            {field.name for field in fields(parsed)}.isdisjoint(
                {field.name for field in fields(RunConfig)}
            )
        )

    def test_invalid_root_and_section_tables_fail_closed(self) -> None:
        value: object
        for value in (None, [], "table", 1, True):
            with (
                self.subTest(root=value),
                self.assertRaisesRegex(ScoutPreferencesError, "configuration must be a TOML table"),
            ):
                parse_scout_preferences(value)
            for section in ("lanes", "discovery", "preferences"):
                with (
                    self.subTest(section=section, value=value),
                    self.assertRaisesRegex(
                        ScoutPreferencesError, f"{section} must be a TOML table"
                    ),
                ):
                    parse_scout_preferences({"version": 1, section: value})

    def test_missing_unsupported_and_wrong_type_versions_fail_closed(self) -> None:
        versions: tuple[object, ...] = (
            None,
            0,
            2,
            -1,
            True,
            False,
            1.0,
            "1",
            [],
            {},
            date(2026, 10, 4),
        )
        for document in ({}, *({"version": value} for value in versions)):
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(ScoutPreferencesError, "requires version = 1"),
            ):
                parse_scout_preferences(document)

    def test_unknown_keys_credentials_and_correctness_controls_fail_closed(self) -> None:
        for section, key in (
            (None, "token"),
            (None, "GITHUB_TOKEN"),
            (None, "delivery"),
            (None, "state"),
            ("lanes", "cash"),
            ("discovery", "network_workers"),
            ("discovery", "pagination"),
            ("preferences", "payment_verification"),
            ("preferences", "coverage_warning_threshold"),
            ("preferences", "private_github_reports_token"),
        ):
            document: dict[str, object] = {"version": 1}
            if section is None:
                document[key] = "do-not-log"
            else:
                document[section] = {key: "do-not-log"}
            with (
                self.subTest(section=section, key=key),
                self.assertRaisesRegex(ScoutPreferencesError, "unknown keys") as caught,
            ):
                parse_scout_preferences(document)
            self.assertNotIn("do-not-log", str(caught.exception))

    def test_name_must_be_a_nonempty_string(self) -> None:
        value: object
        for value in (None, False, 1, [], {}, "", " \t\n"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ScoutPreferencesError, "name must be a non-empty string"),
            ):
                parse_scout_preferences({"version": 1, "name": value})

    def test_boolean_fields_do_not_coerce_other_types(self) -> None:
        value: object
        for section, key in (
            ("lanes", "paid"),
            ("lanes", "strategic"),
            ("discovery", "global_search"),
        ):
            for value in (None, 0, 1, "true", "false", [], {}):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(
                        ScoutPreferencesError, f"{section}.{key} must be a boolean"
                    ),
                ):
                    parse_scout_preferences({"version": 1, section: {key: value}})

    def test_integer_ranges_and_types_are_strict(self) -> None:
        for key, minimum, maximum in (
            ("min_cash_score", 0, 100),
            ("min_career_score", 0, 100),
            ("max_results", 1, 8),
        ):
            for value in (minimum, maximum):
                with self.subTest(key=key, boundary=value):
                    parsed = parse_scout_preferences({"version": 1, "preferences": {key: value}})
                    self.assertEqual(getattr(parsed, key), value)
            invalid_values: tuple[object, ...] = (
                minimum - 1,
                maximum + 1,
                True,
                False,
                1.0,
                "1",
                None,
                [],
                {},
            )
            for invalid in invalid_values:
                with (
                    self.subTest(key=key, invalid=invalid),
                    self.assertRaisesRegex(
                        ScoutPreferencesError, f"preferences.{key} must be an integer"
                    ),
                ):
                    parse_scout_preferences({"version": 1, "preferences": {key: invalid}})

    def test_lists_require_nonempty_string_elements(self) -> None:
        value: object
        item: object
        for section, key in (
            ("discovery", "languages"),
            ("discovery", "repositories"),
            ("discovery", "exclude_repositories"),
            ("preferences", "effort"),
        ):
            for value in (None, "Python", (), 1, True, {}):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ScoutPreferencesError, "must be an array of strings"),
                ):
                    parse_scout_preferences({"version": 1, section: {key: value}})
            for item in (None, False, 1, [], {}, "", " \t"):
                with (
                    self.subTest(key=key, item=item),
                    self.assertRaisesRegex(ScoutPreferencesError, "must be a non-empty string"),
                ):
                    parse_scout_preferences({"version": 1, section: {key: [item]}})

    def test_repository_targets_and_exclusions_require_owner_repo_identifiers(self) -> None:
        for key in ("repositories", "exclude_repositories"):
            for value in (
                "repo",
                "/repo",
                "owner/",
                "owner/repo/extra",
                "owner/.",
                "owner/..",
                "https://github.com/owner/repo",
                "owner name/repo",
                "owner/repo name",
                "-owner/repo",
                "owner-/repo",
                "owner/repo?query=1",
            ):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ScoutPreferencesError, "owner/repository identifiers"),
                ):
                    parse_scout_preferences({"version": 1, "discovery": {key: [value]}})

    def test_effort_uses_only_canonical_bucket_vocabulary(self) -> None:
        for value in ("1-3h", "3-6h", "6-12h", "small", ">1d"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ScoutPreferencesError, "invalid effort bucket"),
            ):
                parse_scout_preferences({"version": 1, "preferences": {"effort": [value]}})


class ScoutPreferencesLoadingTests(unittest.TestCase):
    def test_generic_example_matches_defaults(self) -> None:
        self.assertEqual(
            load_scout_preferences(Path(__file__).resolve().parents[1] / "scout.example.toml"),
            ScoutPreferences(),
        )

    def test_explicit_path_loads_utf8_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.toml"
            path.write_text(
                'version = 1\nname = "Café scout"\n[lanes]\npaid = false\n', encoding="utf-8"
            )
            self.assertEqual(
                load_scout_preferences(path), ScoutPreferences(name="Café scout", paid=False)
            )

    def test_missing_unreadable_or_directory_path_never_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for path in (Path(directory), Path(directory) / "missing.toml"):
                with self.subTest(path=path), self.assertRaises(ScoutPreferencesError):
                    load_scout_preferences(path)
        with patch.object(Path, "open", side_effect=PermissionError("unreadable")):
            with self.assertRaises(ScoutPreferencesError):
                load_scout_preferences(Path("scout.toml"))

    def test_invalid_encoding_syntax_and_duplicate_definitions_fail_closed(self) -> None:
        for content in (
            b"version = 1\nname = '\xff'",
            b"version = [",
            b"version = 1\nversion = 1",
            b"version = 1\n[lanes]\n[lanes]",
            b"version = 1\n[lanes]\npaid = true\npaid = false",
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "scout.toml"
                path.write_bytes(content)
                with self.assertRaisesRegex(ScoutPreferencesError, "valid UTF-8 scout TOML"):
                    load_scout_preferences(path)

    def test_decoded_file_still_requires_valid_schema(self) -> None:
        for content in ("version = true", "version = 2", 'version = 1\ntoken = "do-not-log"'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "scout.toml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ScoutPreferencesError) as caught:
                    load_scout_preferences(path)
                self.assertNotIn("do-not-log", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
