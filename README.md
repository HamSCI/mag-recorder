# mag-recorder

Sigmond client (CONTRACT v0.6, §16 non-radiod data source) that records
3-axis magnetic-field samples from a PNI RM3100 magnetometer attached
via a Pololu Isolated USB-to-I²C adapter, and uploads the day's data
to the HamSCI PSWS network.

`mag-recorder` is a thin Python supervisor on top of Dave Witten's
upstream [mag-usb](https://github.com/wittend/mag-usb) C utility (GPL
3.0).  The C binary is responsible for talking to the sensor (I²C
POLL, DRDY busy-wait, 9-byte XYZ read, MCP9808 ambient temperature
read) and emitting one JSON line per UTC second; this wrapper owns the
sigmond contract surface, the JSONL spool, the daily-zip packaging,
and the SFTP upload to `pswsnetwork.eng.ua.edu`.  No hardware?  See
`mag-recorder daemon --simulate` for synthetic JSONL while you wait
for a "pi eliminator" to arrive.

## Layout

| Path | Purpose |
|---|---|
| `src/mag_recorder/cli.py` | argparse entry point; `inventory`, `validate`, `version`, `config init|edit`, `daemon` |
| `src/mag_recorder/contract.py` | CONTRACT v0.6 inventory + validate JSON builders |
| `src/mag_recorder/config.py` | TOML loader, defaults, env-var fallbacks |
| `src/mag_recorder/configurator.py` | §14 `config init/edit` interview |
| `src/mag_recorder/core/simulator.py` | Synthetic RM3100+MCP9808 JSONL stream (no hardware needed) |
| `src/mag_recorder/core/supervisor.py` | Reads JSONL stream (mag-usb subprocess or simulator), re-stamps, writes spool |
| `systemd/mag-recorder.service` | systemd unit (single instance per host) |
| `config/mag-recorder-config.toml.template` | `[station]`, `[mag]`, `[paths]`, `[uploader]` schema |
| `deploy.toml` | sigmond install manifest |

## PSWS identity

| Field | Value |
|---|---|
| Station ID | `S000082` |
| Instrument ID | `RM3100` |
| Upload host | `pswsnetwork.eng.ua.edu` |
| SSH key | shared with hf-timestd Grape uploader (`/etc/hs-uploader/keys/id_ed25519`) |
| Daily artifact | `OBS<YYYY-MM-DD>T00:00.zip` containing the day's JSONL |
| Trigger directory | `c<dataset_name>_#<instrument_id>_#<timestamp>` (mkdir after the put, Grape convention) |

## JSONL line format

`mag-recorder` re-emits upstream `mag-usb` lines with a millisecond-precision
ISO-8601 UTC timestamp:

```json
{"ts":"2026-05-12T23:45:01.000Z","rt":23.125,"x":12345.678,"y":-234.500,"z":987.001}
```

| field | unit | source |
|---|---|---|
| `ts` | ISO-8601 UTC, ms precision | re-stamped by supervisor |
| `rt` | °C, MCP9808 ambient | passthrough from mag-usb `rt` |
| `x`, `y`, `z` | nanoTesla, RM3100 axes after `[mag_orientation]` rotations | passthrough from mag-usb |

## Status

Scaffold in progress.  No hardware available yet; the supervisor's
data source is the simulator until a Pololu adapter and RM3100 board
are wired up.  See `project_mag_recorder.md` in the auto-memory for
the current to-do list.
