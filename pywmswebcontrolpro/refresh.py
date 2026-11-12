"""Result and statistics of status requests to a destination."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# Error code of the hub: the radio channel is occupied and nothing was sent.
ERROR_RADIO_BUSY = 0x50005


class RefreshResult(StrEnum):
    """Outcome of one status refresh of a destination."""

    OK = "ok"
    BUSY = "busy"
    EMPTY = "empty"
    ERROR = "error"


class StaleChange(StrEnum):
    """Change of the silent state of a destination."""

    STALE = "stale"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of a refresh and the values the hub delivered for each action.

    The outcome is truthy only if the hub delivered data for the destination.
    """

    result: RefreshResult
    values: dict[int, dict[str, Any]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Return True if the hub delivered data for the destination."""
        return self.result is RefreshResult.OK


@dataclass
class RefreshStats:
    """Counters of the refresh outcomes of one destination."""

    ok: int = 0
    busy: int = 0
    empty: int = 0
    error: int = 0
    last_ok: datetime | None = None
    last_result: RefreshResult | None = None
    failing_since: float | None = field(default=None, repr=False)
    stale_reported: bool = field(default=False, repr=False)

    def record(
        self, result: RefreshResult, *, now: float, stale_after: float
    ) -> StaleChange | None:
        """Count a result and report when the silent state changes.

        now is a monotonic time in seconds. A destination is stale when its
        refreshes have failed continuously for stale_after seconds.
        """
        setattr(self, result.value, getattr(self, result.value) + 1)
        self.last_result = result

        if result is RefreshResult.OK:
            self.last_ok = datetime.now(UTC)
            was_stale = self.stale_reported
            self.failing_since = None
            self.stale_reported = False
            return StaleChange.RECOVERED if was_stale else None

        if self.failing_since is None:
            self.failing_since = now
            return None
        if not self.stale_reported and now - self.failing_since >= stale_after:
            self.stale_reported = True
            return StaleChange.STALE
        return None

    def snapshot(self) -> RefreshStats:
        """Return a copy that does not change with later refreshes."""
        return replace(self)

    def as_dict(self) -> dict[str, Any]:
        """Return the counters for diagnostics."""
        return {
            "ok": self.ok,
            "busy": self.busy,
            "empty": self.empty,
            "error": self.error,
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "last_result": self.last_result.value if self.last_result else None,
        }
