"""Tests of the requests to the gateway and of how its answers are read."""

import copy
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest

from pywmswebcontrolpro.const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_animationType,
    WMS_WebControl_pro_API_drivingCause,
    WMS_WebControl_pro_API_responseType,
)
from pywmswebcontrolpro.destination import Destination
from pywmswebcontrolpro.exceptions import (
    WebControlProCommandError,
    WebControlProConnectionError,
    WebControlProError,
    WebControlProResponseError,
)
from pywmswebcontrolpro.refresh import ERROR_RADIO_BUSY, RefreshResult
from pywmswebcontrolpro.webcontrol import WebControlPro

from .conftest import (
    ACTION_ID,
    CONFIG,
    DEST_ID,
    SECOND_ACTION_ID,
    BadBody,
    FakeGateway,
    status_ok,
)

type ControlFactory = Callable[..., WebControlPro]

ROOM_ID = 3
SCENE_ID = 4
CONFIG_WITH_ROOM: dict[str, Any] = {
    **CONFIG,
    "rooms": [
        {
            "id": ROOM_ID,
            "name": "Terrace",
            "destinations": [DEST_ID],
            "scenes": [SCENE_ID],
        }
    ],
    "scenes": [{"id": SCENE_ID, "names": ["Evening"]}],
}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [({"status": 0}, True), ({"status": 1}, False), ({}, False)],
)
async def test_ping(
    make_control: ControlFactory,
    gateway: FakeGateway,
    answer: dict[str, Any],
    expected: bool,
) -> None:
    """Only status 0 is a successful ping."""
    gateway.command_answer = answer

    assert await make_control().ping() is expected


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (TimeoutError(), WebControlProConnectionError),
        (aiohttp.ClientConnectionError(), WebControlProConnectionError),
        (BadBody(ValueError("no json")), WebControlProResponseError),
        (["no", "object"], WebControlProResponseError),
    ],
)
async def test_failed_request_raises_an_error_of_the_library(
    make_control: ControlFactory,
    gateway: FakeGateway,
    answer: Any,
    error: type[WebControlProError],
) -> None:
    """Connection problems and unreadable answers have their own errors."""
    gateway.command_answer = answer

    with pytest.raises(error):
        await make_control().ping()


@pytest.mark.parametrize(
    "config",
    [
        {"rooms": [], "scenes": []},
        {"destinations": [{"id": 1}], "rooms": [], "scenes": []},
        {
            "destinations": [{**CONFIG["destinations"][0], "id": "../x"}],
            "rooms": [],
            "scenes": [],
        },
    ],
)
async def test_unexpected_configuration_keeps_the_known_one(
    make_control: ControlFactory, gateway: FakeGateway, config: dict[str, Any]
) -> None:
    """A configuration that is not understood changes nothing."""
    control = make_control()
    await control.refresh()
    gateway.config = config

    with pytest.raises(WebControlProResponseError):
        await control.refresh()

    assert set(control.dests) == {DEST_ID}
    assert control.config == CONFIG


async def test_unknown_fields_and_values_of_a_newer_firmware_are_accepted(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """New fields are ignored and new enumeration values become Unknown."""
    config = copy.deepcopy(CONFIG_WITH_ROOM)
    config["destinations"][0]["animationType"] = 42
    config["destinations"][0]["newField"] = 1
    config["destinations"][0]["actions"][0]["actionType"] = 42
    config["destinations"][0]["actions"][0]["actionDescription"] = 42
    config["rooms"][0]["newField"] = 1
    config["scenes"][0]["newField"] = 1
    gateway.config = config
    control = make_control()

    await control.refresh()

    dest = control.dests[DEST_ID]
    action = dest.actions[ACTION_ID]
    assert dest.animationType is WMS_WebControl_pro_API_animationType.Unknown
    assert action.actionType is WMS_WebControl_pro_API_actionType.Unknown
    assert action.actionDescription is WMS_WebControl_pro_API_actionDescription.Unknown


async def test_unknown_driving_cause_does_not_fail_the_refresh(
    dest: Destination, gateway: FakeGateway
) -> None:
    """A driving cause of a newer firmware becomes Unknown."""
    status = status_ok()
    status["details"][0]["data"]["drivingCause"] = 42
    gateway.status_answers = [status]

    outcome = await dest.refresh()

    assert outcome.result is RefreshResult.OK
    assert dest.drivingCause is WMS_WebControl_pro_API_drivingCause.Unknown


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        ({"errors": [1]}, RefreshResult.ERROR),
        (
            {"details": [{"destinationId": DEST_ID, "data": {"productData": [{}]}}]},
            RefreshResult.ERROR,
        ),
        (BadBody(ValueError("no json")), RefreshResult.ERROR),
    ],
)
async def test_status_that_is_not_understood_counts_as_error(
    dest: Destination, gateway: FakeGateway, answer: Any, result: RefreshResult
) -> None:
    """Errors other than radio busy and broken answers are errors, not empty."""
    gateway.status_answers = [status_ok(30), answer]
    await dest.refresh()

    outcome = await dest.refresh()

    assert outcome.result is result
    assert dest.actions[ACTION_ID]["percentage"] == 30
    assert dest.status == status_ok(30)


async def test_action_sends_the_command(
    dest: Destination, gateway: FakeGateway
) -> None:
    """An action is sent with its destination, id and parameters."""
    await dest.actions[ACTION_ID](
        percentage=40, responseType=WMS_WebControl_pro_API_responseType.Detailed
    )

    request = gateway.requests[-1]
    assert request["command"] == "action"
    assert request["responseType"] == WMS_WebControl_pro_API_responseType.Detailed
    assert request["actions"] == [
        {
            "destinationId": DEST_ID,
            "actionId": ACTION_ID,
            "parameters": {"percentage": 40},
        }
    ]


async def test_action_list_sends_several_actions_with_one_request(
    dest: Destination, gateway: FakeGateway
) -> None:
    """Prepared actions are combined."""
    actions = dest.actions[ACTION_ID].prep(percentage=10)
    actions += dest.actions[SECOND_ACTION_ID].prep(percentage=20)

    await actions()

    assert [a["actionId"] for a in gateway.requests[-1]["actions"]] == [
        ACTION_ID,
        SECOND_ACTION_ID,
    ]


async def test_rejected_action_raises(dest: Destination, gateway: FakeGateway) -> None:
    """A command that the gateway rejects is an error with its codes."""
    gateway.command_answer = {"errors": [ERROR_RADIO_BUSY]}

    with pytest.raises(WebControlProCommandError) as error:
        await dest.actions[ACTION_ID](percentage=40)

    assert error.value.errors == [ERROR_RADIO_BUSY]


async def test_room_and_scene(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """Rooms link destinations and scenes, a scene can be executed."""
    gateway.config = CONFIG_WITH_ROOM
    control = make_control()
    await control.refresh()
    room = control.rooms[ROOM_ID]
    scene = control.scenes[SCENE_ID]

    await scene()

    assert room.name == "Terrace"
    assert control.dests[DEST_ID].room == room
    assert scene.room == room
    assert room.destinations == {DEST_ID: control.dests[DEST_ID]}
    assert room.scenes == {SCENE_ID: scene}
    assert gateway.requests[-1]["command"] == "sceneActions"
    assert gateway.requests[-1]["sceneId"] == SCENE_ID
    assert control.diag()["rooms"][ROOM_ID]["scenes"] == {SCENE_ID: "Evening"}


async def test_rejected_scene_raises(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """A scene that the gateway rejects is an error."""
    gateway.config = CONFIG_WITH_ROOM
    control = make_control()
    await control.refresh()
    gateway.command_answer = {"errors": [1]}

    with pytest.raises(WebControlProCommandError):
        await control.scenes[SCENE_ID]()


async def test_actions_of_different_destinations_are_not_equal(
    make_control: ControlFactory, gateway: FakeGateway
) -> None:
    """The same action id on another destination is another action."""
    config = copy.deepcopy(CONFIG)
    config["destinations"].append({**config["destinations"][0], "id": DEST_ID + 1})
    gateway.config = config
    control = make_control()
    await control.refresh()

    first = control.dests[DEST_ID].actions[ACTION_ID]
    second = control.dests[DEST_ID + 1].actions[ACTION_ID]

    assert first != second
    assert len({first, second}) == 2
    assert control.dest("Shutter") == control.dests[DEST_ID]
