"""Pure source and final-candidate preference controls."""

from collections.abc import Sequence

from opportunity_scout.preferences import ScoutPreferences
from opportunity_scout.types import Candidate, EffortBucket


def repository_excluded(repository: str | None, preferences: ScoutPreferences) -> bool:
    return repository is not None and repository.casefold() in {
        name.casefold() for name in preferences.exclude_repositories
    }


def strategic_repositories(defaults: Sequence[str], preferences: ScoutPreferences) -> list[str]:
    """Add configured strategic sources in order, deduplicating with exclusions first."""
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


def effort_accepted(effort: EffortBucket, allowed: Sequence[EffortBucket]) -> bool:
    """Match an existing final estimate exactly; an empty list accepts nothing."""
    return effort in allowed


def score_rejection(
    candidate: Candidate, *, min_cash_score: int, min_career_score: int
) -> str | None:
    """Apply an inclusive threshold using refreshed scores and final classification."""
    if candidate["paid"]:
        score, minimum, metric, lane = candidate["cash_score"], min_cash_score, "cash", "paid"
    else:
        score, minimum, metric, lane = (
            candidate["career_score"],
            min_career_score,
            "career",
            "strategic",
        )
    if score < minimum:
        return f"{metric} score {score}/100 below {lane} threshold {minimum}/100"
    return None


def candidate_rejection(candidate: Candidate, preferences: ScoutPreferences) -> str | None:
    """Use resolved metadata, final classification, effort and scores before limits."""
    if repository_excluded(candidate["repo"], preferences):
        return "repository excluded by configuration"
    if not (preferences.paid if candidate["paid"] else preferences.strategic):
        return "final candidate lane disabled by configuration"
    if not language_accepted(candidate["language"], preferences):
        return "repository language excluded by configuration"
    if not effort_accepted(candidate["effort"], preferences.effort):
        return "final effort estimate excluded by configuration"
    return score_rejection(
        candidate,
        min_cash_score=preferences.min_cash_score,
        min_career_score=preferences.min_career_score,
    )
