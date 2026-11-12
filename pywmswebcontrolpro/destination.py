"""A destination of the gateway, for example a drive, a light or a load."""

import asyncio
import logging
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .action import Action
from .const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_animationType,
    WMS_WebControl_pro_API_drivingCause,
)
from .exceptions import WebControlProError
from .refresh import (
    ERROR_RADIO_BUSY,
    RefreshOutcome,
    RefreshResult,
    RefreshStats,
    StaleChange,
)
from .room import Room

if TYPE_CHECKING:
    from .webcontrol import WebControlPro

# A busy radio channel is not asked again, every request adds radio traffic.
DEFAULT_REFRESH_RETRIES = 0

_LOGGER = logging.getLogger(__name__)


class Destination:
    """A component of the gateway with its actions and the last known status."""

    def __init__(
        self,
        control: WebControlPro,
        id: int,
        names: list[str],
        actions: list[dict[str, Any]],
        animationType: int,
        **kwargs: Any,
    ) -> None:
        self._control = control
        # The id becomes part of a file path, so it must be a number.
        self._id = int(id)
        self._names = names
        self._persist = (control._persist / str(self._id)) if control._persist else None
        self._actions = {action["id"]: Action(self, **action) for action in actions}
        self._animationType = WMS_WebControl_pro_API_animationType(animationType)
        self._drivingCause = WMS_WebControl_pro_API_drivingCause.Unknown
        self._unknownProducts: dict[int, Any] = {}
        self._heartbeatError = None
        self._blocking = None
        self._status: dict[str, Any] = {}
        self._stats = RefreshStats()

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Destination {self.id}: {self}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Destination) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # --- Properties ---

    @property
    def host(self) -> str:
        """Return the host of the gateway."""
        return self._control.host

    @property
    def id(self) -> int:
        """Return the id of the destination."""
        return self._id

    @property
    def name(self) -> str:
        """Return the name of the destination."""
        return self._names[0]

    def _rename(self, names: list[str]) -> bool:
        """Take over the names of the configuration, True if the name changed."""
        changed = names[0] != self.name
        self._names = names
        return changed

    @property
    def actions(self) -> dict[int, Action]:
        """Return the actions of the destination by id."""
        return self._actions

    @property
    def animationType(self) -> WMS_WebControl_pro_API_animationType:
        """Return the kind of device that the destination controls."""
        return self._animationType

    @property
    def drivingCause(self) -> WMS_WebControl_pro_API_drivingCause:
        """Return why the drive moved last."""
        return self._drivingCause

    @property
    def room(self) -> Room | None:
        """Return the room of the destination, if it belongs to one."""
        for room in self._control.rooms.values():
            if self._id in room._destination_ids:
                return room
        return None

    @property
    def available(self) -> bool:
        """Return False while the gateway reports an error or a blocked drive."""
        return not (self._heartbeatError or self._blocking)

    @property
    def status(self) -> Mapping[str, Any]:
        """Return the last status that the gateway answered."""
        return MappingProxyType(self._status)

    @property
    def stats(self) -> RefreshStats:
        """Return a snapshot of the refresh counters of this destination."""
        return self._stats.snapshot()

    # --- Public methods ---

    async def sync(self) -> None:
        """Load stored data once and store changes of all actions."""
        for action in self._actions.values():
            await action.sync()

    async def refresh(
        self, retries: int = DEFAULT_REFRESH_RETRIES, *, raise_on_error: bool = False
    ) -> RefreshOutcome:
        """Request the status once and retry up to retries times if radio busy.

        With retries=0 exactly one request is sent. On any failure the
        previously known values stay unchanged. The outcome is counted in
        stats instead of being logged for every failure. With raise_on_error
        a WebControlProError of the request is raised instead of counted.
        """
        await self.sync()

        outcome = await self._request_status(retries, raise_on_error)
        self._record(outcome.result)
        return outcome

    async def _request_status(
        self, retries: int, raise_on_error: bool
    ) -> RefreshOutcome:
        status: dict[str, Any] = {}
        for attempt in range(retries + 1):
            try:
                status = await self._control._getStatus(self._id)
            except WebControlProError:
                if raise_on_error:
                    raise
                return RefreshOutcome(RefreshResult.ERROR)
            if ERROR_RADIO_BUSY not in (status.get("errors") or ()):
                break
            if attempt < retries:
                await asyncio.sleep(self._control.retry_delay)
        else:
            return RefreshOutcome(RefreshResult.BUSY)

        if not status:
            return RefreshOutcome(RefreshResult.EMPTY)

        try:
            values = self._apply_status(status)
        except (AttributeError, KeyError, TypeError) as err:
            _LOGGER.debug("Unexpected status for %s (%s): %r", self, self._id, err)
            return RefreshOutcome(RefreshResult.ERROR)
        if values is None:
            result = (
                RefreshResult.ERROR if status.get("errors") else RefreshResult.EMPTY
            )
            return RefreshOutcome(result)
        self._status = status
        return RefreshOutcome(RefreshResult.OK, values)

    def _apply_status(self, status: dict[str, Any]) -> dict[int, dict[str, Any]] | None:
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
        """Return True if the destination has a matching action."""
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
        """Return the matching action or raise NotImplementedError."""
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

    def diag(self) -> dict[str, Any]:
        """Return the destination for the diagnostics."""
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
        }
