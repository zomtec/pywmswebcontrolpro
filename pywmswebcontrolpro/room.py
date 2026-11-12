"""A room of the gateway with its destinations and scenes."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .webcontrol import WebControlPro


class Room:
    """A room that groups destinations and scenes."""

    def __init__(
        self,
        control: WebControlPro,
        id: int,
        name: str,
        destinations: list[int],
        scenes: list[int],
    ) -> None:
        self._control = control
        self._id = id
        self._name = name
        self._destination_ids = destinations
        self._scene_ids = scenes

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Room {self.id}: {self}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Room) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # --- Properties ---

    @property
    def host(self) -> str:
        """Return the host of the gateway."""
        return self._control.host

    @property
    def id(self) -> int:
        """Return the id of the room."""
        return self._id

    @property
    def name(self) -> str:
        """Return the name of the room."""
        return self._name

    @property
    def destinations(self) -> dict:
        """Return the destinations of the room by id."""
        return {
            dest_id: self._control.dests[dest_id] for dest_id in self._destination_ids
        }

    @property
    def scenes(self) -> dict:
        """Return the scenes of the room by id."""
        return {
            scene_id: self._control.scenes[scene_id] for scene_id in self._scene_ids
        }

    # --- Public methods ---

    def diag(self) -> dict:
        """Return the room for the diagnostics."""
        return {
            "id": self.id,
            "name": self.name,
            "destinations": {dest.id: dest.name for dest in self.destinations.values()},
            "scenes": {scene.id: scene.name for scene in self.scenes.values()},
        }
