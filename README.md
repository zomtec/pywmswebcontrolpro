# pywmswebcontrolpro

Python library for the Warema WMS WebControl pro API. It is the companion library of the
Home Assistant integration `ha_wms_webcontrol_pro`.

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

`Destination.refresh(retries=2)` requests the status of a destination and returns a
`RefreshOutcome` with the result (`ok`, `busy`, `empty` or `error`) and the values
of the actions. Only a "radio busy" answer is retried; with `retries=0` exactly one
request is sent. On any failure the previous values stay unchanged. The outcome is
truthy only for `ok`.

Failed refreshes are counted per destination (`Destination.stats`, also part of
`diag()`) instead of being logged. A warning is logged once when a destination has
failed continuously for `stale_warning_after` (default 24 hours) and once when it
delivers data again.

## Development

```bash
python -m pip install -r requirements_lint.txt -r requirements_test.txt
ruff check . && ruff format --check .
python -m pytest
```

The version is derived from git tags (`hatch-vcs`).
