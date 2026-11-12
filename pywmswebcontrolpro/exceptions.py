"""Errors of the communication with the WMS WebControl pro gateway."""


class WebControlProError(Exception):
    """Base of all errors of this library."""


class WebControlProConnectionError(WebControlProError):
    """The gateway did not answer."""


class WebControlProResponseError(WebControlProError):
    """The gateway answered with something that is not understood."""


class WebControlProCommandError(WebControlProError):
    """The gateway rejected a command, for example because the radio is busy."""

    def __init__(self, errors: list) -> None:
        """Keep the error codes of the gateway."""
        super().__init__(f"The gateway rejected the command: {errors}")
        self.errors = errors
