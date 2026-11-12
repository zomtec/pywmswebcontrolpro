"""Persistent settings and last known state of one destination."""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiofiles
from aiofiles.os import makedirs, path as aio_path, replace

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1


class TravelDirection(StrEnum):
    """Direction of a full drive of a cover."""

    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class DestinationSettings:
    """Settings of a destination that the user can change.

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
    def from_dict(cls, data: Any) -> DestinationSettings:
        """Build settings from stored data, invalid values become 0."""
        if not isinstance(data, dict):
            return cls()
        return cls(**{f.name: _valid_seconds(data.get(f.name)) for f in fields(cls)})


def _valid_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if value >= 0 else 0.0


class DestinationStore:
    """JSON file with the settings and the last state of one destination.

    Reads and writes are serialized and a write replaces the file atomically.
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
            return await self._read()

    async def _read(self) -> dict[str, Any]:
        assert self._path is not None
        if not await aio_path.exists(self._path):
            return {}
        async with aiofiles.open(self._path) as file:
            content = await file.read()
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            _LOGGER.warning("Ignoring unreadable settings file %s", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    async def save(self, settings: DestinationSettings, state: dict[str, Any]) -> None:
        """Replace the file with the given settings and state."""
        if self._path is None:
            return
        content = json.dumps(
            {
                "version": STORE_VERSION,
                "settings": asdict(settings),
                "state": state,
            }
        )
        temp = self._path.with_name(self._path.name + ".tmp")
        async with self._lock:
            await makedirs(self._path.parent, exist_ok=True)
            async with aiofiles.open(temp, mode="w") as file:
                await file.write(content)
            await replace(temp, self._path)
