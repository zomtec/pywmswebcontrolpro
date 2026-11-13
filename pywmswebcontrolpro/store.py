"""Persistent settings and last known state of the actions of one hub."""

import asyncio
import contextlib
import copy
import json
import logging
import os
import tempfile
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

type _Entries = dict[str, dict[str, dict[str, Any]]]


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
    destinations = data.get("destinations")
    if not isinstance(destinations, dict):
        return {}
    entries = {
        dest_id: {
            action_id: entry
            for action_id, entry in actions.items()
            if isinstance(entry, dict) and entry
        }
        for dest_id, actions in destinations.items()
        if isinstance(actions, dict)
    }
    return {dest_id: actions for dest_id, actions in entries.items() if actions}


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


class _Document:
    """Content of one file, an entry for each action that has data."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.entries: _Entries = {}
        self.dirty = False

    def get(self, dest_id: int, action_id: int) -> dict[str, Any]:
        """Return a copy of the entry of an action, empty if there is none."""
        entry = self.entries.get(str(dest_id), {}).get(str(action_id), {})
        return copy.deepcopy(entry)

    def put(self, dest_id: int, action_id: int, entry: dict[str, Any]) -> None:
        """Take over the entry of an action, an empty entry removes it."""
        if entry == self.get(dest_id, action_id):
            return
        self.dirty = True
        if entry:
            actions = self.entries.setdefault(str(dest_id), {})
            actions[str(action_id)] = copy.deepcopy(entry)
            return
        actions = self.entries[str(dest_id)]
        del actions[str(action_id)]
        if not actions:
            del self.entries[str(dest_id)]

    def dumps(self) -> str:
        """Return the content of the file."""
        return json.dumps({"version": STORE_VERSION, "destinations": self.entries})


class HubStore:
    """Stored data of all actions of one hub and the only place that writes it.

    The settings, which change rarely and cannot be measured again, and the
    last known state, which changes with every drive, are kept in two files of
    the given directory. Both are read once. An action hands its data over and
    reads what was stored, only data that differs from the defaults gets an
    entry. Writes are serialized and replace a file atomically. A file that
    cannot be read or written is logged and does not raise. Without a
    directory nothing is stored.
    """

    def __init__(self, directory: Path | None) -> None:
        """Create the store for the files in the given directory."""
        self._settings = _Document(directory / SETTINGS_FILE if directory else None)
        self._state = _Document(directory / STATE_FILE if directory else None)
        self._lock = asyncio.Lock()
        self._loaded = False

    async def load(self) -> None:
        """Read both files once, further calls do nothing."""
        async with self._lock:
            if self._loaded:
                return
            for document in (self._settings, self._state):
                if document.path is not None:
                    document.entries = await _read(document.path)
            self._loaded = True

    def settings(self, dest_id: int, action_id: int) -> dict[str, Any]:
        """Return the stored settings of an action, empty if there are none."""
        return self._settings.get(dest_id, action_id)

    def state(self, dest_id: int, action_id: int) -> dict[str, Any]:
        """Return the stored state of an action, empty if there is none."""
        return self._state.get(dest_id, action_id)

    def set_settings(
        self, dest_id: int, action_id: int, settings: dict[str, Any]
    ) -> None:
        """Take over the settings of an action for the next write."""
        self._settings.put(dest_id, action_id, settings)

    def set_state(self, dest_id: int, action_id: int, state: dict[str, Any]) -> None:
        """Take over the state of an action for the next write."""
        self._state.put(dest_id, action_id, state)

    async def flush(self) -> bool:
        """Write the files that have changes, return False if a write failed.

        A failed write is tried again with the next call.
        """
        written = True
        async with self._lock:
            for document in (self._settings, self._state):
                if not document.dirty:
                    continue
                # Cleared before the write, so a change made during it is
                # written by the next call.
                document.dirty = False
                if document.path is None:
                    continue
                try:
                    await asyncio.to_thread(_write, document.path, document.dumps())
                except OSError as err:
                    _LOGGER.warning("Could not write %s: %r", document.path, err)
                    document.dirty = True
                    written = False
        return written
