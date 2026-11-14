"""Result and statistics of status requests to a destination."""

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# Error code of the hub: the radio channel is occupied and nothing was sent.
ERROR_RADIO_BUSY = 0x50005

# Seconds that the recent success rate looks back.
DEFAULT_STATS_WINDOW = 24 * 60 * 60.0


class RefreshResult(StrEnum):
    """Outcome of one status refresh of a destination."""

    OK = "ok"
    BUSY = "busy"
    EMPTY = "empty"
    ERROR = "error"


class RefreshCause(StrEnum):
    """Reason why the status of a destination was requested."""

    SCHEDULED = "scheduled"
    COMMAND = "command"


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
    last_duration: float | None = None
    window: float = DEFAULT_STATS_WINDOW
    causes: dict[RefreshCause, dict[RefreshResult, int]] = field(
        default_factory=dict, repr=False
    )
    recent: deque[tuple[float, bool]] = field(default_factory=deque, repr=False)
    failing_since: float | None = field(default=None, repr=False)
    stale_reported: bool = field(default=False, repr=False)

    def record(
        self,
        result: RefreshResult,
        *,
        now: float,
        stale_after: float,
        cause: RefreshCause = RefreshCause.COMMAND,
        duration: float | None = None,
    ) -> StaleChange | None:
        """Count a result and report when the silent state changes.

        now is a monotonic time in seconds. A destination is stale when its
        refreshes have failed continuously for stale_after seconds. duration
        is the time in seconds that the hub took to answer, None without an
        answer.
        """
        setattr(self, result.value, getattr(self, result.value) + 1)
        self.last_result = result
        self.last_duration = duration
        by_result = self.causes.setdefault(cause, {})
        by_result[result] = by_result.get(result, 0) + 1
        self.recent.append((now, result is RefreshResult.OK))
        while self.recent[0][0] < now - self.window:
            self.recent.popleft()

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

    @property
    def success_rate(self) -> float | None:
        """Return the share of refreshes with data in percent, None without one."""
        total = self.ok + self.busy + self.empty + self.error
        return 100 * self.ok / total if total else None

    @property
    def recent_success_rate(self) -> float | None:
        """Return the share of refreshes with data within the window in percent.

        None without a refresh in the last window seconds.
        """
        start = time.monotonic() - self.window
        results = [ok for at, ok in self.recent if at >= start]
        return 100 * sum(results) / len(results) if results else None

    @classmethod
    def combined(cls, stats: Iterable[RefreshStats]) -> RefreshStats:
        """Return the sum of the counters and the latest refresh with data.

        The answer time belongs to one destination and is not combined.
        """
        total = cls()
        for part in stats:
            total.window = part.window
            total.recent.extend(part.recent)
            for cause, by_result in part.causes.items():
                counts = total.causes.setdefault(cause, {})
                for result, count in by_result.items():
                    counts[result] = counts.get(result, 0) + count
            total.ok += part.ok
            total.busy += part.busy
            total.empty += part.empty
            total.error += part.error
            if part.last_ok and (total.last_ok is None or part.last_ok > total.last_ok):
                total.last_ok = part.last_ok
        return total

    def snapshot(self) -> RefreshStats:
        """Return a copy that does not change with later refreshes."""
        return replace(
            self,
            causes={cause: dict(counts) for cause, counts in self.causes.items()},
            recent=deque(self.recent),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the counters for diagnostics."""
        return {
            "ok": self.ok,
            "busy": self.busy,
            "empty": self.empty,
            "error": self.error,
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "last_result": self.last_result.value if self.last_result else None,
            "last_duration": self.last_duration,
            "recent_success_rate": self.recent_success_rate,
            "causes": {
                cause.value: {result.value: count for result, count in counts.items()}
                for cause, counts in self.causes.items()
            },
        }
