"""Tests of the rotation limits and the conversion between rotation and tilt."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pywmswebcontrolpro.action import Action
from pywmswebcontrolpro.const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
)
from pywmswebcontrolpro.travel import rotation_to_tilt, tilt_to_rotation
from pywmswebcontrolpro.webcontrol import WebControlPro

from .conftest import FakeGateway

type ControlFactory = Callable[..., WebControlPro]

SLAT_DEST_ID = 9
ROTATE_ID = 6


def slat_config(minimum: int = -127, maximum: int = 127) -> dict[str, Any]:
    """Return a configuration with one slat drive that can rotate."""
    return {
        "destinations": [
            {
                "id": SLAT_DEST_ID,
                "names": ["Blind"],
                "animationType": 0,
                "actions": [
                    {
                        "id": 0,
                        "actionType": WMS_WebControl_pro_API_actionType.Percentage,
                        "actionDescription": (
                            WMS_WebControl_pro_API_actionDescription.SlatDrive
                        ),
                        "minValue": 0,
                        "maxValue": 100,
                    },
                    {
                        "id": ROTATE_ID,
                        "actionType": WMS_WebControl_pro_API_actionType.Rotation,
                        "actionDescription": (
                            WMS_WebControl_pro_API_actionDescription.SlatRotate
                        ),
                        "minValue": minimum,
                        "maxValue": maximum,
                    },
                ],
            }
        ],
        "rooms": [],
        "scenes": [],
    }


def status_rotation(rotation: Any) -> dict[str, Any]:
    """Return a status answer with a rotation of the slat drive."""
    return {
        "details": [
            {
                "destinationId": SLAT_DEST_ID,
                "data": {
                    "productData": [
                        {"actionId": 0, "value": {"percentage": 100}},
                        {"actionId": ROTATE_ID, "value": {"rotation": rotation}},
                    ]
                },
            }
        ]
    }


async def rotate_action(
    make_control: ControlFactory,
    gateway: FakeGateway,
    config: dict[str, Any],
    persist: Path | None = None,
) -> Action:
    """Return the rotation action of a fresh client."""
    gateway.config = config
    control = make_control(persist=str(persist) if persist else None)
    await control.refresh()
    return control.dests[SLAT_DEST_ID].actions[ROTATE_ID]


@pytest.mark.parametrize(
    ("rotation", "tilt"),
    [(-75, 100), (-38, 75), (0, 50), (38, 25), (75, 0), (-127, 100), (127, 0)],
)
def test_rotation_to_tilt_is_linear_and_limited(rotation: int, tilt: int) -> None:
    """The minimum rotation is tilt 100, values beyond the limits are cut."""
    assert rotation_to_tilt(rotation, -75, 75) == tilt


@pytest.mark.parametrize(
    ("tilt", "rotation"),
    [(100, -75), (75, -37), (50, 0), (25, 38), (0, 75), (120, -75), (-5, 75)],
)
def test_tilt_to_rotation_stays_within_the_limits(tilt: int, rotation: int) -> None:
    """Tilt 100 is the minimum rotation and the result is a whole number."""
    result = tilt_to_rotation(tilt, -75, 75)

    assert result == rotation
    assert isinstance(result, int)


def test_rotation_to_tilt_without_a_range() -> None:
    """A range without width has no tilt."""
    assert rotation_to_tilt(0, 0, 0) == 0


async def test_limits_of_the_firmware_bug_are_replaced(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """Limits of -127 and 127 are a firmware bug, -75 and 75 are used."""
    action = await rotate_action(make_control, gateway, slat_config())

    assert action.reported_rotation_range == (-127, 127)
    assert action.rotation_range == (-75, 75)
    assert action.tilt_to_rotation(100) == -75
    assert action.tilt_to_rotation(0) == 75
    assert action.rotation_to_tilt(75) == 0


async def test_other_limits_of_the_hub_are_used_unchanged(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """A firmware that reports other limits needs no workaround."""
    action = await rotate_action(make_control, gateway, slat_config(-80, 80))

    assert action.rotation_range == (-80, 80)


async def test_drive_action_has_no_rotation_default(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """Only rotation actions get the default limits."""
    gateway.config = slat_config()
    control = make_control()
    await control.refresh()

    drive = control.dests[SLAT_DEST_ID].actions[0]

    assert (drive.minValue, drive.maxValue) == (0, 100)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (-90, (-90, 75)),
        (100, (-75, 100)),
        (60, (-75, 75)),
        (127, (-75, 75)),
        (-127, (-75, 75)),
        (None, (-75, 75)),
        (True, (-75, 75)),
    ],
)
async def test_measured_rotation_beyond_the_range_widens_it(
    make_control: ControlFactory,
    gateway: FakeGateway,
    tmp_path: Path,
    rotation: Any,
    expected: tuple[int, int],
) -> None:
    """A real rotation outside the range in use becomes the new limit."""
    action = await rotate_action(make_control, gateway, slat_config(), tmp_path)
    gateway.status_answers = [status_rotation(rotation)]

    await action._dest.refresh()
    await action.sync()

    assert action.rotation_range == expected


async def test_learned_limit_survives_a_restart(
    make_control: ControlFactory, gateway: FakeGateway, tmp_path: Path
) -> None:
    """The widened range is stored."""
    action = await rotate_action(make_control, gateway, slat_config(), tmp_path)
    gateway.status_answers = [status_rotation(90)]
    await action._dest.refresh()
    await action.sync()

    restarted = await rotate_action(make_control, gateway, slat_config(), tmp_path)
    await restarted.sync()

    assert restarted.rotation_range == (-75, 90)


async def test_rotation_range_can_be_set_and_reset(
    make_control: ControlFactory, gateway: FakeGateway, tmp_path: Path
) -> None:
    """The user can overwrite the limits, a reset returns to the default."""
    action = await rotate_action(make_control, gateway, slat_config(), tmp_path)
    path = tmp_path / str(SLAT_DEST_ID) / f"{ROTATE_ID}.json"

    await action.set_rotation_range(minimum=-60)
    assert action.rotation_range == (-60, 75)
    assert json.loads(path.read_text())["overwrites"] == {"minValue": -60}

    await action.set_rotation_range(maximum=70)
    assert action.rotation_range == (-60, 70)

    await action.reset_rotation_range()
    assert action.rotation_range == (-75, 75)
    assert json.loads(path.read_text())["overwrites"] == {}
