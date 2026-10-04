"""Pure source and final-candidate preference controls."""

from collections.abc import Sequence

from opportunity_scout.preferences import ScoutPreferences
from opportunity_scout.types import Candidate


def repository_excluded(repository: str | None, preferences: ScoutPreferences) -> bool:
    return repository is not None and repository.casefold() in {
        name.casefold() for name in preferences.exclude_repositories
    }


def strategic_repositories(defaults: Sequence[str], preferences: ScoutPreferences) -> list[str]:
    """Add configured targets in order, deduplicating and giving exclusions precedence."""
    result: list[str] = []
    seen: set[str] = set()
    for repository in (*defaults, *preferences.repositories):
        key = repository.casefold()
        if key not in seen and not repository_excluded(repository, preferences):
            seen.add(key)
            result.append(repository)
    return result


def language_accepted(language: str | None, preferences: ScoutPreferences) -> bool:
    """Match primary repository language; explicit lists exclude unknown metadata."""
    if not preferences.languages:
        return True
    key = (language or "").casefold()
    return key not in {"", "unknown"} and key in {name.casefold() for name in preferences.languages}


def candidate_rejection(candidate: Candidate, preferences: ScoutPreferences) -> str | None:
    """Use resolved repository and final paid classification before selection limits."""
    if repository_excluded(candidate["repo"], preferences):
        return "repository excluded by configuration"
    if not (preferences.paid if candidate["paid"] else preferences.strategic):
        return "final candidate lane disabled by configuration"
    if not language_accepted(candidate["language"], preferences):
        return "repository language excluded by configuration"
    return None
