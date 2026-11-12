import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from types import MappingProxyType
from typing import Any

import aiohttp

from .action import Action
from .const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_animationType,
    WMS_WebControl_pro_API_drivingCause,
)
from .refresh import (
    ERROR_RADIO_BUSY,
    RefreshOutcome,
    RefreshResult,
    RefreshStats,
    StaleChange,
)
from .room import Room
from .store import DestinationSettings, DestinationStore, TravelDirection
from .travel import CoverModel, Measurement

DEFAULT_REFRESH_RETRIES = 2
SETTINGS_FILE = "settings.json"

_LOGGER = logging.getLogger(__name__)


class Destination:
    def __init__(
        self, control, id: int, names: list, actions: list, animationType: int
    ) -> None:
        self._control = control
        self._id = id
        self._names = names
        self._persist = (control._persist / str(id)) if control._persist else None
        self._actions = {action["id"]: Action(self, **action) for action in actions}
        self._animationType = WMS_WebControl_pro_API_animationType(animationType)
        self._drivingCause = WMS_WebControl_pro_API_drivingCause.Unknown
        self._unknownProducts = {}
        self._heartbeatError = None
        self._blocking = None
        self._status = {}
        self._stats = RefreshStats()
        self._store = DestinationStore(
            self._persist / SETTINGS_FILE if self._persist else None
        )
        self._settings = DestinationSettings()
        self._saved_state: dict[str, Any] | None = None
        self._loaded = False
        self.model = CoverModel()

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Destination {self.id}: {self}>"

    def __eq__(self, other) -> bool:
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # --- Properties ---

    @property
    def host(self) -> str:
        return self._control.host

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self._names[0]

    @property
    def actions(self) -> dict:
        return self._actions

    @property
    def animationType(self) -> WMS_WebControl_pro_API_animationType:
        return self._animationType

    @property
    def drivingCause(self) -> WMS_WebControl_pro_API_drivingCause:
        return self._drivingCause

    @property
    def room(self) -> Room | None:
        for room in self._control.rooms.values():
            if self._id in room._destination_ids:
                return room
        return None

    @property
    def available(self) -> bool:
        return not (self._heartbeatError or self._blocking)

    @property
    def status(self) -> Mapping:
        return MappingProxyType(self._status)

    @property
    def stats(self) -> RefreshStats:
        """Return a snapshot of the refresh counters of this destination."""
        return self._stats.snapshot()

    # --- Public methods ---

    @property
    def settings(self) -> DestinationSettings:
        """Return the settings of this destination."""
        return self._settings

    async def set_travel_time(self, direction: TravelDirection, seconds: float) -> None:
        """Store the travel time of a full drive in seconds, 0 means unknown."""
        if not seconds >= 0:
            raise ValueError(f"Travel time must not be negative: {seconds}")
        await self._ensure_loaded()
        self._settings = replace(
            self._settings, **{f"travel_time_{direction.value}": float(seconds)}
        )
        await self._save()

    def start_move(self, now: float, target: float, tilt: int | None = None) -> float:
        """Handle an accepted drive command with the stored travel times.

        Returns the delay in seconds until the settle poll.
        """
        return self.model.start_move(
            now,
            target,
            self._settings.travel_time(TravelDirection.OPEN),
            self._settings.travel_time(TravelDirection.CLOSE),
            tilt,
        )

    def apply_measurement(
        self, now: float, position: int | None, tilt: int | None
    ) -> Measurement:
        """Take over a measured position unless a drive is running."""
        return self.model.apply_measurement(now, position, tilt)

    async def sync(self) -> None:
        """Load stored data once and store changes of settings and state.

        Call this after commands and measurements. refresh calls it as well.
        """
        for action in self._actions.values():
            await action.sync()
        await self._ensure_loaded()
        if self.model.snapshot() != self._saved_state:
            await self._save()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        data = await self._store.load()
        self._settings = DestinationSettings.from_dict(data.get("settings"))
        state = data.get("state")
        if isinstance(state, dict):
            self.model.restore(
                state.get("position"),
                state.get("tilt"),
                state.get("target_position"),
                state.get("target_tilt"),
            )
        self._saved_state = self.model.snapshot()

    async def _save(self) -> None:
        state = self.model.snapshot()
        await self._store.save(self._settings, state)
        self._saved_state = state

    async def refresh(
        self, retries: int = DEFAULT_REFRESH_RETRIES, *, raise_on_error: bool = False
    ) -> RefreshOutcome:
        """Request the status once and retry up to retries times if radio busy.

        With retries=0 exactly one request is sent. On any failure the
        previously known values stay unchanged. The outcome is counted in
        stats instead of being logged for every failure.
        """
        await self.sync()

        outcome = await self._request_status(retries, raise_on_error)
        self._record(outcome.result)
        return outcome

    async def _request_status(
        self, retries: int, raise_on_error: bool
    ) -> RefreshOutcome:
        status: dict = {}
        for attempt in range(retries + 1):
            try:
                status = await self._control._getStatus(self._id)
            except aiohttp.ClientError, TimeoutError:
                if raise_on_error:
                    raise
                return RefreshOutcome(RefreshResult.ERROR)
            if ERROR_RADIO_BUSY not in ((status or {}).get("errors") or ()):
                break
            if attempt < retries:
                await asyncio.sleep(self._control.retry_delay)
        else:
            return RefreshOutcome(RefreshResult.BUSY)

        if not status:
            return RefreshOutcome(RefreshResult.EMPTY)

        self._status = status
        values = self._apply_status(status)
        if values is None:
            return RefreshOutcome(RefreshResult.EMPTY)
        return RefreshOutcome(RefreshResult.OK, values)

    def _apply_status(self, status: dict) -> dict[int, dict[str, Any]] | None:
        values: dict[int, dict[str, Any]] = {}
        refreshed = False
        for detail in status.get("details") or ():
            if detail.get("destinationId") != self._id or "data" not in detail:
                continue

            refreshed = True
            data = detail["data"]
            if "drivingCause" in data:
                self._drivingCause = WMS_WebControl_pro_API_drivingCause(
                    data["drivingCause"]
                )
            if "heartbeatError" in data:
                self._heartbeatError = data["heartbeatError"]
            if "blocking" in data:
                self._blocking = data["blocking"]

            for product in data.get("productData", []):
                action_id = product["actionId"]
                if action_id in self._actions and "value" in product:
                    self._actions[action_id]._update_params(product["value"])
                    values[action_id] = dict(product["value"])
                    continue

                _LOGGER.debug(
                    "Unknown actionId %s in productData for %s (%s)",
                    action_id,
                    self,
                    self._id,
                )
                self._unknownProducts[action_id] = product

        return values if refreshed else None

    def _record(self, result: RefreshResult) -> None:
        change = self._stats.record(
            result,
            now=time.monotonic(),
            stale_after=self._control.stale_warning_after.total_seconds(),
        )
        if change is StaleChange.STALE:
            _LOGGER.warning(
                "No status for %s (%s) for %s, last result: %s",
                self,
                self._id,
                self._control.stale_warning_after,
                result.value,
            )
        elif change is StaleChange.RECOVERED:
            _LOGGER.warning("Status for %s (%s) is available again", self, self._id)

    def hasAction(
        self,
        actionDescription: WMS_WebControl_pro_API_actionDescription,
        actionType: WMS_WebControl_pro_API_actionType | None = None,
    ) -> bool:
        for action in self._actions.values():
            if action.actionDescription == actionDescription and (
                actionType is None or actionType == action.actionType
            ):
                return True
        return False

    def action(
        self,
        actionDescription: WMS_WebControl_pro_API_actionDescription,
        actionType: WMS_WebControl_pro_API_actionType | None = None,
    ) -> Action:
        for action in self._actions.values():
            if action.actionDescription == actionDescription and (
                actionType is None or actionType == action.actionType
            ):
                return action
        description = actionDescription.name if actionDescription else "*"
        type_name = actionType.name if actionType else "*"
        raise NotImplementedError(
            f"Failed to find action with description {description} "
            f"and type {type_name} in {self} ({self._id})"
        )

    def diag(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "room": {self.room.id: self.room.name} if self.room else None,
            "actions": {k: v.diag() for k, v in self._actions.items()},
            "animationType": self.animationType.name,
            "drivingCause": self.drivingCause.name,
            "available": self.available,
            "unknownProducts": self._unknownProducts,
            "heartbeatError": self._heartbeatError,
            "blocking": self._blocking,
            "status": self._status,
            "stats": self._stats.as_dict(),
            "settings": asdict(self._settings),
            "model": {
                **self.model.snapshot(),
                "source": self.model.source.value if self.model.source else None,
            },
        }
