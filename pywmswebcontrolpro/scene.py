"""A scene of the gateway."""

from typing import TYPE_CHECKING, Any

from .room import Room

if TYPE_CHECKING:
    from .webcontrol import WebControlPro


class Scene:
    """A scene that the gateway can execute."""

    def __init__(
        self, control: WebControlPro, id: int, names: list[str], **kwargs: Any
    ) -> None:
        self._control = control
        self._id = id
        self._names = names

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Scene {self.id}: {self}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Scene) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # --- Properties ---

    @property
    def host(self) -> str:
        """Return the host of the gateway."""
        return self._control.host

    @property
    def id(self) -> int:
        """Return the id of the scene."""
        return self._id

    @property
    def name(self) -> str:
        """Return the name of the scene."""
        return self._names[0]

    @property
    def room(self) -> Room | None:
        """Return the room of the scene, if it belongs to one."""
        for room in self._control.rooms.values():
            if self._id in room._scene_ids:
                return room
        return None

    # --- Public methods ---

    async def __call__(self, **kwargs: Any) -> Any:
        """Execute the scene and return the answer of the gateway."""
        return await self._control._sceneActions(self._id, **kwargs)

    def diag(self) -> dict:
        """Return the scene for the diagnostics."""
        return {
            "id": self.id,
            "name": self.name,
            "room": {self.room.id: self.room.name} if self.room else None,
        }
