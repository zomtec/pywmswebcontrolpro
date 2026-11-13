# pywmswebcontrolpro

Python library for the Warema WMS WebControl pro API. It is the companion library of the
Home Assistant integration [`wms_webcontrol_pro`](https://github.com/zomtec/ha_wms_webcontrol_pro).

Reference: https://media.warema.com/dokumente/anleitungen-handbuecher/966664/warema_2064534_alhb_de_v0.pdf

## Origin and license

This project is a fork of [`pywmspro`](https://github.com/mback2k/pywmspro) by
Marc Hoersken, licensed under the Apache License 2.0. The full history of the original
is kept in this repository. All files changed since the fork are changed by this
project's contributors; see the git history for details. The license text is in
`LICENSE` and applies to this repository unchanged.

## Example usage

See the main section in `pywmswebcontrolpro/webcontrol.py`.

## Status refresh

`Destination.refresh()` requests the status of a destination once and returns a
`RefreshOutcome` with the result (`ok`, `busy`, `empty` or `error`) and the values
of the actions. A "radio busy" answer is not retried by default, because every request
adds traffic to the radio channel; `retries=2` asks up to two more times. On any
failure the previous values stay unchanged. The outcome is truthy only for `ok`.

`WebControlPro(request_spacing=...)` sets the minimum pause between two status requests
to the hub (default 0.5 seconds).

Failed refreshes are counted per destination (`Destination.stats`, also part of
`diag()`) instead of being logged. `RefreshStats.success_rate` is the share of
refreshes with data in percent, `RefreshStats.last_ok` the time of the last one, and
`WebControlPro.stats` combines all destinations. A warning is logged once when a
destination has failed continuously for `stale_warning_after` (default 24 hours) and
once when it delivers data again.

## Errors

All errors of the library derive from `WebControlProError`
(`pywmswebcontrolpro.exceptions`):

- `WebControlProConnectionError`: the gateway did not answer (connection error or
  timeout of 5 seconds).
- `WebControlProResponseError`: the gateway answered with an HTTP error or with
  something that is not understood. A configuration that is not understood leaves the
  known destinations unchanged.
- `WebControlProCommandError`: the gateway rejected an action or a scene, for example
  because the radio channel is busy. `errors` holds the codes of the gateway. A
  rejected command is not sent again.

`Destination.refresh()` raises none of them unless `raise_on_error=True` is passed, it
reports them as result `error`. Fields and enumeration values that a newer firmware
adds are accepted, an unknown value becomes `Unknown`.

## Security

The gateway offers its API only over HTTP without authentication, so the library
cannot encrypt or authenticate the connection. Run the gateway in a trusted network.

## Settings, movement model and stored state

The client keeps the data of all actions of a hub in two files of the `persist`
directory:

- `settings.json`: what was set or learned and cannot be measured again. For a drive
  action (for example `AwningDrive`, `ValanceDrive` or `RollerShutterBlindDrive`) these
  are the travel times for a full drive, open and close, in seconds
  (`Action.set_travel_time`, 0 means unknown), for a rotation action the `overwrites`
  of the rotation limits.
- `state.json`: the last known position and tilt of a drive action and the target of a
  running drive.

Both files have an entry for each destination and action, and only for data that
differs from the defaults:

```json
{"version": 1, "destinations": {"169520": {"0": {"settings": {"travel_time_open": 39.0, "travel_time_close": 39.0}}, "6": {"overwrites": {"minValue": -70}}}}}
```

The store of the client (`pywmswebcontrolpro/store.py`) is the only place that reads
and writes the files. It reads them once with the first `WebControlPro.refresh()`, an
action has its stored data from then on. Writes are serialized and replace a file
atomically, changes that arrive during a write are stored together with the next one.
A drive only writes `state.json`. Entries of destinations that the hub does not report
are kept. Without a persist path nothing is stored.

`Action.model` is the movement model of the cover driven by this action: `start_move`,
`stop`, `set_tilt` and `apply_measurement` calculate the displayed position, which is
replaced by a measured value when the cover stands still. Because the model belongs to
the drive action, an awning and its valance, which share a destination, move
independently. After a restart the position is restored from the file and marked as
`restored` until a measurement arrives. Call `Action.sync()` after commands and
measurements to store changes; `Destination.refresh` does it as well.

## Rotation limits of slat drives (firmware bug)

The hub reports the rotation limits of slat drives as -127 and 127. The drives turn
from -75 to 75, so a tilt calculated from the reported limits is wrong: closed slats
(rotation 75) would be shown as 21 % open. Warema knows the bug, a fixed firmware has
not been announced.

The library works around it in `pywmswebcontrolpro/action.py`:

- While the hub reports exactly -127 and 127 for a rotation action, `Action.rotation_range`
  is -75 to 75. `Action.reported_rotation_range` returns the limits of the hub.
- A rotation that is measured beyond the range in use widens it. The limits of the hub
  themselves (-127 and 127) are never learned.
- `Action.set_rotation_range` overwrites the limits, `Action.reset_rotation_range`
  returns to the default. Both are stored in `settings.json`.
- `Action.rotation_to_tilt` and `Action.tilt_to_rotation` convert with the limits in
  use. The minimum rotation is tilt 100, the maximum rotation is tilt 0, and the
  rotation that is sent is a whole number within the limits.

The workaround ends by itself when a firmware reports other limits. It can be removed
from the library once such a firmware is available.

## Development

```bash
python -m pip install -r requirements_lint.txt -r requirements_test.txt
ruff check . && ruff format --check .
python -m pytest
```

The version is derived from git tags (`hatch-vcs`).
