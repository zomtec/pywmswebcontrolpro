"""Tests for the status refresh of a destination."""

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import aiohttp
import pytest

from pywmswebcontrolpro.destination import Destination
from pywmswebcontrolpro.exceptions import WebControlProConnectionError
from pywmswebcontrolpro.refresh import (
    RefreshCause,
    RefreshResult,
    RefreshStats,
    StaleChange,
)
from pywmswebcontrolpro.webcontrol import WebControlPro

from .conftest import (
    ACTION_ID,
    STATUS_BUSY,
    STATUS_EMPTY,
    STATUS_NO_DETAILS,
    FakeGateway,
    status_ok,
)


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        (status_ok(), RefreshResult.OK),
        (STATUS_BUSY, RefreshResult.BUSY),
        (STATUS_EMPTY, RefreshResult.EMPTY),
        (STATUS_NO_DETAILS, RefreshResult.EMPTY),
        (TimeoutError(), RefreshResult.ERROR),
        (aiohttp.ClientError(), RefreshResult.ERROR),
    ],
)
async def test_single_request_result(
    dest: Destination,
    gateway: FakeGateway,
    answer: dict[str, Any] | Exception,
    result: RefreshResult,
) -> None:
    """With retries=0 exactly one request is sent and its result is returned."""
    gateway.status_answers = [answer]

    outcome = await dest.refresh(retries=0)

    assert outcome.result is result
    assert bool(outcome) is (result is RefreshResult.OK)
    assert gateway.status_requests == 1
    assert getattr(dest.stats, result.value) == 1
    assert dest.stats.last_result is result


async def test_default_does_not_retry_on_busy(
    dest: Destination, gateway: FakeGateway
) -> None:
    """A call without arguments asks once, also while the radio is busy."""
    gateway.status_answers = [STATUS_BUSY]

    outcome = await dest.refresh()

    assert outcome.result is RefreshResult.BUSY
    assert gateway.status_requests == 1
    assert dest.stats.busy == 1


async def test_retries_ask_again_while_busy(
    dest: Destination, gateway: FakeGateway
) -> None:
    """With retries=2 a busy radio is asked three times."""
    gateway.status_answers = [STATUS_BUSY, STATUS_BUSY, STATUS_BUSY]

    outcome = await dest.refresh(retries=2)

    assert outcome.result is RefreshResult.BUSY
    assert gateway.status_requests == 3
    assert dest.stats.busy == 1


async def test_retry_succeeds_after_busy(
    dest: Destination, gateway: FakeGateway
) -> None:
    """A later attempt can still deliver the status."""
    gateway.status_answers = [STATUS_BUSY, status_ok(55)]

    outcome = await dest.refresh(retries=2)

    assert outcome.result is RefreshResult.OK
    assert outcome.values == {ACTION_ID: {"percentage": 55}}
    assert dest.actions[ACTION_ID]["percentage"] == 55
    assert gateway.status_requests == 2


@pytest.mark.parametrize("answer", [STATUS_EMPTY, TimeoutError()])
async def test_no_retry_on_empty_or_error(
    dest: Destination, gateway: FakeGateway, answer: dict[str, Any] | Exception
) -> None:
    """Only radio busy is retried."""
    gateway.status_answers = [answer]

    await dest.refresh()

    assert gateway.status_requests == 1


async def test_failure_keeps_previous_values(
    dest: Destination, gateway: FakeGateway
) -> None:
    """A failed refresh leaves the last known values in place."""
    gateway.status_answers = [status_ok(30), STATUS_BUSY]
    await dest.refresh(retries=0)

    await dest.refresh(retries=0)

    assert dest.actions[ACTION_ID]["percentage"] == 30
    assert dest.status["details"][0]["data"]["productData"][0]["value"] == {
        "percentage": 30
    }


async def test_raise_on_error(dest: Destination, gateway: FakeGateway) -> None:
    """Communication errors can be raised instead of counted."""
    gateway.status_answers = [TimeoutError()]

    with pytest.raises(WebControlProConnectionError):
        await dest.refresh(retries=0, raise_on_error=True)


async def test_stats_snapshot_is_detached(
    dest: Destination, gateway: FakeGateway
) -> None:
    """The public stats do not change with later refreshes."""
    gateway.status_answers = [status_ok(), STATUS_EMPTY]
    await dest.refresh(retries=0)
    snapshot = dest.stats

    await dest.refresh(retries=0)

    assert snapshot.empty == 0
    assert dest.stats.empty == 1


def test_success_rate_is_unknown_without_a_refresh() -> None:
    """Without a request there is no rate."""
    assert RefreshStats().success_rate is None


async def test_success_rate_is_the_share_of_refreshes_with_data(
    dest: Destination, gateway: FakeGateway
) -> None:
    """Busy, empty and failed refreshes lower the rate."""
    gateway.status_answers = [status_ok(), STATUS_BUSY, STATUS_EMPTY, TimeoutError()]
    for _ in range(4):
        await dest.refresh(retries=0)

    assert dest.stats.success_rate == 25.0


async def test_stats_of_the_client_combine_all_destinations(
    make_control: Callable[..., WebControlPro], gateway: FakeGateway
) -> None:
    """The client sums the counters and knows the latest refresh with data."""
    gateway.config = {
        **gateway.config,
        "destinations": [
            gateway.config["destinations"][0],
            {**gateway.config["destinations"][0], "id": 8},
        ],
    }
    control = make_control()
    await control.refresh()
    assert control.stats.success_rate is None
    assert control.stats.last_ok is None
    gateway.status_answers = [status_ok(), STATUS_BUSY]

    for dest in control.dests.values():
        await dest.refresh(retries=0)

    assert control.stats.ok == 1
    assert control.stats.busy == 1
    assert control.stats.success_rate == 50.0
    assert control.stats.last_ok == control.dests[7].stats.last_ok


async def test_diag_contains_stats(dest: Destination, gateway: FakeGateway) -> None:
    """Diagnostics include the counters."""
    gateway.status_answers = [status_ok()]
    await dest.refresh(retries=0)

    stats = dest.diag()["stats"]

    assert stats["ok"] == 1
    assert stats["last_result"] == "ok"
    assert stats["last_ok"] is not None


async def test_stale_warning_once_and_recovery(
    make_control: Callable[..., WebControlPro],
    gateway: FakeGateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A silent destination is reported once and again when it recovers."""
    control = make_control(stale_warning_after=timedelta(0))
    await control.refresh()
    destination = control.dests[7]
    gateway.status_answers = [STATUS_BUSY, STATUS_BUSY, STATUS_BUSY, status_ok()]

    for _ in range(4):
        await destination.refresh(retries=0)

    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(messages) == 2
    assert messages[0].startswith("No status for Shutter")
    assert messages[1] == "Status for Shutter (7) is available again"


async def test_no_log_for_single_failures(
    dest: Destination, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """Failed refreshes are counted, not logged."""
    gateway.status_answers = [STATUS_BUSY, STATUS_EMPTY, TimeoutError()]

    for _ in range(3):
        await dest.refresh(retries=0)

    assert not caplog.records


@pytest.mark.parametrize(
    ("results", "changes"),
    [
        ([RefreshResult.OK, RefreshResult.OK], [None, None]),
        ([RefreshResult.BUSY, RefreshResult.BUSY], [None, StaleChange.STALE]),
        (
            [RefreshResult.BUSY, RefreshResult.BUSY, RefreshResult.BUSY],
            [None, StaleChange.STALE, None],
        ),
        (
            [RefreshResult.BUSY, RefreshResult.BUSY, RefreshResult.OK],
            [None, StaleChange.STALE, StaleChange.RECOVERED],
        ),
        ([RefreshResult.BUSY, RefreshResult.OK], [None, None]),
    ],
)
def test_stats_stale_changes(
    results: list[RefreshResult], changes: list[StaleChange | None]
) -> None:
    """Stale and recovered are reported on state changes only."""
    stats = RefreshStats()

    reported = [
        stats.record(result, now=float(i), stale_after=1.0)
        for i, result in enumerate(results)
    ]

    assert reported == changes


async def test_request_spacing_delays_the_next_status_request(
    make_control: Callable[..., WebControlPro],
    gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two status requests are at least request_spacing seconds apart."""
    control = make_control(request_spacing=0.25)
    await control.refresh()
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("pywmswebcontrolpro.webcontrol.asyncio.sleep", fake_sleep)
    gateway.status_answers = [status_ok(), status_ok()]

    await control.dests[7].refresh(retries=0)
    await control.dests[7].refresh(retries=0)

    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.25


@pytest.mark.parametrize(
    ("answer", "answered"),
    [
        (status_ok(), True),
        (STATUS_BUSY, True),
        (STATUS_EMPTY, True),
        (TimeoutError(), False),
    ],
)
async def test_last_duration_is_the_answer_time_of_the_last_request(
    dest: Destination,
    gateway: FakeGateway,
    answer: dict[str, Any] | Exception,
    answered: bool,
) -> None:
    """Every answer of the hub has a duration, a request without answer has none."""
    gateway.status_answers = [status_ok(), answer]
    await dest.refresh()

    await dest.refresh()

    assert (dest.stats.last_duration is not None) is answered
    assert dest.diag()["stats"]["last_duration"] == dest.stats.last_duration


async def test_last_duration_excludes_the_request_spacing(
    make_control: Callable[..., WebControlPro],
    gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pause before a request does not count as answer time of the hub."""
    control = make_control(request_spacing=60)
    await control.refresh()
    clock = [1000.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    monkeypatch.setattr("pywmswebcontrolpro.webcontrol.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "pywmswebcontrolpro.webcontrol.time.monotonic", lambda: clock[0]
    )
    gateway.status_answers = [status_ok(), status_ok()]
    await control.dests[7].refresh()

    await control.dests[7].refresh()

    assert clock[0] == 1060.0
    assert control.dests[7].stats.last_duration == 0.0


async def test_results_are_counted_for_each_cause(
    dest: Destination, gateway: FakeGateway
) -> None:
    """Regular requests and requests after a command have their own counters."""
    gateway.status_answers = [status_ok(), STATUS_BUSY, STATUS_BUSY]

    await dest.refresh(cause=RefreshCause.SCHEDULED)
    await dest.refresh(cause=RefreshCause.SCHEDULED)
    await dest.refresh()

    assert dest.stats.causes == {
        RefreshCause.SCHEDULED: {RefreshResult.OK: 1, RefreshResult.BUSY: 1},
        RefreshCause.COMMAND: {RefreshResult.BUSY: 1},
    }
    assert dest.stats.busy == 2
    assert dest.diag()["stats"]["causes"] == {
        "scheduled": {"ok": 1, "busy": 1},
        "command": {"busy": 1},
    }


def test_recent_success_rate_is_unknown_without_a_refresh() -> None:
    """Without a request in the window there is no recent rate."""
    assert RefreshStats().recent_success_rate is None


def test_recent_success_rate_forgets_results_older_than_the_window() -> None:
    """Only the refreshes of the last window seconds count."""
    stats = RefreshStats(window=100.0)
    now = time.monotonic()

    stats.record(RefreshResult.BUSY, now=now - 150, stale_after=1e9)
    stats.record(RefreshResult.BUSY, now=now - 50, stale_after=1e9)
    stats.record(RefreshResult.OK, now=now, stale_after=1e9)

    assert stats.recent_success_rate == 50.0
    assert stats.success_rate == pytest.approx(100 / 3)
    assert len(stats.recent) == 2


def test_recent_success_rate_ends_when_the_last_refresh_is_too_old() -> None:
    """A destination that was not asked within the window has no recent rate."""
    stats = RefreshStats(window=100.0)

    stats.record(RefreshResult.OK, now=time.monotonic() - 150, stale_after=1e9)

    assert stats.recent_success_rate is None


async def test_stats_window_of_the_client_is_used_by_its_destinations(
    make_control: Callable[..., WebControlPro],
) -> None:
    """The window of the recent rate is set when the client is created."""
    control = make_control(stats_window=timedelta(hours=1))
    await control.refresh()

    assert control.dests[7].stats.window == 3600.0
    assert control.stats.window == 3600.0


async def test_stats_of_the_client_combine_causes_and_recent_results(
    make_control: Callable[..., WebControlPro], gateway: FakeGateway
) -> None:
    """The client sums the counters of the causes and the recent results."""
    gateway.config = {
        **gateway.config,
        "destinations": [
            gateway.config["destinations"][0],
            {**gateway.config["destinations"][0], "id": 8},
        ],
    }
    control = make_control()
    await control.refresh()
    assert control.stats.recent_success_rate is None
    gateway.status_answers = [status_ok(), STATUS_BUSY, STATUS_BUSY, STATUS_BUSY]

    for dest in control.dests.values():
        await dest.refresh(cause=RefreshCause.SCHEDULED)
        await dest.refresh(cause=RefreshCause.SCHEDULED)

    assert control.stats.causes == {
        RefreshCause.SCHEDULED: {RefreshResult.OK: 1, RefreshResult.BUSY: 3}
    }
    assert control.stats.recent_success_rate == 25.0
    assert control.stats.last_duration is None


async def test_stats_snapshot_keeps_its_causes_and_recent_results(
    dest: Destination, gateway: FakeGateway
) -> None:
    """Later refreshes do not change the causes and recent results of a snapshot."""
    gateway.status_answers = [status_ok(), STATUS_BUSY]
    await dest.refresh()
    snapshot = dest.stats

    await dest.refresh()

    assert snapshot.causes == {RefreshCause.COMMAND: {RefreshResult.OK: 1}}
    assert snapshot.recent_success_rate == 100.0
    assert dest.stats.recent_success_rate == 50.0
