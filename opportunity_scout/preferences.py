"""Immutable, non-secret scout preferences and strict version-1 TOML loading.

This module validates configuration only. Application assembly will opt into it
in a later migration slice; importing it does not configure a scanner run.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from opportunity_scout.types import EffortBucket

_DEFAULT_EFFORT: Final[tuple[EffortBucket, ...]] = ("<1h", "1–3h", "3–6h", "6–12h", "1d+")
_REPOSITORY: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class ScoutPreferences:
    """Preference values, separate from credential-bearing RunConfig.

    Repository targets add strategic sources; exclusions take precedence across
    both lanes. Language and effort acceptance, lane/source controls, and final
    classification thresholds are applied by later runtime-wiring slices.
    """

    version: Literal[1] = 1
    name: str | None = None
    paid: bool = True
    strategic: bool = True
    languages: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    exclude_repositories: tuple[str, ...] = ()
    global_search: bool = True
    effort: tuple[EffortBucket, ...] = _DEFAULT_EFFORT
    min_career_score: int = 55
    min_cash_score: int = 55
    max_results: int = 8


class ScoutPreferencesError(ValueError):
    """Configuration is unreadable or violates the preference-only schema."""


def _table(value: object, field: str, allowed: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ScoutPreferencesError(f"{field} must be a TOML table.")
    if value.keys() - allowed:
        raise ScoutPreferencesError(f"{field} contains unknown keys.")
    return cast(Mapping[str, object], value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScoutPreferencesError(f"{field} must be a non-empty string.")
    return value.strip()


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ScoutPreferencesError(f"{field} must be a boolean.")
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ScoutPreferencesError(f"{field} must be an integer from {minimum}–{maximum}.")
    return value


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ScoutPreferencesError(f"{field} must be an array of strings.")
    return tuple(_text(item, field) for item in value)


def _repositories(value: object, field: str) -> tuple[str, ...]:
    repositories = _strings(value, field)
    for repository in repositories:
        if not _REPOSITORY.fullmatch(repository) or repository.split("/")[1] in {".", ".."}:
            raise ScoutPreferencesError(f"{field} entries must be owner/repository identifiers.")
    return repositories


def _effort(value: object) -> tuple[EffortBucket, ...]:
    buckets = _strings(value, "preferences.effort")
    if any(bucket not in _DEFAULT_EFFORT for bucket in buckets):
        raise ScoutPreferencesError("preferences.effort contains an invalid effort bucket.")
    return cast(tuple[EffortBucket, ...], buckets)


def parse_scout_preferences(document: object) -> ScoutPreferences:
    """Validate a decoded TOML document without I/O or mutable input retention."""
    root = _table(
        document, "configuration", {"version", "name", "lanes", "discovery", "preferences"}
    )
    version = root.get("version")
    if type(version) is not int or version != 1:
        raise ScoutPreferencesError("configuration requires version = 1 (integer).")
    lanes = _table(root.get("lanes", {}), "lanes", {"paid", "strategic"})
    discovery = _table(
        root.get("discovery", {}),
        "discovery",
        {"languages", "repositories", "exclude_repositories", "global_search"},
    )
    preferences = _table(
        root.get("preferences", {}),
        "preferences",
        {"effort", "min_career_score", "min_cash_score", "max_results"},
    )
    return ScoutPreferences(
        name=_text(root["name"], "name") if "name" in root else None,
        paid=_boolean(lanes.get("paid", True), "lanes.paid"),
        strategic=_boolean(lanes.get("strategic", True), "lanes.strategic"),
        languages=_strings(discovery.get("languages", []), "discovery.languages"),
        repositories=_repositories(discovery.get("repositories", []), "discovery.repositories"),
        exclude_repositories=_repositories(
            discovery.get("exclude_repositories", []), "discovery.exclude_repositories"
        ),
        global_search=_boolean(discovery.get("global_search", True), "discovery.global_search"),
        effort=_effort(preferences.get("effort", list(_DEFAULT_EFFORT))),
        min_career_score=_integer(
            preferences.get("min_career_score", 55), "preferences.min_career_score", 0, 100
        ),
        min_cash_score=_integer(
            preferences.get("min_cash_score", 55), "preferences.min_cash_score", 0, 100
        ),
        max_results=_integer(preferences.get("max_results", 8), "preferences.max_results", 1, 8),
    )


def load_scout_preferences(path: Path) -> ScoutPreferences:
    """Read an explicit path, failing closed without echoing file contents."""
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ScoutPreferencesError("Could not read a valid UTF-8 scout TOML file.") from error
    return parse_scout_preferences(document)
