"""Client of the local API of the WMS WebControl pro gateway."""

import asyncio
import pprint
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout

from .const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_command_action,
    WMS_WebControl_pro_API_command_getConfiguration,
    WMS_WebControl_pro_API_command_getStatus,
    WMS_WebControl_pro_API_command_ping,
    WMS_WebControl_pro_API_command_sceneActions,
    WMS_WebControl_pro_API_protocolVersion,
    WMS_WebControl_pro_API_responseType,
    WMS_WebControl_pro_API_sceneActionType,
    WMS_WebControl_pro_API_source,
)
from .destination import Destination
from .exceptions import (
    WebControlProCommandError,
    WebControlProConnectionError,
    WebControlProResponseError,
)
from .refresh import RefreshStats
from .room import Room
from .scene import Scene
from .store import HubStore

DEFAULT_STALE_WARNING_AFTER = timedelta(hours=24)
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_REQUEST_SPACING = 0.5


def _names(names: object) -> list[str]:
    """Return the names of a destination or raise ValueError."""
    if not isinstance(names, list) or not names:
        raise ValueError(f"Unexpected names: {names!r}")
    return [str(name) for name in names]


@dataclass(frozen=True)
class ConfigChanges:
    """Destinations that appeared, disappeared or got a new name with a refresh."""

    added: frozenset[int] = frozenset()
    removed: frozenset[int] = frozenset()
    renamed: frozenset[int] = frozenset()


class WebControlPro:
    """Client of one WMS WebControl pro gateway."""

    def __init__(
        self,
        host: str,
        session: ClientSession,
        persist: str | None = None,
        *,
        stale_warning_after: timedelta = DEFAULT_STALE_WARNING_AFTER,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        request_spacing: float = DEFAULT_REQUEST_SPACING,
    ) -> None:
        """Create a client.

        persist is the directory for the stored settings and the last state
        of the actions, without it nothing is stored. stale_warning_after
        sets how long a destination may stay silent before a warning is
        logged, retry_delay the pause in seconds between retries after a
        radio busy answer and request_spacing the minimum pause in seconds
        between two status requests.
        """
        self.stale_warning_after = stale_warning_after
        self.retry_delay = retry_delay
        self._common_lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self.request_spacing = request_spacing
        self._status_last = time.monotonic() - self.request_spacing
        self._host = host
        self._control = f"http://{host}/commonCommand"
        self._session = session
        self._store = HubStore(Path(persist) if persist else None)
        self._config: dict[str, Any] = {}
        self._dests: dict[int, Destination] = {}
        self._rooms: dict[int, Room] = {}
        self._scenes: dict[int, Scene] = {}

    # --- Private methods ---

    async def _commonCommand(self, command: str, **kwargs: Any) -> Any:
        data = {
            "protocolVersion": WMS_WebControl_pro_API_protocolVersion,
            "command": command,
            "source": WMS_WebControl_pro_API_source,
        }
        data.update(kwargs)
        try:
            async with (
                self._common_lock,
                self._session.post(
                    url=self._control, json=data, timeout=ClientTimeout(total=5)
                ) as response,
            ):
                response.raise_for_status()
                answer = await response.json()
        except (ClientResponseError, ValueError) as err:
            raise WebControlProResponseError(
                f"Unexpected answer to {command}: {err!r}"
            ) from err
        except (ClientError, TimeoutError) as err:
            raise WebControlProConnectionError(
                f"No answer to {command}: {err!r}"
            ) from err
        if not isinstance(answer, dict):
            raise WebControlProResponseError(f"Unexpected answer to {command}")
        return answer

    async def _command(self, command: str, **kwargs: Any) -> Any:
        answer = await self._commonCommand(command, **kwargs)
        if errors := answer.get("errors"):
            raise WebControlProCommandError(errors)
        return answer

    async def _ping(self) -> Any:
        return await self._commonCommand(WMS_WebControl_pro_API_command_ping)

    async def _getConfiguration(self) -> Any:
        return await self._commonCommand(
            WMS_WebControl_pro_API_command_getConfiguration
        )

    async def _getStatus(self, destinationId: int) -> Any:
        async with self._status_lock:
            elapsed_time = time.monotonic() - self._status_last
            if elapsed_time < self.request_spacing:
                await asyncio.sleep(self.request_spacing - elapsed_time)
            try:
                return await self._commonCommand(
                    WMS_WebControl_pro_API_command_getStatus,
                    destinations=[destinationId],
                )
            finally:
                self._status_last = time.monotonic()

    async def _action(
        self,
        actions: list[dict[str, Any]],
        responseType: WMS_WebControl_pro_API_responseType = (
            WMS_WebControl_pro_API_responseType.Instant
        ),
    ) -> Any:
        return await self._command(
            WMS_WebControl_pro_API_command_action,
            responseType=responseType,
            actions=actions,
        )

    async def _sceneActions(
        self,
        sceneId: int,
        sceneActionType: WMS_WebControl_pro_API_sceneActionType = (
            WMS_WebControl_pro_API_sceneActionType.Execute
        ),
        responseType: WMS_WebControl_pro_API_responseType = (
            WMS_WebControl_pro_API_responseType.Instant
        ),
    ) -> Any:
        return await self._command(
            WMS_WebControl_pro_API_command_sceneActions,
            responseType=responseType,
            sceneId=sceneId,
            sceneActionType=sceneActionType,
        )

    # --- Public methods ---

    async def ping(self) -> bool:
        """Return True if the gateway answers the ping."""
        ping = await self._ping()
        return bool(ping.get("status") == 0)

    async def refresh(self) -> ConfigChanges:
        """Read the configuration and create destinations, rooms and scenes.

        A configuration that is not understood raises WebControlProResponseError
        and leaves the known destinations, rooms and scenes unchanged.

        A destination that is already known stays the same object, so its
        actions keep their movement model and stored state. It takes over the
        names of the configuration. Only new destinations are created, and the
        result names the destinations that appeared, disappeared or got a new
        name. Rooms and scenes are created again.
        """
        config = await self._getConfiguration()
        # New actions read their stored data when they are created.
        await self._store.load()
        known = self._dests
        try:
            dests = {
                dest["id"]: known.get(dest["id"]) or Destination(self, **dest)
                for dest in config["destinations"]
            }
            rooms = {room["id"]: Room(self, **room) for room in config["rooms"]}
            scenes = {scene["id"]: Scene(self, **scene) for scene in config["scenes"]}
            names = {
                dest["id"]: _names(dest["names"])
                for dest in config["destinations"]
                if dest["id"] in known
            }
        except (KeyError, TypeError, ValueError) as err:
            raise WebControlProResponseError(
                f"Unexpected configuration: {err!r}"
            ) from err
        self._config = config
        self._dests = dests
        self._rooms = rooms
        self._scenes = scenes
        return ConfigChanges(
            added=frozenset(self._dests) - frozenset(known),
            removed=frozenset(known) - frozenset(self._dests),
            renamed=frozenset(
                dest_id
                for dest_id, dest_names in names.items()
                if dests[dest_id]._rename(dest_names)
            ),
        )

    def dest(self, name: str) -> Destination:
        """Return the destination with the given name or raise KeyError."""
        for dest in self._dests.values():
            if dest.name == name:
                return dest
        raise KeyError(f"Destination with name '{name}' not found")

    def diag(self) -> dict[str, Any]:
        """Return the client for the diagnostics."""
        return {
            "host": self._host,
            "config": self._config,
            "dests": {k: v.diag() for k, v in self._dests.items()},
            "rooms": {k: v.diag() for k, v in self._rooms.items()},
            "scenes": {k: v.diag() for k, v in self._scenes.items()},
        }

    # --- Properties ---

    @property
    def host(self) -> str:
        """Return the host of the gateway."""
        return self._host

    @property
    def config(self) -> Mapping[str, Any]:
        """Return the configuration of the gateway."""
        return MappingProxyType(self._config)

    @property
    def dests(self) -> Mapping[int, Destination]:
        """Return the destinations by id."""
        return MappingProxyType(self._dests)

    @property
    def stats(self) -> RefreshStats:
        """Return the refresh counters of all destinations together."""
        return RefreshStats.combined(dest.stats for dest in self._dests.values())

    @property
    def rooms(self) -> Mapping[int, Room]:
        """Return the rooms by id."""
        return MappingProxyType(self._rooms)

    @property
    def scenes(self) -> Mapping[int, Scene]:
        """Return the scenes by id."""
        return MappingProxyType(self._scenes)


async def async_main_test() -> None:
    """Run the example against a gateway named webcontrol."""
    async with ClientSession() as session:
        control = WebControlPro("webcontrol", session)
        pprint.pprint(await control.ping())
        await control.refresh()

        for dest in control.dests.values():
            print((dest.room, dest, dest.animationType))
            await dest.refresh()
            for action in dest.actions.values():
                print((action, action._attrs, action._params))

        for scene in control.scenes.values():
            print(scene)
            if scene.name == "Licht an":
                response = await scene()
                print(response)

        action = control.dest("Licht").action(
            WMS_WebControl_pro_API_actionDescription.LightDimming,
            WMS_WebControl_pro_API_actionType.Percentage,
        )
        print(action)
        response = await action(
            percentage=0, responseType=WMS_WebControl_pro_API_responseType.Detailed
        )
        print(response)

        action_list = action.prep(percentage=100)
        action_list += action.prep(percentage=0)
        print(action_list)
        response = await action_list(
            responseType=WMS_WebControl_pro_API_responseType.Detailed
        )
        print(response)

        pprint.pprint(control.diag())


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_main_test())
