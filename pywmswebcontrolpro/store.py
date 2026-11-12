"""Persistent settings and last known state of one destination."""

import asyncio
import contextlib
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


class JsonStore:
    """JSON file with the persistent data of one action.

    Reads and writes are serialized and a write replaces the file atomically.
    A file that cannot be read or written is logged and does not raise.
    Without a path nothing is stored.
    """

    def __init__(self, path: Path | None) -> None:
        """Create a store for the given file."""
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path | None:
        """Return the file of this store."""
        return self._path

    async def load(self) -> dict[str, Any]:
        """Return the stored data, empty if there is none or it is unreadable."""
        if self._path is None:
            return {}
        async with self._lock:
            return await self._read(self._path)

    @staticmethod
    async def _read(path: Path) -> dict[str, Any]:
        if not await aio_path.exists(path):
            return {}
        try:
            async with aiofiles.open(path, encoding="utf-8") as file:
                content = await file.read()
            data = json.loads(content)
        except (OSError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable file %s: %r", path, err)
            return {}
        return data if isinstance(data, dict) else {}

    async def save(self, data: dict[str, Any]) -> bool:
        """Replace the file with the given data, return False if that failed."""
        if self._path is None:
            return True
        content = json.dumps(data)
        async with self._lock:
            try:
                await asyncio.to_thread(self._write, self._path, content)
            except OSError as err:
                _LOGGER.warning("Could not write %s: %r", self._path, err)
                return False
        return True

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A temporary file of its own and a sync to disk keep the old file
        # intact if the write is interrupted or runs twice at the same time.
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
