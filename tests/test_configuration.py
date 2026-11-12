"""Tests for reading the configuration of the gateway again."""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from pywmswebcontrolpro.webcontrol import ConfigChanges, WebControlPro

from .conftest import CONFIG, DEST_ID, FakeGateway

NEW_DEST_ID = 8


def _config_with(*dest_ids: int) -> dict[str, Any]:
    """Return a configuration with copies of the test destination."""
    config = deepcopy(CONFIG)
    template = config["destinations"][0]
    config["destinations"] = [{**template, "id": dest_id} for dest_id in dest_ids]
    return config


async def test_first_refresh_reports_all_destinations_as_added(
    make_control: Callable[..., WebControlPro],
) -> None:
    """The first read of the configuration finds the destinations."""
    changes = await make_control().refresh()

    assert changes == ConfigChanges(added=frozenset({DEST_ID}))


async def test_refresh_reports_added_and_removed_destinations(
    make_control: Callable[..., WebControlPro], gateway: FakeGateway
) -> None:
    """A destination that appears or disappears is named in the result."""
    control = make_control()
    await control.refresh()
    gateway.config = _config_with(NEW_DEST_ID)

    changes = await control.refresh()

    assert changes == ConfigChanges(
        added=frozenset({NEW_DEST_ID}), removed=frozenset({DEST_ID})
    )
    assert set(control.dests) == {NEW_DEST_ID}


async def test_refresh_keeps_the_known_destinations(
    make_control: Callable[..., WebControlPro], gateway: FakeGateway
) -> None:
    """A known destination stays the same object with its state."""
    control = make_control()
    await control.refresh()
    known = control.dests[DEST_ID]
    gateway.config = _config_with(DEST_ID, NEW_DEST_ID)

    changes = await control.refresh()

    assert changes == ConfigChanges(added=frozenset({NEW_DEST_ID}))
    assert control.dests[DEST_ID] is known
