from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Iterable, Literal, Mapping, TypedDict

from bountyscout.types import IssueLifecycleStatus

StateVersion = Literal[2]
STATE_VERSION: Final[StateVersion] = 2
DEFAULT_STATE_FILE: Final = Path("seen_bounties.json")
SEEN_STATE_REVALIDATION_LIMIT: Final = 20
SEEN_STATE_RECHECK_INTERVAL: Final = timedelta(days=30)
_GITHUB_ISSUE_URL_RE: Final = re.compile(r"^https://github\.com/[^/]+/[^/]+/issues/\d+$")


class SeenEntryDocument(TypedDict):
    reported_at: str | None
    last_checked_at: str | None


class SeenStateDocument(TypedDict):
    version: StateVersion
    seen: dict[str, SeenEntryDocument]


@dataclass(frozen=True)
class SeenEntry:
    reported_at: str | None = None
    last_checked_at: str | None = None


@dataclass(frozen=True)
class SeenStateMaintenanceResult:
    state: SeenState
    checked_urls: tuple[str, ...]
    pruned_urls: tuple[str, ...]


class SeenStateLoadError(Exception):
    """Raised when an existing seen-state file cannot be loaded safely."""


class SeenStateSaveError(Exception):
    """Raised when seen-state cannot be persisted safely."""


class SeenState:
    """Schema-independent logical view of previously reported opportunity URLs."""

    def __init__(self, entries: Mapping[str, SeenEntry] | None = None) -> None:
        self._entries = dict(entries) if entries is not None else {}

    @classmethod
    def from_urls(cls, urls: Iterable[str]) -> SeenState:
        state = cls()
        state.mark_reported_many(urls)
        return state

    def contains(self, url: str) -> bool:
        return url in self._entries

    def __contains__(self, url: object) -> bool:
        return url in self._entries

    def urls(self) -> set[str]:
        return set(self._entries)

    def record(self, url: str) -> SeenEntry | None:
        return self._entries.get(url)

    def copy(self) -> SeenState:
        return SeenState(self._entries)

    def remove(self, url: str) -> bool:
        return self._entries.pop(url, None) is not None

    def mark_checked(self, url: str, *, checked_at: str) -> bool:
        entry = self._entries.get(url)
        if entry is None:
            return False
        self._entries[url] = SeenEntry(
            reported_at=entry.reported_at,
            last_checked_at=checked_at,
        )
        return True

    def mark_reported(self, url: str, *, reported_at: str | None = None) -> None:
        if url in self._entries:
            return
        self._entries[url] = SeenEntry(reported_at=reported_at)

    def mark_reported_many(
        self,
        urls: Iterable[str],
        *,
        reported_at: str | None = None,
    ) -> None:
        for url in urls:
            self.mark_reported(url, reported_at=reported_at)

    def to_document(self) -> SeenStateDocument:
        seen: dict[str, SeenEntryDocument] = {}
        for url in sorted(self._entries):
            entry = self._entries[url]
            seen[url] = {
                "reported_at": entry.reported_at,
                "last_checked_at": entry.last_checked_at,
            }
        return {"version": STATE_VERSION, "seen": seen}


def _timestamp_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _state_timestamp(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat().replace("+00:00", "Z")


def _github_issue_url(url: str) -> bool:
    return _GITHUB_ISSUE_URL_RE.fullmatch(url) is not None


def eligible_for_revalidation(
    entry: SeenEntry,
    now: datetime,
    interval: timedelta = SEEN_STATE_RECHECK_INTERVAL,
) -> bool:
    """Return whether a seen entry is old or unknown enough for lifecycle maintenance."""
    if entry.last_checked_at is None:
        return True
    return now - _timestamp_datetime(entry.last_checked_at) >= interval


def select_revalidation_batch(
    state: SeenState,
    now: datetime,
    limit: int = SEEN_STATE_REVALIDATION_LIMIT,
    interval: timedelta = SEEN_STATE_RECHECK_INTERVAL,
) -> list[str]:
    """Select a deterministic bounded batch of stale canonical GitHub issue URLs."""
    if limit <= 0:
        return []

    eligible = [
        (url, entry)
        for url, entry in state._entries.items()
        if _github_issue_url(url) and eligible_for_revalidation(entry, now, interval)
    ]

    def sort_key(item: tuple[str, SeenEntry]) -> tuple[int, datetime, str]:
        url, entry = item
        if entry.last_checked_at is None:
            return (0, datetime.min.replace(tzinfo=timezone.utc), url)
        return (1, _timestamp_datetime(entry.last_checked_at).astimezone(timezone.utc), url)

    eligible.sort(key=sort_key)
    return [url for url, _ in eligible[:limit]]


def apply_revalidation_result(
    state: SeenState,
    url: str,
    status: IssueLifecycleStatus,
    checked_at: datetime,
) -> None:
    """Apply one lifecycle outcome, pruning only confirmed closed GitHub issues."""
    if status == "closed":
        state.remove(url)
        return
    state.mark_checked(url, checked_at=_state_timestamp(checked_at))


def maintain_seen_state(
    state: SeenState,
    now: datetime,
    checker: Callable[[str], IssueLifecycleStatus],
    *,
    limit: int = SEEN_STATE_REVALIDATION_LIMIT,
    interval: timedelta = SEEN_STATE_RECHECK_INTERVAL,
) -> SeenStateMaintenanceResult:
    """Return a maintained copy after a bounded lifecycle pass.

    A checker exception is treated as a failed lifecycle attempt: the URL stays seen
    and its check timestamp advances so one failure cannot monopolize later batches.
    """
    maintained = state.copy()
    batch = select_revalidation_batch(state, now, limit, interval)
    pruned: list[str] = []
    for url in batch:
        try:
            status = checker(url)
        except Exception:
            status = "failed"
        if status == "closed":
            pruned.append(url)
        apply_revalidation_result(maintained, url, status, now)
    return SeenStateMaintenanceResult(
        state=maintained,
        checked_urls=tuple(batch),
        pruned_urls=tuple(pruned),
    )


def _validated_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SeenStateLoadError("Seen-state URL keys must be non-empty strings.")
    return value


def _validated_timestamp(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SeenStateLoadError(f"{field_name} must be an ISO timestamp string or null.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SeenStateLoadError(f"{field_name} must be a valid ISO timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SeenStateLoadError(f"{field_name} must include a timezone.")
    return value


def _parse_legacy_state(raw: list[object]) -> SeenState:
    entries: dict[str, SeenEntry] = {}
    for value in raw:
        entries[_validated_url(value)] = SeenEntry()
    return SeenState(entries)


def _parse_current_state(raw: dict[object, object]) -> SeenState:
    if "version" not in raw:
        raise SeenStateLoadError("Versioned seen-state is missing the version field.")

    version = raw["version"]
    if type(version) is not int:
        raise SeenStateLoadError("Seen-state version must be an integer.")
    if version != STATE_VERSION:
        raise SeenStateLoadError(f"Unsupported seen-state version: {version}.")
    if set(raw) != {"version", "seen"}:
        raise SeenStateLoadError("Versioned seen-state has unexpected top-level fields.")

    seen_raw = raw["seen"]
    if not isinstance(seen_raw, dict):
        raise SeenStateLoadError("Seen-state 'seen' field must be an object.")

    entries: dict[str, SeenEntry] = {}
    for raw_url, raw_entry in seen_raw.items():
        url = _validated_url(raw_url)
        if not isinstance(raw_entry, dict):
            raise SeenStateLoadError(f"Seen-state entry for {url} must be an object.")
        if set(raw_entry) != {"reported_at", "last_checked_at"}:
            raise SeenStateLoadError(f"Seen-state entry for {url} has malformed fields.")
        entries[url] = SeenEntry(
            reported_at=_validated_timestamp(raw_entry["reported_at"], "reported_at"),
            last_checked_at=_validated_timestamp(raw_entry["last_checked_at"], "last_checked_at"),
        )

    return SeenState(entries)


def parse_seen_state(raw: object) -> SeenState:
    """Parse legacy or current state without weakening malformed-state safety."""
    if isinstance(raw, list):
        return _parse_legacy_state(raw)
    if isinstance(raw, dict):
        return _parse_current_state(raw)
    raise SeenStateLoadError("Seen-state top level must be a legacy list or versioned object.")


def load_seen_state(path: str | Path = DEFAULT_STATE_FILE) -> SeenState:
    """Load seen-state, treating only a genuinely absent file as empty first-run state."""
    state_path = Path(path)
    try:
        raw_text = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SeenState()
    except OSError as exc:
        raise SeenStateLoadError(f"Could not read seen-state file {state_path}: {exc}") from exc

    try:
        raw: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SeenStateLoadError(f"Malformed JSON in seen-state file {state_path}.") from exc
    return parse_seen_state(raw)


def save_seen_state(state: SeenState, path: str | Path = DEFAULT_STATE_FILE) -> None:
    """Atomically save the current versioned schema with deterministic formatting."""
    state_path = Path(path)
    serialized = json.dumps(state.to_document(), indent=2, ensure_ascii=False) + "\n"
    try:
        with tempfile.TemporaryDirectory(
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
        ) as temp_dir:
            temp_path = Path(temp_dir) / state_path.name
            temp_path.write_text(serialized, encoding="utf-8", newline="\n")
            os.replace(temp_path, state_path)
    except OSError as exc:
        raise SeenStateSaveError(f"Could not save seen-state file {state_path}: {exc}") from exc
