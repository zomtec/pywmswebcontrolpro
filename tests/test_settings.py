"""Tests of the stored settings and last state of a drive action."""

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from pywmswebcontrolpro.action import Action
from pywmswebcontrolpro.store import ActionSettings, TravelDirection
from pywmswebcontrolpro.travel import PositionSource
from pywmswebcontrolpro.webcontrol import WebControlPro

from .conftest import ACTION_ID, DEST_ID, SECOND_ACTION_ID

type ControlFactory = Callable[..., WebControlPro]


async def new_action(
    make_control: ControlFactory,
    persist: Path | None,
    action_id: int = ACTION_ID,
) -> Action:
    """Return an action of a fresh client, like after a restart."""
    control = make_control(persist=str(persist) if persist else None)
    await control.refresh()
    return control.dests[DEST_ID].actions[action_id]


def action_file(persist: Path, action_id: int = ACTION_ID) -> Path:
    """Return the file of an action."""
    return persist / str(DEST_ID) / f"{action_id}.json"


def is_empty(path: Path) -> bool:
    """Return True if the directory has no entries."""
    return not any(path.iterdir())


async def test_defaults_without_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Without a stored file the travel times are 0 and nothing is written."""
    action = await new_action(make_control, tmp_path)

    await action.sync()

    assert action.settings == ActionSettings()
    assert not action_file(tmp_path).exists()


@pytest.mark.parametrize(
    ("direction", "seconds"),
    [(TravelDirection.OPEN, 41.5), (TravelDirection.CLOSE, 38.0)],
)
async def test_travel_time_survives_restart(
    make_control: ControlFactory,
    tmp_path: Path,
    direction: TravelDirection,
    seconds: float,
) -> None:
    """A stored travel time is read back by a new client."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(direction, seconds)

    restarted = await new_action(make_control, tmp_path)
    await restarted.sync()

    assert restarted.settings.travel_time(direction) == seconds


async def test_set_travel_time_keeps_other_direction(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Setting one direction does not change the other."""
    action = await new_action(make_control, tmp_path)

    await action.set_travel_time(TravelDirection.OPEN, 40)
    await action.set_travel_time(TravelDirection.CLOSE, 50)

    assert action.settings == ActionSettings(40.0, 50.0)


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf")])
async def test_invalid_travel_time_is_rejected(
    make_control: ControlFactory, tmp_path: Path, seconds: float
) -> None:
    """Negative and non-finite travel times are refused."""
    action = await new_action(make_control, tmp_path)

    with pytest.raises(ValueError, match="must be a number"):
        await action.set_travel_time(TravelDirection.OPEN, seconds)

    assert not action_file(tmp_path).exists()


async def test_drive_actions_are_independent(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Each drive action of a destination has its own times, model and file."""
    control = make_control(persist=str(tmp_path))
    await control.refresh()
    first, second = (
        control.dests[DEST_ID].actions[ACTION_ID],
        control.dests[DEST_ID].actions[SECOND_ACTION_ID],
    )

    await first.set_travel_time(TravelDirection.OPEN, 40)
    first.apply_measurement(0.0, 70, 10)
    await first.sync()

    assert second.settings == ActionSettings()
    assert second.model.position is None
    assert action_file(tmp_path, ACTION_ID).exists()
    assert not action_file(tmp_path, SECOND_ACTION_ID).exists()


async def test_state_survives_restart(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Position and tilt are restored after a restart and marked as restored."""
    action = await new_action(make_control, tmp_path)
    await action.sync()
    action.apply_measurement(0.0, 35, 20)
    await action.sync()

    restarted = await new_action(make_control, tmp_path)
    await restarted.sync()

    assert restarted.model.position_int == 35
    assert restarted.model.tilt == 20
    assert restarted.model.source is PositionSource.RESTORED


async def test_running_drive_is_restored_at_its_target(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A drive that was running at shutdown is restored at its target."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.CLOSE, 40)
    action.apply_measurement(0.0, 100, 100)
    action.start_move(0.0, 0, tilt=0)
    await action.sync()

    restarted = await new_action(make_control, tmp_path)
    await restarted.sync()

    assert restarted.model.position_int == 0
    assert restarted.model.tilt == 0
    assert restarted.settings.travel_time_close == 40


async def test_start_move_uses_stored_travel_times(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """The drive duration follows the stored time of the direction."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.CLOSE, 50)
    action.apply_measurement(0.0, 100, 100)

    delay = action.start_move(10.0, 0)

    assert delay == 52.0
    assert action.model.is_closing


async def test_state_is_written_only_on_change(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated syncs without a change do not rewrite the file."""
    action = await new_action(make_control, tmp_path)
    saves = []
    original = action._store.save

    async def counting_save(data: dict) -> bool:
        saves.append(data)
        return await original(data)

    monkeypatch.setattr(action._store, "save", counting_save)

    await action.sync()
    action.apply_measurement(0.0, 10, 10)
    await action.sync()
    await action.sync()

    assert len(saves) == 1


async def test_existing_overwrites_and_unknown_keys_are_kept(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A file written by an older version keeps its content when settings change."""
    path = action_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"overwrites": {"minValue": -127}, "other": 1}))
    action = await new_action(make_control, tmp_path)

    await action.set_travel_time(TravelDirection.OPEN, 30)

    stored = json.loads(path.read_text())
    assert stored["overwrites"] == {"minValue": -127}
    assert stored["other"] == 1
    assert stored["settings"]["travel_time_open"] == 30.0
    assert action["minValue"] == -127


async def test_overwrite_change_is_saved_with_settings(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Changing an overwrite writes it together with the existing settings."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.OPEN, 30)

    action._attrs["minValue"] = 0
    action["minValue"] = -75
    await action.sync()

    stored = json.loads(action_file(tmp_path).read_text())
    assert stored["overwrites"] == {"minValue": -75}
    assert stored["settings"]["travel_time_open"] == 30.0


async def test_concurrent_writes_keep_a_valid_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Many simultaneous writes end with the last value in a valid file."""
    action = await new_action(make_control, tmp_path)

    await asyncio.gather(
        *(action.set_travel_time(TravelDirection.OPEN, i) for i in range(1, 41))
    )

    path = action_file(tmp_path)
    stored = json.loads(path.read_text())
    assert stored["settings"]["travel_time_open"] == action.settings.travel_time_open
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"settings": "x"}', '{"settings": {"travel_time_open": -5}}'],
)
async def test_unusable_file_falls_back_to_defaults(
    make_control: ControlFactory, tmp_path: Path, content: str
) -> None:
    """A broken or invalid file does not break the action."""
    path = action_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)

    action = await new_action(make_control, tmp_path)
    await action.sync()

    assert action.settings == ActionSettings()


async def test_without_persist_path_nothing_is_stored(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A client without a persist path keeps the settings in memory only."""
    action = await new_action(make_control, None)

    await action.set_travel_time(TravelDirection.OPEN, 30)

    assert action.settings.travel_time_open == 30
    assert is_empty(tmp_path)


async def test_diag_contains_settings_and_model(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Diagnostics show settings and the displayed state with its origin."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.OPEN, 30)
    action.apply_measurement(0.0, 60, 0)

    diag = action.diag()

    assert diag["settings"] == {"travel_time_open": 30.0, "travel_time_close": 0.0}
    assert diag["model"]["position"] == 60
    assert diag["model"]["source"] == "measured"


async def test_first_load_and_a_change_at_the_same_time_keep_the_stored_data(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A change waits for the stored data instead of replacing it."""
    path = action_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "overwrites": {"minValue": -75},
                "settings": {"travel_time_open": 20.0, "travel_time_close": 21.0},
                "state": {"position": 40},
            }
        )
    )
    action = await new_action(make_control, tmp_path)

    await asyncio.gather(
        action.sync(), action.set_travel_time(TravelDirection.OPEN, 33.0)
    )

    stored = json.loads(path.read_text())
    assert stored["overwrites"] == {"minValue": -75}
    assert stored["settings"] == {"travel_time_open": 33.0, "travel_time_close": 21.0}
    assert stored["state"]["position"] == 40
    assert action.settings == ActionSettings(33.0, 21.0)


async def test_file_that_is_no_text_falls_back_to_defaults(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A file with broken encoding is ignored like broken JSON."""
    path = action_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00")

    action = await new_action(make_control, tmp_path)
    await action.sync()

    assert action.settings == ActionSettings()


@pytest.mark.parametrize(
    "state",
    [
        {"position": "abc"},
        {"position": 140},
        {"position": True},
        {"target_position": -1, "tilt": "x"},
    ],
)
async def test_invalid_stored_state_is_ignored(
    make_control: ControlFactory, tmp_path: Path, state: dict[str, object]
) -> None:
    """A stored position that is no percentage is not restored."""
    path = action_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"state": state}))

    action = await new_action(make_control, tmp_path)
    await action.sync()

    assert action.model.position is None
    assert action.model.tilt is None


async def test_failed_write_is_tried_again_with_the_next_sync(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write error is logged and the change is stored later."""
    action = await new_action(make_control, tmp_path)
    await action.sync()
    replace = os.replace

    def fail(source: str, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    await action.set_travel_time(TravelDirection.OPEN, 12.0)
    assert not action_file(tmp_path).exists()
    assert "Could not write" in caplog.text
    assert not list(action_file(tmp_path).parent.glob("*.tmp"))

    monkeypatch.setattr(os, "replace", replace)
    await action.sync()

    stored = json.loads(action_file(tmp_path).read_text())
    assert stored["settings"]["travel_time_open"] == 12.0
