"""Tests of the stored settings and last state of a destination."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from pywmswebcontrolpro.destination import Destination
from pywmswebcontrolpro.store import DestinationSettings, TravelDirection
from pywmswebcontrolpro.travel import PositionSource
from pywmswebcontrolpro.webcontrol import WebControlPro

from .conftest import DEST_ID

type ControlFactory = Callable[..., WebControlPro]


async def new_dest(make_control: ControlFactory, persist: Path | None) -> Destination:
    """Return the test destination of a fresh client, like after a restart."""
    control = make_control(persist=str(persist) if persist else None)
    await control.refresh()
    return control.dests[DEST_ID]


def settings_file(persist: Path) -> Path:
    """Return the settings file of the test destination."""
    return persist / str(DEST_ID) / "settings.json"


def is_empty(path: Path) -> bool:
    """Return True if the directory has no entries."""
    return not any(path.iterdir())


async def test_defaults_without_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Without a stored file the travel times are 0 and nothing is written."""
    dest = await new_dest(make_control, tmp_path)

    await dest.sync()

    assert dest.settings == DestinationSettings()
    assert not settings_file(tmp_path).exists()


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
    dest = await new_dest(make_control, tmp_path)
    await dest.set_travel_time(direction, seconds)

    restarted = await new_dest(make_control, tmp_path)
    await restarted.sync()

    assert restarted.settings.travel_time(direction) == seconds


async def test_set_travel_time_keeps_other_direction(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Setting one direction does not change the other."""
    dest = await new_dest(make_control, tmp_path)

    await dest.set_travel_time(TravelDirection.OPEN, 40)
    await dest.set_travel_time(TravelDirection.CLOSE, 50)

    assert dest.settings == DestinationSettings(40.0, 50.0)


@pytest.mark.parametrize("seconds", [-1, float("nan")])
async def test_invalid_travel_time_is_rejected(
    make_control: ControlFactory, tmp_path: Path, seconds: float
) -> None:
    """Negative and non-numeric travel times are refused."""
    dest = await new_dest(make_control, tmp_path)

    with pytest.raises(ValueError, match="must not be negative"):
        await dest.set_travel_time(TravelDirection.OPEN, seconds)

    assert not settings_file(tmp_path).exists()


async def test_state_survives_restart(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Position and tilt are restored after a restart and marked as restored."""
    dest = await new_dest(make_control, tmp_path)
    await dest.sync()
    dest.apply_measurement(0.0, 35, 20)
    await dest.sync()

    restarted = await new_dest(make_control, tmp_path)
    await restarted.sync()

    assert restarted.model.position_int == 35
    assert restarted.model.tilt == 20
    assert restarted.model.source is PositionSource.RESTORED


async def test_running_drive_is_restored_as_target(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A drive that was running at shutdown is restored at its target."""
    dest = await new_dest(make_control, tmp_path)
    await dest.set_travel_time(TravelDirection.CLOSE, 40)
    await dest.sync()
    dest.apply_measurement(0.0, 100, 100)
    dest.start_move(0.0, 0, tilt=0)
    await dest.sync()

    restarted = await new_dest(make_control, tmp_path)
    await restarted.sync()

    assert restarted.model.position_int == 0
    assert restarted.model.tilt == 0
    assert restarted.settings.travel_time_close == 40


async def test_start_move_uses_stored_travel_times(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """The drive duration follows the stored time of the direction."""
    dest = await new_dest(make_control, tmp_path)
    await dest.set_travel_time(TravelDirection.CLOSE, 50)
    dest.apply_measurement(0.0, 100, 100)

    delay = dest.start_move(10.0, 0)

    assert delay == 52.0
    assert dest.model.is_closing


async def test_state_is_written_only_on_change(
    make_control: ControlFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated syncs without a change do not rewrite the file."""
    dest = await new_dest(make_control, tmp_path)
    saves = []
    original = dest._store.save

    async def counting_save(*args: object) -> None:
        saves.append(args)
        await original(*args)

    monkeypatch.setattr(dest._store, "save", counting_save)

    await dest.sync()
    dest.apply_measurement(0.0, 10, 10)
    await dest.sync()
    await dest.sync()

    assert len(saves) == 1


async def test_concurrent_writes_keep_a_valid_file(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Many simultaneous writes end with the last value in a valid file."""
    dest = await new_dest(make_control, tmp_path)

    await asyncio.gather(
        *(dest.set_travel_time(TravelDirection.OPEN, i) for i in range(1, 41))
    )

    stored = json.loads(settings_file(tmp_path).read_text())
    assert stored["settings"]["travel_time_open"] == dest.settings.travel_time_open
    assert not list(settings_file(tmp_path).parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"settings": "x"}', '{"settings": {"travel_time_open": -5}}'],
)
async def test_unusable_file_falls_back_to_defaults(
    make_control: ControlFactory, tmp_path: Path, content: str
) -> None:
    """A broken or invalid file does not break the destination."""
    path = settings_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)

    dest = await new_dest(make_control, tmp_path)
    await dest.sync()

    assert dest.settings == DestinationSettings()


async def test_without_persist_path_nothing_is_stored(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """A client without a persist path keeps the settings in memory only."""
    dest = await new_dest(make_control, None)

    await dest.set_travel_time(TravelDirection.OPEN, 30)

    assert dest.settings.travel_time_open == 30
    assert is_empty(tmp_path)


async def test_diag_contains_settings_and_model(
    make_control: ControlFactory, tmp_path: Path
) -> None:
    """Diagnostics show settings and the displayed state with its origin."""
    dest = await new_dest(make_control, tmp_path)
    await dest.set_travel_time(TravelDirection.OPEN, 30)
    dest.apply_measurement(0.0, 60, 0)

    diag = dest.diag()

    assert diag["settings"] == {"travel_time_open": 30.0, "travel_time_close": 0.0}
    assert diag["model"]["position"] == 60
    assert diag["model"]["source"] == "measured"
