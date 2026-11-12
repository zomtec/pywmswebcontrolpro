"""Mock gateway for tests: a fake aiohttp session that replays queued answers."""

from collections.abc import Callable
from types import TracebackType
from typing import Any

import pytest

from pywmswebcontrolpro.const import (
    WMS_WebControl_pro_API_actionDescription,
    WMS_WebControl_pro_API_actionType,
)
from pywmswebcontrolpro.destination import Destination
from pywmswebcontrolpro.refresh import ERROR_RADIO_BUSY
from pywmswebcontrolpro.webcontrol import WebControlPro

DEST_ID = 7
ACTION_ID = 1

CONFIG = {
    "destinations": [
        {
            "id": DEST_ID,
            "names": ["Shutter"],
            "animationType": 2,
            "actions": [
                {
                    "id": ACTION_ID,
                    "actionType": WMS_WebControl_pro_API_actionType.Percentage,
                    "actionDescription": (
                        WMS_WebControl_pro_API_actionDescription.RollerShutterBlindDrive
                    ),
                }
            ],
        }
    ],
    "rooms": [],
    "scenes": [],
}


def status_ok(percentage: int = 40) -> dict[str, Any]:
    """Return a status answer with a value for the test action."""
    return {
        "details": [
            {
                "destinationId": DEST_ID,
                "data": {
                    "drivingCause": 0,
                    "productData": [
                        {"actionId": ACTION_ID, "value": {"percentage": percentage}}
                    ],
                },
            }
        ]
    }


STATUS_BUSY = {"errors": [ERROR_RADIO_BUSY]}
STATUS_EMPTY: dict[str, Any] = {}
STATUS_NO_DETAILS = {"details": []}


class FakeResponse:
    """Response of the fake gateway."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        """Return the queued payload."""
        return self._payload


class FakeRequest:
    """Async context manager returned by the fake session."""

    def __init__(self, answer: dict[str, Any] | Exception) -> None:
        self._answer = answer

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._answer, Exception):
            raise self._answer
        return FakeResponse(self._answer)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class FakeGateway:
    """Fake session that answers getStatus from a queue and records requests."""

    def __init__(self) -> None:
        self.status_answers: list[dict[str, Any] | Exception] = []
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any], timeout: int) -> FakeRequest:
        """Answer one request of the library."""
        self.requests.append(json)
        if json["command"] == "getConfiguration":
            return FakeRequest(CONFIG)
        return FakeRequest(self.status_answers.pop(0))

    @property
    def status_requests(self) -> int:
        """Return the number of getStatus requests sent so far."""
        return sum(r["command"] == "getStatus" for r in self.requests)


@pytest.fixture
def gateway() -> FakeGateway:
    """Return a fake gateway."""
    return FakeGateway()


@pytest.fixture
def make_control(gateway: FakeGateway) -> Callable[..., WebControlPro]:
    """Return a factory for a client connected to the fake gateway."""

    def factory(**kwargs: Any) -> WebControlPro:
        kwargs.setdefault("retry_delay", 0)
        control = WebControlPro("gateway", gateway, **kwargs)  # type: ignore[arg-type]  # fake session
        control._status_wait = 0
        return control

    return factory


@pytest.fixture
async def dest(make_control: Callable[..., WebControlPro]) -> Destination:
    """Return the test destination."""
    control = make_control()
    await control.refresh()
    return control.dests[DEST_ID]
