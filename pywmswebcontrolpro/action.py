import math
from dataclasses import asdict, replace
from typing import Any

from .const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_responseType,
)
from .store import STORE_VERSION, ActionSettings, JsonStore, TravelDirection
from .travel import CoverModel, Measurement

_STORED_KEYS = frozenset({"version", "overwrites", "settings", "state"})


class ActionList(list):
    def __init__(self, control) -> None:
        super().__init__()
        self._control = control

    async def __call__(
        self, responseType=WMS_WebControl_pro_API_responseType.Instant
    ) -> Any:
        if len(self) == 0:
            raise ValueError("ActionList is empty")
        return await self._control._action(
            actions=self,
            responseType=responseType,
        )


class Action:
    def __init__(
        self, dest, id: int, actionType: int, actionDescription: int, **kwargs
    ) -> None:
        self._dest = dest
        self._id = id
        self._persist = (dest._persist / f"{id}.json") if dest._persist else None
        self._store = JsonStore(self._persist)
        self._actionType = WMS_WebControl_pro_API_actionType(actionType)
        self._actionDescription = WMS_WebControl_pro_API_actionDescription(
            actionDescription
        )
        self._attrs = kwargs
        self._params = {}
        self._overwrites = {}
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

    def __eq__(self, other) -> bool:
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # --- Properties ---

    @property
    def host(self) -> str:
        return self._dest.host

    @property
    def id(self) -> int:
        return self._id

    @property
    def actionType(self) -> WMS_WebControl_pro_API_actionType:
        return self._actionType

    @property
    def actionDescription(self) -> WMS_WebControl_pro_API_actionDescription:
        return self._actionDescription

    # --- Private methods ---

    def _update_params(self, value: dict) -> None:
        self._params.update(value)

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
        if name in self._overwrites:
            return self._overwrites[name]
        if name.startswith("wms__"):
            name = name[5:]
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

    def prep(self, **kwargs) -> ActionList:
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
        self, responseType=WMS_WebControl_pro_API_responseType.Instant, **kwargs
    ) -> Any:
        return await self._dest._control._action(
            actions=self.prep(**kwargs),
            responseType=responseType,
        )

    def diag(self) -> dict:
        return {
            "id": self.id,
            "actionType": self.actionType.name,
            "actionDescription": self.actionDescription.name,
            "attrs": self._attrs,
            "params": self._params,
            "overwrites": self._overwrites,
            "settings": asdict(self._settings),
            "model": {
                **self.model.snapshot(),
                "source": self.model.source.value if self.model.source else None,
            },
        }
