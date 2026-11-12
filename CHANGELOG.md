# Changelog

## 0.2.0

Breaking changes:

- Requests raise the errors of the library (`WebControlProConnectionError`,
  `WebControlProResponseError`) instead of `aiohttp.ClientError` and `TimeoutError`.
- An action or a scene that the gateway rejects raises `WebControlProCommandError`
  instead of returning the answer with its `errors`.
- `Destination.refresh()` does not ask again by default when the radio is busy
  (`retries=0`, was 2).
- A rotation action whose limits the hub reports as -127 and 127 uses -75 and 75
  (`minValue`, `maxValue`, `rotation_range`). `wms__minValue`, `wms__maxValue` and
  `reported_rotation_range` return the limits of the hub.
- Actions of different destinations with the same id are no longer equal.

Added:

- Workaround for the firmware bug of the rotation limits in the library: default
  limits, learning from a larger measured rotation, `Action.set_rotation_range`,
  `Action.reset_rotation_range`, `Action.rotation_to_tilt` and
  `Action.tilt_to_rotation`. The conversion is linear and sends whole numbers within
  the limits.
- Unknown enumeration values of a newer firmware become `Unknown`, unknown fields of
  the configuration are ignored.

Fixed:

- The HTTP status of an answer is checked, an answer that is no JSON object is an
  error of the library.
- A configuration that is not understood leaves the known destinations unchanged.
- A status with an error other than radio busy counts as `error` instead of `empty`
  and does not replace the last status.
- A change during the first load of the stored file no longer replaces the file with
  defaults. A change during a write is stored with the next sync.
- The stored file is written through a temporary file of its own and synced to disk.
  A failed write is logged and tried again, a file with a broken encoding or an
  invalid stored position is ignored.
- Ids of destinations and actions must be numbers, they are part of a file path.

## 0.1.2

- Keep known destinations when the configuration is read again.

## 0.1.1

- Docstrings, type hints and `py.typed`.

## 0.1.0

- First release as `pywmswebcontrolpro`, forked from `pywmspro` by Marc Hoersken.
- Refresh outcome and counters per destination, settings, movement model and stored
  state per drive action.
- The spacing of status requests is a constructor argument.
