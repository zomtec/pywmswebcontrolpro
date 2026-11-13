"""Tests of the stored settings and last state of a drive action."""

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from pywmswebcontrolpro import store
from pywmswebcontrolpro.action import Action
from pywmswebcontrolpro.store import (
    SETTINGS_FILE,
    STATE_FILE,
    STORE_VERSION,
    ActionSettings,
    TravelDirection,
)
from pywmswebcontrolpro.travel import PositionSource
from pywmswebcontrolpro.webcontrol import WebControlPro

from .conftest import (
    ACTION_ID,
    DEST_ID,
    SECOND_ACTION_ID,
    FakeGateway,
    stored_settings,
    stored_state,
)

type ControlFactory = Callable[..., WebControlPro]

OTHER_DEST_ID = 99
ACTION = str(ACTION_ID)
DEST_SETTINGS = f"{DEST_ID}/{SETTINGS_FILE}"
DEST_STATE = f"{DEST_ID}/{STATE_FILE}"


async def new_action(
    make_control: ControlFactory,
    persist: Path | None,
    action_id: int = ACTION_ID,
) -> Action:
    """Return an action of a fresh client, like after a restart."""
    control = make_control(persist=str(persist) if persist else None)
    await control.refresh()
    return control.dests[DEST_ID].actions[action_id]


def dest_dir(persist: Path, dest_id: int = DEST_ID) -> Path:
    """Return the directory with the files of a destination."""
    return persist / str(dest_id)


def write_file(persist: Path, name: str, entry: object) -> None:
    """Write a stored file with one entry for the test action."""
    directory = dest_dir(persist)
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(
        json.dumps({"version": STORE_VERSION, "actions": {ACTION: entry}})
    )


def is_empty(path: Path) -> bool:
    """Return True if the directory has no entries."""
    return not any(path.iterdir())


def temporary_files(path: Path) -> list[Path]:
    """Return the temporary files that a write left in the directory."""
    return list(path.glob("*.tmp"))


def count_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return a list that collects every written file as destination/name."""
    writes: list[str] = []
    original = store._write

    def counting_write(path: Path, content: str) -> None:
        writes.append(f"{path.parent.name}/{path.name}")
        original(path, content)

    monkeypatch.setattr(store, "_write", counting_write)
    return writes


async def test_defaults_without_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Without stored files the travel times are 0 and nothing is written."""
    action = await new_action(make_control, tmp_path)

    await action.sync()

    assert action.settings == ActionSettings()
    assert is_empty(tmp_path)


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

    assert restarted.settings.travel_time(direction) == seconds


async def test_stored_data_is_known_without_a_sync(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A new action has its stored data as soon as the client created it."""
    write_file(tmp_path, SETTINGS_FILE, {"settings": {"travel_time_open": 20.0}})
    write_file(tmp_path, STATE_FILE, {"position": 40})

    action = await new_action(make_control, tmp_path)

    assert action.settings == ActionSettings(20.0, 0.0)
    assert action.model.position_int == 40


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

    assert is_empty(tmp_path)


async def test_drive_actions_are_independent(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Each drive action of a destination has its own times, model and entry."""
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
    assert list(stored_settings(tmp_path)) == [ACTION]
    assert list(stored_state(tmp_path)) == [ACTION]


async def test_settings_and_state_are_stored_in_their_own_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Each file holds only its kind of data and only what is known."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.OPEN, 40)
    action.apply_measurement(0.0, 70, None)
    await action.sync()

    assert stored_settings(tmp_path) == {
        ACTION: {"settings": {"travel_time_open": 40.0, "travel_time_close": 0.0}}
    }
    assert stored_state(tmp_path) == {ACTION: {"position": 70}}


async def test_state_change_does_not_write_the_settings(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drive only writes the file of the state."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.OPEN, 40)
    writes = count_writes(monkeypatch)

    action.apply_measurement(0.0, 70, None)
    await action.sync()

    assert writes == [DEST_STATE]


async def test_state_survives_restart(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Position and tilt are restored after a restart and marked as restored."""
    action = await new_action(make_control, tmp_path)
    action.apply_measurement(0.0, 35, 20)
    await action.sync()

    restarted = await new_action(make_control, tmp_path)

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
    writes = count_writes(monkeypatch)

    await action.sync()
    action.apply_measurement(0.0, 10, 10)
    await action.sync()
    await action.sync()

    assert writes == [DEST_STATE]


async def test_destination_sync_writes_each_file_once(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changes of several actions of a destination are stored with one write."""
    control = make_control(persist=str(tmp_path))
    await control.refresh()
    dest = control.dests[DEST_ID]
    writes = count_writes(monkeypatch)

    dest.actions[ACTION_ID].apply_measurement(0.0, 10, None)
    dest.actions[SECOND_ACTION_ID].apply_measurement(0.0, 20, None)
    await dest.sync()

    assert writes == [DEST_STATE]
    assert stored_state(tmp_path) == {
        ACTION: {"position": 10},
        str(SECOND_ACTION_ID): {"position": 20},
    }


async def test_each_destination_has_its_own_files(
    make_control: ControlFactory,
    gateway: FakeGateway,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change of one destination does not touch the files of another."""
    gateway.config = {
        **gateway.config,
        "destinations": [
            gateway.config["destinations"][0],
            {**gateway.config["destinations"][0], "id": OTHER_DEST_ID},
        ],
    }
    control = make_control(persist=str(tmp_path))
    await control.refresh()
    first = control.dests[DEST_ID].actions[ACTION_ID]
    other = control.dests[OTHER_DEST_ID].actions[ACTION_ID]
    await first.set_travel_time(TravelDirection.OPEN, 30)
    writes = count_writes(monkeypatch)

    await other.set_travel_time(TravelDirection.OPEN, 20)

    assert writes == [f"{OTHER_DEST_ID}/{SETTINGS_FILE}"]
    assert stored_settings(tmp_path)[ACTION]["settings"]["travel_time_open"] == 30.0
    assert stored_settings(tmp_path, OTHER_DEST_ID) == {
        ACTION: {"settings": {"travel_time_open": 20.0, "travel_time_close": 0.0}}
    }


async def test_files_of_an_unknown_destination_are_kept(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Data of a destination that the hub does not report is not touched."""
    unknown = dest_dir(tmp_path, OTHER_DEST_ID) / SETTINGS_FILE
    unknown.parent.mkdir()
    unknown.write_text("kept")
    action = await new_action(make_control, tmp_path)

    await action.set_travel_time(TravelDirection.OPEN, 30)

    assert unknown.read_text() == "kept"


async def test_destination_added_later_reads_its_files(
    make_control: ControlFactory, gateway: FakeGateway, tmp_path: Path
) -> None:
    """A destination that appears with a later refresh has its stored data."""
    control = make_control(persist=str(tmp_path))
    await control.refresh()
    await (
        control.dests[DEST_ID]
        .actions[ACTION_ID]
        .set_travel_time(TravelDirection.OPEN, 30)
    )
    other = dest_dir(tmp_path, OTHER_DEST_ID)
    other.mkdir()
    (other / SETTINGS_FILE).write_text((dest_dir(tmp_path) / SETTINGS_FILE).read_text())
    gateway.config = {
        **gateway.config,
        "destinations": [
            gateway.config["destinations"][0],
            {**gateway.config["destinations"][0], "id": OTHER_DEST_ID},
        ],
    }

    await control.refresh()

    added = control.dests[OTHER_DEST_ID].actions[ACTION_ID]
    assert added.settings.travel_time_open == 30.0


async def test_overwrite_change_is_saved_with_settings(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Changing an overwrite writes it together with the existing settings."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.OPEN, 30)

    action._attrs["minValue"] = 0
    action["minValue"] = -75
    await action.sync()

    assert stored_settings(tmp_path)[ACTION] == {
        "overwrites": {"minValue": -75},
        "settings": {"travel_time_open": 30.0, "travel_time_close": 0.0},
    }


async def test_destination_without_data_has_no_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Removing the last entry removes the file and the empty directory."""
    action = await new_action(make_control, tmp_path)
    action._attrs["minValue"] = 0
    action["minValue"] = -75
    await action.sync()

    del action["minValue"]
    await action.sync()

    assert is_empty(tmp_path)


async def test_removed_settings_keep_the_state_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Only the file without entries is removed, the other one stays."""
    action = await new_action(make_control, tmp_path)
    await action.set_travel_time(TravelDirection.OPEN, 30)
    action.apply_measurement(0.0, 70, None)
    await action.sync()

    await action.set_travel_time(TravelDirection.OPEN, 0)

    assert not (dest_dir(tmp_path) / SETTINGS_FILE).exists()
    assert stored_state(tmp_path) == {ACTION: {"position": 70}}


async def test_concurrent_writes_keep_a_valid_file(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many simultaneous changes end with the last value in a valid file."""
    action = await new_action(make_control, tmp_path)
    writes = count_writes(monkeypatch)

    await asyncio.gather(
        *(action.set_travel_time(TravelDirection.OPEN, i) for i in range(1, 41))
    )

    stored = stored_settings(tmp_path)[ACTION]["settings"]
    assert stored["travel_time_open"] == action.settings.travel_time_open == 40.0
    assert not temporary_files(dest_dir(tmp_path))
    # The first change is written at once, all later ones with a second write.
    assert writes == [DEST_SETTINGS, DEST_SETTINGS]


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 1, "actions": []}',
        '{"version": 1, "actions": {"1": "x"}}',
        '{"version": 1, "actions": {"1": {"settings": "x"}}}',
        '{"version": 1, "actions": {"1": {"overwrites": "x"}}}',
        '{"version": 1, "actions": {"1": {"settings": {"travel_time_open": -5}}}}',
        '{"version": 2, "actions": {"1": {"settings": {"travel_time_open": 5}}}}',
    ],
)
async def test_unusable_file_falls_back_to_defaults(
    make_control: ControlFactory, tmp_path: Path, content: str
) -> None:
    """A broken or invalid file does not break the action."""
    dest_dir(tmp_path).mkdir()
    (dest_dir(tmp_path) / SETTINGS_FILE).write_text(content)

    action = await new_action(make_control, tmp_path)
    await action.sync()

    assert action.settings == ActionSettings()
    assert action.rotation_range == (None, None)


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


async def test_file_that_is_no_text_falls_back_to_defaults(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A file with broken encoding is ignored like broken JSON."""
    dest_dir(tmp_path).mkdir()
    (dest_dir(tmp_path) / SETTINGS_FILE).write_bytes(b"\xff\xfe\x00")

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
    write_file(tmp_path, STATE_FILE, state)

    action = await new_action(make_control, tmp_path)

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
    replace = os.replace

    def fail(source: str, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    await action.set_travel_time(TravelDirection.OPEN, 12.0)
    assert "Could not write" in caplog.text
    assert is_empty(dest_dir(tmp_path))

    monkeypatch.setattr(os, "replace", replace)
    await action.sync()

    stored = stored_settings(tmp_path)[ACTION]["settings"]
    assert stored["travel_time_open"] == 12.0


async def test_change_during_a_write_is_stored_with_the_next_write(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change that arrives while a file is written is not lost."""
    action = await new_action(make_control, tmp_path)
    original = store._write
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_write(path: Path, content: str) -> None:
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        original(path, content)

    monkeypatch.setattr(store, "_write", slow_write)
    first = asyncio.create_task(action.set_travel_time(TravelDirection.OPEN, 10))
    await started.wait()
    monkeypatch.setattr(store, "_write", original)
    second = asyncio.create_task(action.set_travel_time(TravelDirection.OPEN, 20))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    stored = stored_settings(tmp_path)[ACTION]["settings"]
    assert stored["travel_time_open"] == 20.0
