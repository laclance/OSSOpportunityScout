from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, TypedDict, cast

from opportunity_scout.types import IssueLifecycleStatus

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
    """Logical set of previously reported opportunity URLs and their timestamps."""

    def __init__(self, entries: Mapping[str, SeenEntry] | None = None) -> None:
        self._entries: dict[str, SeenEntry] = dict(entries or {})

    @classmethod
    def from_urls(cls, urls: Iterable[str]) -> SeenState:
        return cls(dict.fromkeys(urls, SeenEntry()))

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
        if url not in self._entries:
            return False
        del self._entries[url]
        return True

    def mark_checked(self, url: str, *, checked_at: str) -> bool:
        current = self._entries.get(url)
        if current is None:
            return False
        self._entries[url] = SeenEntry(current.reported_at, checked_at)
        return True

    def mark_reported(self, url: str, *, reported_at: str | None = None) -> None:
        self._entries.setdefault(url, SeenEntry(reported_at=reported_at))

    def mark_reported_many(
        self,
        urls: Iterable[str],
        *,
        reported_at: str | None = None,
    ) -> None:
        for url in urls:
            self.mark_reported(url, reported_at=reported_at)

    def to_document(self) -> SeenStateDocument:
        ordered_seen = {
            url: SeenEntryDocument(
                reported_at=entry.reported_at,
                last_checked_at=entry.last_checked_at,
            )
            for url, entry in sorted(self._entries.items())
        }
        return SeenStateDocument(version=STATE_VERSION, seen=ordered_seen)

    def _snapshot(self) -> tuple[tuple[str, SeenEntry], ...]:
        return tuple(self._entries.items())


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_github_issue_url(url: str) -> bool:
    return _GITHUB_ISSUE_URL_RE.fullmatch(url) is not None


def eligible_for_revalidation(
    entry: SeenEntry,
    now: datetime,
    interval: timedelta = SEEN_STATE_RECHECK_INTERVAL,
) -> bool:
    """Return whether an entry has never been checked or is stale enough to recheck."""
    if entry.last_checked_at is None:
        return True
    return now - _parse_timestamp(entry.last_checked_at) >= interval


def _revalidation_order(item: tuple[str, SeenEntry]) -> tuple[int, datetime, str]:
    url, entry = item
    checked_at = entry.last_checked_at
    if checked_at is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc), url)
    return (1, _parse_timestamp(checked_at).astimezone(timezone.utc), url)


def select_revalidation_batch(
    state: SeenState,
    now: datetime,
    limit: int = SEEN_STATE_REVALIDATION_LIMIT,
    interval: timedelta = SEEN_STATE_RECHECK_INTERVAL,
) -> list[str]:
    """Choose a deterministic bounded batch of stale canonical GitHub issue URLs."""
    if limit <= 0:
        return []

    candidates = [
        item
        for item in state._snapshot()
        if _is_github_issue_url(item[0]) and eligible_for_revalidation(item[1], now, interval)
    ]
    candidates.sort(key=_revalidation_order)
    return [url for url, _entry in candidates[:limit]]


def apply_revalidation_result(
    state: SeenState,
    url: str,
    status: IssueLifecycleStatus,
    checked_at: datetime,
) -> None:
    """Apply a lifecycle result, pruning only a confirmed closed issue."""
    if status == "closed":
        state.remove(url)
    else:
        state.mark_checked(url, checked_at=_format_timestamp(checked_at))


def maintain_seen_state(
    state: SeenState,
    now: datetime,
    checker: Callable[[str], IssueLifecycleStatus],
    *,
    limit: int = SEEN_STATE_REVALIDATION_LIMIT,
    interval: timedelta = SEEN_STATE_RECHECK_INTERVAL,
) -> SeenStateMaintenanceResult:
    """Return a maintained copy after one bounded lifecycle pass."""
    checked_urls = tuple(select_revalidation_batch(state, now, limit, interval))
    maintained = state.copy()
    pruned_urls: list[str] = []

    for url in checked_urls:
        try:
            status = checker(url)
        except Exception:
            status = "failed"

        if status == "closed":
            pruned_urls.append(url)
        apply_revalidation_result(maintained, url, status, now)

    return SeenStateMaintenanceResult(
        state=maintained,
        checked_urls=checked_urls,
        pruned_urls=tuple(pruned_urls),
    )


def _as_object_dict(value: object, message: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise SeenStateLoadError(message)
    return cast(dict[object, object], value)


def _decode_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SeenStateLoadError("Seen-state URL keys must be non-empty strings.")
    return value


def _decode_timestamp(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SeenStateLoadError(f"{field_name} must be an ISO timestamp string or null.")
    try:
        parsed = _parse_timestamp(value)
    except ValueError as exc:
        raise SeenStateLoadError(f"{field_name} must be a valid ISO timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SeenStateLoadError(f"{field_name} must include a timezone.")
    return value


def _decode_entry(url: str, value: object) -> SeenEntry:
    raw_entry = _as_object_dict(value, f"Seen-state entry for {url} must be an object.")
    if set(raw_entry) != {"reported_at", "last_checked_at"}:
        raise SeenStateLoadError(f"Seen-state entry for {url} has malformed fields.")
    return SeenEntry(
        reported_at=_decode_timestamp(raw_entry["reported_at"], "reported_at"),
        last_checked_at=_decode_timestamp(raw_entry["last_checked_at"], "last_checked_at"),
    )


def _decode_document(raw: dict[object, object]) -> SeenState:
    if "version" not in raw:
        raise SeenStateLoadError("Versioned seen-state is missing the version field.")

    version = raw["version"]
    if type(version) is not int:
        raise SeenStateLoadError("Seen-state version must be an integer.")
    if version != STATE_VERSION:
        raise SeenStateLoadError(f"Unsupported seen-state version: {version}.")
    if set(raw) != {"version", "seen"}:
        raise SeenStateLoadError("Versioned seen-state has unexpected top-level fields.")

    raw_seen = _as_object_dict(raw["seen"], "Seen-state 'seen' field must be an object.")
    entries: dict[str, SeenEntry] = {}
    for raw_url, raw_entry in raw_seen.items():
        url = _decode_url(raw_url)
        entries[url] = _decode_entry(url, raw_entry)
    return SeenState(entries)


def parse_seen_state(raw: object) -> SeenState:
    """Decode and validate the current versioned state schema."""
    if not isinstance(raw, dict):
        raise SeenStateLoadError("Seen-state top level must be a versioned object.")
    return _decode_document(cast(dict[object, object], raw))


def load_seen_state(path: str | Path = DEFAULT_STATE_FILE) -> SeenState:
    """Load state, treating only a missing file as an empty first run."""
    state_path = Path(path)
    try:
        text = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SeenState()
    except OSError as exc:
        raise SeenStateLoadError(f"Could not read seen-state file {state_path}: {exc}") from exc

    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SeenStateLoadError(f"Malformed JSON in seen-state file {state_path}.") from exc
    return parse_seen_state(raw)


def _serialize_state(state: SeenState) -> str:
    return json.dumps(state.to_document(), indent=2, ensure_ascii=False) + "\n"


def _replace_with_text(path: Path, text: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_seen_state(state: SeenState, path: str | Path = DEFAULT_STATE_FILE) -> None:
    """Persist the current versioned schema via an atomic sibling-file replacement."""
    state_path = Path(path)
    try:
        _replace_with_text(state_path, _serialize_state(state))
    except OSError as exc:
        raise SeenStateSaveError(f"Could not save seen-state file {state_path}: {exc}") from exc
