"""An action of a destination with its stored settings and movement model."""

import math
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from .const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_responseType,
)
from .store import STORE_VERSION, ActionSettings, JsonStore, TravelDirection
from .travel import CoverModel, Measurement, rotation_to_tilt, tilt_to_rotation

if TYPE_CHECKING:
    from .destination import Destination
    from .webcontrol import WebControlPro

_STORED_KEYS = frozenset({"version", "overwrites", "settings", "state"})

# Firmware bug of the WMS WebControl pro: the hub reports the rotation limits
# of slat drives as -127 and 127, the drives turn from -75 to 75. Warema knows
# the bug and has not fixed it. While the hub reports exactly these limits,
# -75 and 75 are used instead. A rotation measured beyond them widens the
# range, and the user can overwrite it. The workaround ends by itself when a
# firmware reports other limits and can then be removed.
ROTATION_LIMIT_REPORTED_BY_BUG = 127
ROTATION_LIMIT_DEFAULT = 75
_MIN_VALUE = "minValue"
_MAX_VALUE = "maxValue"


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class ActionList(list):
    """Actions that are sent to the gateway with one request."""

    def __init__(self, control: WebControlPro) -> None:
        """Create an empty list for the given client."""
        super().__init__()
        self._control = control

    async def __call__(
        self,
        responseType: WMS_WebControl_pro_API_responseType = (
            WMS_WebControl_pro_API_responseType.Instant
        ),
    ) -> Any:
        """Send all actions and return the answer of the gateway."""
        if len(self) == 0:
            raise ValueError("ActionList is empty")
        return await self._control._action(
            actions=self,
            responseType=responseType,
        )


class Action:
    """One function of a destination, for example moving an awning."""

    def __init__(
        self,
        dest: Destination,
        id: int,
        actionType: int,
        actionDescription: int,
        **kwargs: Any,
    ) -> None:
        """Create the action from the configuration of the gateway."""
        self._dest = dest
        # The id becomes part of a file path, so it must be a number.
        self._id = int(id)
        self._persist = (dest._persist / f"{self._id}.json") if dest._persist else None
        self._store = JsonStore(self._persist)
        self._actionType = WMS_WebControl_pro_API_actionType(actionType)
        self._actionDescription = WMS_WebControl_pro_API_actionDescription(
            actionDescription
        )
        self._attrs = kwargs
        self._defaults = self._rotation_defaults()
        self._params: dict[str, Any] = {}
        self._overwrites: dict[str, Any] = {}
        self._needs_load = True
        self._needs_save = False
        self._extra: dict[str, Any] = {}
        self._settings = ActionSettings()
        self._saved_state: dict[str, Any] | None = None
        self.model = CoverModel()

    def __str__(self) -> str:
        return self.actionDescription.name

    def __repr__(self) -> str:
        return f"<Action {self.id}: {self} ({self.actionType.name})>"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Action)
            and self._dest == other._dest
            and self.id == other.id
        )

    def __hash__(self) -> int:
        return hash((self._dest.id, self.id))

    # --- Properties ---

    @property
    def host(self) -> str:
        """Return the host of the gateway."""
        return self._dest.host

    @property
    def id(self) -> int:
        """Return the id of the action."""
        return self._id

    @property
    def actionType(self) -> WMS_WebControl_pro_API_actionType:
        """Return the kind of value or command of the action."""
        return self._actionType

    @property
    def actionDescription(self) -> WMS_WebControl_pro_API_actionDescription:
        """Return the function of the action."""
        return self._actionDescription

    # --- Private methods ---

    def _rotation_defaults(self) -> dict[str, Any]:
        if (
            self._actionType is WMS_WebControl_pro_API_actionType.Rotation
            and self._attrs.get(_MIN_VALUE) == -ROTATION_LIMIT_REPORTED_BY_BUG
            and self._attrs.get(_MAX_VALUE) == ROTATION_LIMIT_REPORTED_BY_BUG
        ):
            return {
                _MIN_VALUE: -ROTATION_LIMIT_DEFAULT,
                _MAX_VALUE: ROTATION_LIMIT_DEFAULT,
            }
        return {}

    def _update_params(self, value: dict) -> None:
        self._params.update(value)
        self._learn_rotation_range(value.get("rotation"))

    def _learn_rotation_range(self, rotation: Any) -> None:
        if (
            self._actionType is not WMS_WebControl_pro_API_actionType.Rotation
            or not _is_number(rotation)
        ):
            return
        reported_min, reported_max = self.reported_rotation_range
        if not (_is_number(reported_min) and _is_number(reported_max)):
            return
        # The reported limits themselves are no real position of a drive.
        if not reported_min < rotation < reported_max:
            return
        minimum, maximum = self.rotation_range
        if rotation < minimum:
            self._overwrites[_MIN_VALUE] = rotation
            self._needs_save = True
        elif rotation > maximum:
            self._overwrites[_MAX_VALUE] = rotation
            self._needs_save = True

    async def _load_persists(self) -> None:
        data = await self._store.load()
        self._extra = {k: v for k, v in data.items() if k not in _STORED_KEYS}
        overwrites = data.get("overwrites")
        if isinstance(overwrites, dict):
            self._overwrites = overwrites
        self._settings = ActionSettings.from_dict(data.get("settings"))
        state = data.get("state")
        if isinstance(state, dict):
            self.model.restore(
                state.get("position"),
                state.get("tilt"),
                state.get("target_position"),
                state.get("target_tilt"),
            )
        self._saved_state = self.model.snapshot()

    async def _save_persists(self) -> None:
        state = self.model.snapshot()
        await self._store.save(
            {
                **self._extra,
                "version": STORE_VERSION,
                "overwrites": self._overwrites,
                "settings": asdict(self._settings),
                "state": state,
            }
        )
        self._saved_state = state
        self._needs_save = False

    async def _ensure_loaded(self) -> None:
        if self._needs_load:
            self._needs_load = False
            await self._load_persists()

    # --- Public methods ---

    @property
    def settings(self) -> ActionSettings:
        """Return the settings of this action."""
        return self._settings

    async def set_travel_time(self, direction: TravelDirection, seconds: float) -> None:
        """Store the travel time of a full drive in seconds, 0 means unknown."""
        if not (math.isfinite(seconds) and seconds >= 0):
            raise ValueError(f"Travel time must be a number >= 0: {seconds}")
        await self._ensure_loaded()
        self._settings = replace(
            self._settings, **{f"travel_time_{direction.value}": float(seconds)}
        )
        await self._save_persists()

    @property
    def reported_rotation_range(self) -> tuple[Any, Any]:
        """Return the rotation limits as the hub reports them."""
        return self._attrs.get(_MIN_VALUE), self._attrs.get(_MAX_VALUE)

    @property
    def rotation_range(self) -> tuple[float, float]:
        """Return the rotation limits in use, minimum and maximum.

        An overwrite wins over the default for the firmware bug, which wins
        over the limits of the hub.
        """
        return self.minValue, self.maxValue

    async def set_rotation_range(
        self, *, minimum: float | None = None, maximum: float | None = None
    ) -> None:
        """Overwrite one or both rotation limits and store them."""
        await self._ensure_loaded()
        if minimum is not None:
            self._overwrites[_MIN_VALUE] = minimum
        if maximum is not None:
            self._overwrites[_MAX_VALUE] = maximum
        await self._save_persists()

    async def reset_rotation_range(self) -> None:
        """Remove the overwritten rotation limits and store that."""
        await self._ensure_loaded()
        self._overwrites.pop(_MIN_VALUE, None)
        self._overwrites.pop(_MAX_VALUE, None)
        await self._save_persists()

    def rotation_to_tilt(self, rotation: float) -> int:
        """Convert a rotation to a tilt (0 closed, 100 open) with the limits in use."""
        return rotation_to_tilt(rotation, *self.rotation_range)

    def tilt_to_rotation(self, tilt: float) -> int:
        """Convert a tilt (0 closed, 100 open) to a rotation with the limits in use."""
        return tilt_to_rotation(tilt, *self.rotation_range)

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
        """Load stored data once and store changes of overwrites, settings and state.

        Call this after commands and measurements. Destination.refresh does it too.
        """
        await self._ensure_loaded()
        if self._needs_save or self.model.snapshot() != self._saved_state:
            await self._save_persists()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._overwrites:
            return self._overwrites[name]
        if name.startswith("wms__"):
            return self._attrs.get(name[5:])
        if name in self._defaults:
            return self._defaults[name]
        return self._attrs.get(name)

    def __getitem__(self, name: str) -> Any:
        if name in self._overwrites:
            return self._overwrites[name]
        return self._params.get(name)

    def __setitem__(self, name: str, value: Any) -> None:
        if name in self._attrs:
            self._overwrites[name] = value
            self._needs_save = True
        elif name in self._params:
            self._params[name] = value

    def __delitem__(self, name: str) -> None:
        if name in self._overwrites:
            del self._overwrites[name]
            self._needs_save = True

    def prep(self, **kwargs: Any) -> ActionList:
        """Return an action list with this action and the given parameters."""
        actionList = ActionList(self._dest._control)
        actionList.append(
            {
                "destinationId": self._dest.id,
                "actionId": self.id,
                "parameters": kwargs,
            }
        )
        return actionList

    async def __call__(
        self,
        responseType: WMS_WebControl_pro_API_responseType = (
            WMS_WebControl_pro_API_responseType.Instant
        ),
        **kwargs: Any,
    ) -> Any:
        """Send the action with the given parameters to the gateway."""
        return await self._dest._control._action(
            actions=self.prep(**kwargs),
            responseType=responseType,
        )

    def diag(self) -> dict:
        """Return the action for the diagnostics."""
        return {
            "id": self.id,
            "actionType": self.actionType.name,
            "actionDescription": self.actionDescription.name,
            "attrs": self._attrs,
            "params": self._params,
            "overwrites": self._overwrites,
            "defaults": self._defaults,
            "settings": asdict(self._settings),
            "model": {
                **self.model.snapshot(),
                "source": self.model.source.value if self.model.source else None,
            },
        }
