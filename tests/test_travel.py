"""Tests of the movement model of a cover."""

import pytest

from pywmswebcontrolpro import travel
from pywmswebcontrolpro.travel import CoverModel, PositionSource as Source


def known(position=100, tilt=100):
    model = CoverModel()
    model.apply_measurement(0.0, position, tilt)
    return model


def test_t01_conversion():
    conv = travel.wms_percentage_to_position
    assert conv(0) == 100
    assert conv(100) == 0
    assert conv(37.4) == 63
    assert conv(127.5) is None
    assert conv(-1) is None
    assert conv(None) is None
    assert conv(True) is None
    assert conv("50") is None


def test_t02_unknown_start_sets_target():
    model = CoverModel()
    delay = model.start_move(10.0, 0, 40, 50, tilt=0)
    assert delay == 120.0
    assert model.position_int == 0
    assert model.tilt == 0
    assert not model.is_travelling
    assert model.source is Source.CALCULATED


def test_t03_zero_time_sets_target():
    model = known()
    delay = model.start_move(10.0, 0, 0, 0, tilt=20)
    assert delay == 120.0
    assert model.position_int == 0
    assert model.tilt == 20
    assert not model.is_travelling


def test_t04_closing_interpolates():
    model = known(100, 100)
    delay = model.start_move(10.0, 0, 40, 50, tilt=0)
    assert delay == 52.0
    assert model.is_closing
    assert not model.is_opening
    assert model.position_int == 100
    assert model.tilt == 100
    assert model.advance(35.0) is False
    assert model.position_int == 50
    assert model.advance(59.9) is False
    assert model.position_int == 0
    assert model.is_travelling
    assert model.advance(60.0) is True
    assert model.position_int == 0
    assert model.tilt == 0
    assert not model.is_travelling
    assert model.advance(61.0) is False


def test_t05_opening_uses_open_time_and_partial_distance():
    model = known(50, 0)
    delay = model.start_move(0.0, 100, 40, 50)
    assert delay == 22.0
    assert model.is_opening
    model.advance(10.0)
    assert model.position_int == 75
    assert model.advance(20.0) is True
    assert model.position_int == 100
    assert model.tilt == 0


def test_t06_stop_freezes():
    model = known(100, 100)
    model.start_move(0.0, 0, 40, 50, tilt=0)
    delay = model.stop(12.5)
    assert delay == 2.0
    assert model.position_int == 75
    assert model.tilt == 100
    assert not model.is_travelling
    model.advance(100.0)
    assert model.position_int == 75


def test_t07_new_command_during_travel_reverses():
    model = known(100, 100)
    model.start_move(0.0, 0, 40, 50)
    delay = model.start_move(25.0, 100, 40, 50)
    assert model.is_opening
    assert delay == 22.0
    model.advance(35.0)
    assert model.position_int == 75
    assert model.advance(45.0) is True
    assert model.position_int == 100


def test_t08_same_target_no_travel():
    model = known(0, 0)
    delay = model.start_move(0.0, 0, 40, 50, tilt=30)
    assert delay == 5.0
    assert not model.is_travelling
    assert model.tilt == 30


def test_t09_tilt_command_ends_travel():
    model = known(100, 100)
    model.start_move(0.0, 0, 40, 50, tilt=0)
    delay = model.set_tilt(25.0, 40)
    assert delay == 5.0
    assert model.position_int == 50
    assert model.tilt == 40
    assert not model.is_travelling


def test_t10_measurement_ignored_during_travel():
    model = known(100, 100)
    model.start_move(0.0, 0, 40, 50)
    result = model.apply_measurement(10.0, 90, 50)
    assert result == travel.Measurement(False, False)
    assert model.position_int == 80
    assert model.source is Source.CALCULATED


def test_t11_measurement_after_travel_and_deviation():
    model = known(100, 100)
    model.start_move(0.0, 0, 40, 50, tilt=0)
    result = model.apply_measurement(52.0, 2, 5)
    assert result == travel.Measurement(True, False)
    assert model.position_int == 2
    assert model.tilt == 5
    assert model.source is Source.MEASURED
    result = model.apply_measurement(60.0, 40, 5)
    assert result == travel.Measurement(True, True)
    result = model.apply_measurement(70.0, 43, 5)
    assert result == travel.Measurement(True, False)


def test_t12_measurement_without_position():
    model = known(60, 10)
    result = model.apply_measurement(5.0, None, 90)
    assert result == travel.Measurement(False, False)
    assert model.position_int == 60
    assert model.tilt == 10


def test_t13_first_measurement_is_no_deviation():
    model = CoverModel()
    assert model.apply_measurement(0.0, 0, 0) == travel.Measurement(True, False)


@pytest.mark.parametrize(
    ("known_position", "restore_args", "expected"),
    [
        (None, (40, 20, None, None), (40, 20, Source.RESTORED)),
        (None, (40, 20, 0, 5), (0, 5, Source.RESTORED)),
        (None, (40, 20, 0, None), (0, 20, Source.RESTORED)),
        (None, (None, None, None, None), (None, None, None)),
        (10, (40, 20, None, None), (10, 10, Source.MEASURED)),
    ],
)
def test_t14_restore(
    known_position: int | None,
    restore_args: tuple,
    expected: tuple,
) -> None:
    model = CoverModel() if known_position is None else known(known_position, 10)

    model.restore(*restore_args)

    assert (model.position_int, model.tilt, model.source) == expected


def test_t15_snapshot():
    model = known(100, 100)
    assert model.snapshot() == {
        "position": 100,
        "tilt": 100,
        "target_position": None,
        "target_tilt": None,
    }
    model.start_move(0.0, 0, 40, 50, tilt=0)
    model.advance(25.0)
    assert model.snapshot() == {
        "position": 50,
        "tilt": 100,
        "target_position": 0,
        "target_tilt": 0,
    }


def test_t16_stop_without_position_keeps_source():
    model = CoverModel()
    assert model.stop(1.0) == 2.0
    assert model.source is None
    assert model.position is None


def test_t17_target_is_clamped():
    model = known(50, 0)
    model.start_move(0.0, 150, 40, 50)
    model.advance(1000.0)
    assert model.position_int == 100
