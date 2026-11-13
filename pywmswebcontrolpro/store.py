"""Persistent settings and last known state of the actions of one hub."""

import asyncio
import contextlib
import copy
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiofiles
from aiofiles.os import path as aio_path

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SETTINGS_FILE = "settings.json"
STATE_FILE = "state.json"

type _Entries = dict[str, dict[str, Any]]


class TravelDirection(StrEnum):
    """Direction of a full drive of a cover."""

    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class ActionSettings:
    """Settings of a drive action that the user can change.

    A travel time of 0 means unknown, the target of a drive is then taken over
    at once.
    """

    travel_time_open: float = 0.0
    travel_time_close: float = 0.0

    def travel_time(self, direction: TravelDirection) -> float:
        """Return the travel time of a full drive in seconds."""
        if direction is TravelDirection.OPEN:
            return self.travel_time_open
        return self.travel_time_close

    @classmethod
    def from_dict(cls, data: Any) -> ActionSettings:
        """Build settings from stored data, invalid values become 0."""
        if not isinstance(data, dict):
            return cls()
        return cls(**{f.name: _valid_seconds(data.get(f.name)) for f in fields(cls)})


def valid_percent(value: Any) -> float | None:
    """Return a stored position or tilt, None if it is no number from 0 to 100."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if 0 <= value <= 100 else None


def _valid_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if value >= 0 else 0.0


def _valid_entries(path: Path, data: Any) -> _Entries:
    """Return the entries of a file, without everything that has no content."""
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        _LOGGER.warning("Ignoring file %s with an unknown format", path)
        return {}
    actions = data.get("actions")
    if not isinstance(actions, dict):
        return {}
    return {
        action_id: entry
        for action_id, entry in actions.items()
        if isinstance(entry, dict) and entry
    }


async def _read(path: Path) -> _Entries:
    if not await aio_path.exists(path):
        return {}
    try:
        async with aiofiles.open(path, encoding="utf-8") as file:
            content = await file.read()
        data = json.loads(content)
    except (OSError, ValueError) as err:
        _LOGGER.warning("Ignoring unreadable file %s: %r", path, err)
        return {}
    return _valid_entries(path, data)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A temporary file of its own and a sync to disk keep the old file
    # intact if the write is interrupted.
    descriptor, temp = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)
    # The directory of a destination only exists while it has a file.
    with contextlib.suppress(OSError):
        path.parent.rmdir()


class _Document:
    """Content of one file, an entry for each action that has data."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.entries: _Entries = {}
        self.dirty = False

    def get(self, action_id: int) -> dict[str, Any]:
        """Return a copy of the entry of an action, empty if there is none."""
        return copy.deepcopy(self.entries.get(str(action_id), {}))

    def put(self, action_id: int, entry: dict[str, Any]) -> None:
        """Take over the entry of an action, an empty entry removes it."""
        if entry == self.get(action_id):
            return
        self.dirty = True
        if entry:
            self.entries[str(action_id)] = copy.deepcopy(entry)
        else:
            del self.entries[str(action_id)]

    def store(self) -> None:
        """Write the file, a document without entries has no file."""
        if self.path is None:
            return
        if not self.entries:
            _remove(self.path)
            return
        content = json.dumps({"version": STORE_VERSION, "actions": self.entries})
        _write(self.path, content)


class _DestinationData:
    """Both documents of one destination."""

    def __init__(self, directory: Path | None) -> None:
        self.settings = _Document(directory / SETTINGS_FILE if directory else None)
        self.state = _Document(directory / STATE_FILE if directory else None)
        self.loaded = False

    @property
    def documents(self) -> tuple[_Document, _Document]:
        """Return the settings and the state."""
        return (self.settings, self.state)


class HubStore:
    """Stored data of all actions of one hub and the only place that writes it.

    Each destination has a directory of its own, named by its id, in the given
    directory. The settings, which change rarely and cannot be measured again,
    and the last known state, which changes with every drive, are kept in two
    files of it. They are read once. An action hands its data over and reads
    what was stored, only data that differs from the defaults gets an entry
    and only a destination with entries has a file. Writes are serialized and
    replace a file atomically. A file that cannot be read or written is logged
    and does not raise. Without a directory nothing is stored.
    """

    def __init__(self, directory: Path | None) -> None:
        """Create the store for the files below the given directory."""
        self._directory = directory
        self._destinations: dict[int, _DestinationData] = {}
        self._lock = asyncio.Lock()

    def _destination(self, dest_id: int) -> _DestinationData:
        if dest_id not in self._destinations:
            directory = self._directory / str(dest_id) if self._directory else None
            self._destinations[dest_id] = _DestinationData(directory)
        return self._destinations[dest_id]

    async def load(self, dest_ids: Iterable[int]) -> None:
        """Read the files of the given destinations, those of each only once."""
        async with self._lock:
            for dest_id in dest_ids:
                destination = self._destination(dest_id)
                if destination.loaded:
                    continue
                for document in destination.documents:
                    if document.path is not None:
                        document.entries = await _read(document.path)
                destination.loaded = True

    def settings(self, dest_id: int, action_id: int) -> dict[str, Any]:
        """Return the stored settings of an action, empty if there are none."""
        return self._destination(dest_id).settings.get(action_id)

    def state(self, dest_id: int, action_id: int) -> dict[str, Any]:
        """Return the stored state of an action, empty if there is none."""
        return self._destination(dest_id).state.get(action_id)

    def set_settings(
        self, dest_id: int, action_id: int, settings: dict[str, Any]
    ) -> None:
        """Take over the settings of an action for the next write."""
        self._destination(dest_id).settings.put(action_id, settings)

    def set_state(self, dest_id: int, action_id: int, state: dict[str, Any]) -> None:
        """Take over the state of an action for the next write."""
        self._destination(dest_id).state.put(action_id, state)

    async def flush(self) -> bool:
        """Write the files that have changes, return False if a write failed.

        A failed write is tried again with the next call.
        """
        written = True
        async with self._lock:
            for destination in list(self._destinations.values()):
                for document in destination.documents:
                    if not document.dirty:
                        continue
                    # Cleared before the write, so a change made during it is
                    # written by the next call.
                    document.dirty = False
                    try:
                        await asyncio.to_thread(document.store)
                    except OSError as err:
                        _LOGGER.warning("Could not write %s: %r", document.path, err)
                        document.dirty = True
                        written = False
        return written
