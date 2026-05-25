# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**mag-recorder** is a sigmond client that records 3-axis magnetic-field
samples from a PNI RM3100 magnetometer (over a Pololu Isolated
USB-to-I²C adapter) and uploads daily datasets to the HamSCI Personal
Space Weather Station (PSWS) network.

The hardware side is handled by Dave Witten's upstream
[`mag-usb`](https://github.com/wittend/mag-usb) C utility (GPL-3.0).
This Python repo is a thin supervisor that owns everything sigmond cares
about: contract surface, JSONL spool, daily zip packaging, and the
SFTP upload pipeline.

Part of the HamSCI sigmond suite — see `/opt/git/sigmond/sigmond/CLAUDE.md`
(orchestrator) and `/opt/git/sigmond/CLAUDE.md` (umbrella) for
cross-repo context. mag-recorder is the **first** consumer of
`sigmond.wizard_dispatch` (commit `52190e7`).

## Authors

- Michael Hauan (AC0G, GitHub: mijahauan)
- Repo: https://github.com/mijahauan/mag-recorder
- Upstream C binary: Dave Witten (wittend) — see `docs/PROVENANCE.md`
  for the origin story and the patches contributed back.

## Status (as of last commit)

Hardware-pending; the contract surface, simulator, packager, and
upload pipeline all work end-to-end without hardware. See README's
status table for the live punch list. Run with `mag-recorder daemon
--simulate` and `mag-recorder upload --dry-run` until real hardware
("pi eliminator") arrives.

## Quick reference

```bash
# Development — uv canonical
uv sync --extra dev
uv run pytest tests/
uv run pytest tests/test_contract.py -v          # one file
uv run pytest -k packager -v                     # by keyword

# Run-from-source without install:
PYTHONPATH=src python3 -m mag_recorder inventory --json --config <path>

# Production install / upgrade (uses sigmond's shared _ensure_uv helper)
sudo ./scripts/install.sh           # first-run
sudo ./scripts/deploy.sh            # ongoing refresh

# CLI (current — verify against `mag-recorder --help`)
mag-recorder inventory --json       # per-instance resource view
mag-recorder validate --json        # config validation
mag-recorder version --json
mag-recorder daemon [--simulate]    # JSONL spool supervisor
mag-recorder package                # daily JSONL → OBS<date>T00:00.zip
mag-recorder upload [--dry-run]     # SFTP to PSWS
mag-recorder config init|edit       # whiptail wizard via sigmond.wizard_dispatch
```

## Data flow

```
PNI RM3100 magnetometer  +  MCP9808 ambient temp
        │
        │ I²C (0x23 / 0x1F)
        ▼
Pololu 5396/5397 USB ↔ I²C adapter
        │ USB CDC-ACM → /dev/ttyMAG0 (udev rule pins the symlink)
        ▼
mag-usb (Dave Witten's C utility)
        │ 1 Hz JSONL on stdout: {"ts":"DD Mon YYYY HH:MM:SS","rt":...,"x":...,"y":...,"z":...}
        ▼
mag_recorder.core.supervisor       (Python; uses SimulatorSource OR MagUsbSubprocessSource)
        │ - re-stamps each line with ISO-8601 ms UTC
        │ - appends to daily JSONL spool
        │ - sd_notify watchdog
        ▼
/var/lib/mag-recorder/samples-YYYY-MM-DD.jsonl
        │
   (mag-recorder-upload.timer @ 03:00 UTC + jitter)
        ▼
mag_recorder.core.packager
        │ → OBS<date>T00:00.zip
        ▼
mag_recorder.core.uploader
        │ → hs_uploader.transports.PswsMagnetometerSftp
        │ → sftp put .part → rename → mkdir trigger
        ▼
pswsnetwork.eng.ua.edu
```

## Project structure

```
src/mag_recorder/
  cli.py              # argparse entry; subcommands listed above
  config.py           # TOML loader, defaults
  configurator.py     # `config init|edit` — whiptail via sigmond.wizard_dispatch
  contract.py         # inventory/validate JSON builders (contract v0.6)
  version.py          # GIT_INFO dict
  core/
    supervisor.py     # consume JSONL, re-stamp, write daily spool
    simulator.py      # synthetic 1 Hz JSONL source (no hardware needed)
    driver_config.py  # render mag-usb's config.toml from operator's [mag] section
    packager.py       # daily JSONL → OBS<date>T00:00.zip
    uploader.py       # SFTP to PSWS via hs-uploader
tests/                # 7 files; contract / packager / supervisor / simulator
config/               # mag-recorder-config.toml.template
scripts/              # install.sh, deploy.sh
systemd/              # mag-recorder@.service + mag-recorder-upload.timer
deploy.toml           # sigmond client manifest
docs/PROVENANCE.md    # origin story + upstream patches + license analysis
```

## Key design decisions

- **One supervisor, two sources.** `SimulatorSource` and
  `MagUsbSubprocessSource` implement the same iterator interface;
  the supervisor doesn't know which one it has. Lets development
  proceed without hardware.
- **Re-stamp at receive time.** Upstream `mag-usb` emits second-
  resolution timestamps. Supervisor re-stamps with ISO-8601 ms UTC
  because the cadence is wall-clock-aligned at 1 Hz; nothing is lost
  by trusting receive time at sub-second precision.
- **Daily rotation, not size.** Spool rolls at UTC midnight to match
  the PSWS daily-zip upload cadence. One `OBS<date>T00:00.zip` per
  UTC day.
- **mag-usb owns the I²C wire protocol.** This repo does not parse
  RM3100 / MCP9808 register layouts. We render `mag-usb`'s own TOML
  config from the operator's `[mag]` section (`driver_config.py`),
  start `mag-usb -f <rendered.toml>`, and consume its stdout.
- **Operator config in `/etc/mag-recorder/`, driver config in
  `/etc/mag-usb/`.** Per CONTRACT v0.6 §16.7 — clients own their
  operator-facing settings; we render the driver's settings from
  ours, never the reverse.
- **PSWS upload runs via timer, not on every flush.** The
  `mag-recorder-upload.timer` fires at 03:00 UTC with jitter so PSWS
  doesn't see a thundering herd. Installed disabled by default.

## Client contract (v0.6)

mag-recorder declares `CONTRACT_VERSION = "0.6"` in `src/mag_recorder/
contract.py`. It is one version behind the recorders that have moved to
v0.7. The authoritative spec is
`/opt/git/sigmond/sigmond/docs/CLIENT-CONTRACT.md`.

Sections implemented:

- **§1 / §2 / §3 / §5** — native TOML config, instance binding,
  self-describe CLI (`inventory`/`validate`/`version` `--json`),
  `deploy.toml` manifest. No radiod binding (this is a §16
  independent-source client, not an RTP subscriber).
- **§10 / §11** — `log_paths` in inventory; daemon process log
  goes to the systemd journal.
- **§14** — `configurator.py` whiptail wizard via
  `sigmond.wizard_dispatch` (mag-recorder is the lib's *first*
  consumer).
- **§16 (independent data-source clients)** — mag-recorder doesn't
  consume radiod; it consumes a USB device via mag-usb. The contract
  acknowledges this class of client.
- **§16.7** — operator config separation from driver config (see
  Key Design Decisions).
- **§18 (timing authority)** — not yet wired. mag-recorder is a
  candidate non-radiod subscriber per §18's host-monotonic anchor
  variant (`host_monotonic_at_anchor`); when authority subscription
  lands, sample-time labelling becomes UTC-anchored rather than
  host-clock.

## Dependencies

- **Python:** `hs-uploader>=0.1.0` (PSWS SFTP transport;
  resolved as editable sibling from `../hs-uploader` via
  `[tool.uv.sources]`). `tomli` shim for Python <3.11.
- **External binary:** `mag-usb` (Dave Witten / wittend, GPL-3.0).
  Built and installed separately; mag-recorder spawns it via
  subprocess and reads its stdout. PR #1 against upstream adds the
  `-f <config.toml>` flag this repo relies on.
- **Hardware:** PNI RM3100 + MCP9808, Pololu 5396 or 5397 USB↔I²C
  adapter. udev rule pins the device to `/dev/ttyMAG0`.

## Production paths

- Config: `/etc/mag-recorder/mag-recorder-config.toml`
- Driver config: `/etc/mag-usb/config.toml` (rendered by `driver_config.py`)
- Spool: `/var/lib/mag-recorder/samples-YYYY-MM-DD.jsonl`
- Upload staging: `/var/lib/mag-recorder/upload/OBS<date>T00:00.zip`
- Process log: systemd journal (`journalctl -u mag-recorder@<instance>`)
- Venv: `/opt/mag-recorder/venv`
- Source: `/opt/git/sigmond/mag-recorder` (editable install)
- Service user: per `install.sh`

## Further reading

- `README.md` — full status table + data-flow diagram with
  hardware photos / part numbers.
- `docs/PROVENANCE.md` — upstream mag-usb origin, patches
  contributed back, GPL/MIT license analysis.
