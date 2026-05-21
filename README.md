# mag-recorder

A sigmond client that records 3-axis magnetic-field samples from a PNI
RM3100 magnetometer (over a Pololu Isolated USB-to-I²C adapter) and
uploads daily datasets to the HamSCI Personal Space Weather Station
network at `pswsnetwork.eng.ua.edu`.

`mag-recorder` is a thin Python supervisor wrapping
[Dave Witten's upstream `mag-usb`](https://github.com/wittend/mag-usb)
C utility (GPL-3.0).  The C binary handles the I²C wire protocol —
triggering an RM3100 POLL, busy-waiting on DRDY, reading the 9-byte
XYZ register, reading the MCP9808 ambient-temperature register — and
emits one JSON line per UTC second to stdout.  This wrapper owns
everything sigmond cares about: the contract surface
(`inventory --json`, `validate --json`), the JSONL spool, daily
zip packaging, and the SFTP upload pipeline.

See [`docs/PROVENANCE.md`](docs/PROVENANCE.md) for the full origin
story, the patches we contributed back to `wittend/mag-usb`, and the
GPL/MIT license analysis.

## Status

| Item | State |
|---|---|
| Scaffold + contract surface | ✅ shipped, 34 tests pass (0.15 s) |
| Simulator (synthetic JSONL, no hardware needed) | ✅ |
| JSONL spool + UTC-midnight rotation | ✅ |
| Daily zip packager (`OBS<date>T00:00.zip`) | ✅ |
| PSWS SFTP transport (in `hs-uploader`) | ✅ |
| `mag-recorder-upload.timer` (03:00 UTC daily) | ✅ (installed disabled) |
| Sigmond catalog registration | ✅ `smd list` discovers as `available` |
| Upstream `mag-usb` patches | 🟡 [PR #1 open at wittend/mag-usb](https://github.com/wittend/mag-usb/pull/1) |
| Live PSWS upload | 🟡 blocked on real hardware |
| Real RM3100 + Pololu adapter validation | 🟡 hardware ("pi eliminator") not on hand |

Until the hardware arrives, run with `mag-recorder daemon --simulate`
and `mag-recorder upload --dry-run` to exercise the full pipeline
without polluting PSWS.

## Data flow

```
                       ┌─────────────────────────────┐
                       │ PNI RM3100 magnetometer     │
                       │ MCP9808 ambient temperature │
                       └──────────────┬──────────────┘
                                      │ I²C (0x23 / 0x1F)
                       ┌──────────────▼──────────────┐
                       │ Pololu 5396/5397 USB ↔ I²C  │
                       └──────────────┬──────────────┘
                                      │ USB CDC-ACM
                                      │ → /dev/ttyMAG0 (udev)
                       ┌──────────────▼──────────────┐
                       │ mag-usb (Dave Witten's C    │
                       │ utility; 1 Hz JSONL stdout) │
                       └──────────────┬──────────────┘
                                      │ stdin
                       ┌──────────────▼──────────────┐
                       │ mag_recorder.core.supervisor│
                       │ - re-stamps ISO-8601 ms     │
                       │ - daily JSONL spool         │
                       │ - sd_notify watchdog        │
                       └──────────────┬──────────────┘
                                      │ /var/lib/mag-recorder/samples-YYYY-MM-DD.jsonl
                                      │
                  (03:00 UTC + jitter; mag-recorder-upload.timer)
                                      │
                       ┌──────────────▼──────────────┐
                       │ mag_recorder.core.packager  │
                       │ → OBS<date>T00:00.zip       │
                       └──────────────┬──────────────┘
                                      │ /var/lib/mag-recorder/upload/
                       ┌──────────────▼──────────────┐
                       │ mag_recorder.core.uploader  │
                       │ →  hs_uploader.transports.  │
                       │    PswsMagnetometerSftp     │
                       └──────────────┬──────────────┘
                                      │ sftp put .part → rename → mkdir trigger
                       ┌──────────────▼──────────────┐
                       │ pswsnetwork.eng.ua.edu      │
                       │ as S000082 / instrument     │
                       │ RM3100                      │
                       └─────────────────────────────┘
```

## Hardware

- **PNI RM3100** 3-axis magnetometer board (TAPR/TangerineSDR or
  equivalent), I²C address `0x23`.
- **MCP9808** ambient-temperature sensor on the same I²C bus, address
  `0x1F` (an RM3100 carrier-board convention).
- **Pololu Isolated USB-to-I²C Adapter** — either 5396 (no isolated
  power, USB PID `0x2502`) or 5397 (with isolated power, USB PID
  `0x2503`).  Same vendor ID `0x1ffb` on both.
- A Linux host with USB 2.0+ ports.  The Pololu adapter appears as
  `/dev/ttyACMn` natively; the udev rule shipped by `mag-usb`
  symlinks it to `/dev/ttyMAG0` regardless of which ACM number the
  kernel picked.

## Quick start (no hardware)

```bash
git clone https://github.com/mijahauan/mag-recorder
cd mag-recorder
uv venv --python 3.11
uv pip install -e .[dev]

# Run the simulator for a few seconds; writes JSONL to a local spool.
mkdir -p /tmp/mr/{spool,queue,log}
cat > /tmp/mr/config.toml <<EOF
[station]
psws_station_id = "S000082"
instrument_id   = "RM3100"
callsign        = "AC0G"
grid_square     = "EM38ww"

[paths]
spool_dir        = "/tmp/mr/spool"
log_dir          = "/tmp/mr/log"
upload_queue_dir = "/tmp/mr/queue"

[simulator]
enabled = true
EOF

# 3 s of synthetic samples (1 Hz).
timeout 3 mag-recorder daemon --config /tmp/mr/config.toml --simulate

# Bundle the JSONL into the upload zip.
mag-recorder package --config /tmp/mr/config.toml --date "$(date -u +%Y-%m-%d)"

# Pretend to ship it (dry-run: log the sftp batch, do not invoke sftp).
mag-recorder upload --config /tmp/mr/config.toml --dry-run --log-level INFO
```

The dry-run output shows the exact wire format a real upload would
use:

```
[dry-run] PswsMagnetometerSftp would upload .../OBS2026-05-13T00:00.zip
  as S000082@pswsnetwork.eng.ua.edu
  with trigger cOBS2026-05-13T00:00_#RM3100_#2026-05-13T03-05
upload: acked=1 failed=0 remaining=0 [dry-run]
```

## Installation (sigmond-managed)

mag-recorder is registered in the sigmond catalog
(see `deploy.toml`'s `[client]` block).  The canonical installer is
`install.sh`; sigmond invokes it via `smd install mag-recorder`.
Either path works:

```bash
# Via sigmond
sudo smd install mag-recorder
sudo systemctl enable --now mag-recorder.service

# Or directly (idempotent; re-run to upgrade)
sudo /opt/git/sigmond/mag-recorder/install.sh
sudo systemctl enable --now mag-recorder.service
```

`install.sh` handles everything `pip install` can't:

- creates the `magrec` service user with `dialout` membership so the
  daemon can open `/dev/ttyMAG0` (mode 0660 root:dialout)
- builds the upstream `mag-usb` C binary from `/opt/git/sigmond/mag-usb`
  (clone `wittend/mag-usb` sigmond-integration branch there first, or
  point `MAG_USB_REPO=/path` at it) and installs it to `/usr/local/bin/`
- installs `/etc/udev/rules.d/99-PololuI2C.rules` and runs
  `udevadm control --reload-rules && udevadm trigger` so `/dev/ttyMAG0`
  stabilizes across reconnects and USB-port swaps
- creates the Python venv at `/opt/mag-recorder/venv`, installs the
  package in editable mode, and exposes `mag-recorder` on `$PATH`
- renders the config template into `/etc/mag-recorder/` if absent
- symlinks the three systemd units into `/etc/systemd/system/`

For the daily upload, enable the timer once PSWS uploads are wanted:

```bash
sudo systemctl enable --now mag-recorder-upload.timer
sudo systemctl list-timers mag-recorder-upload.timer
```

### Sharing the PSWS SSH key with hf-timestd

PSWS authorizes one SSH key per station; multiple instruments live
under that same key.  On a host already running `hf-timestd` for
Grape uploads, the PSWS key typically lives at
`/home/timestd/.ssh/id_rsa_psws` (mode 0600, owned by `timestd`).
mag-recorder needs `magrec` to read that same file without
duplicating it.  The least-invasive way is filesystem ACLs:

```bash
sudo setfacl -m u:magrec:rx /home/timestd
sudo setfacl -m u:magrec:rx /home/timestd/.ssh
sudo setfacl -m u:magrec:r  /home/timestd/.ssh/id_rsa_psws
sudo setfacl -m u:magrec:r  /home/timestd/.ssh/id_rsa_psws.pub
```

Then point `[uploader].ssh_key_file` in
`/etc/mag-recorder/mag-recorder-config.toml` at
`/home/timestd/.ssh/id_rsa_psws`.  Verify with:

```bash
sudo -u magrec ssh-keygen -lf /home/timestd/.ssh/id_rsa_psws.pub  # should print the fingerprint
sudo -u magrec MAG_RECORDER_VALIDATE_CHIP=1 mag-recorder validate --json
```

On a fresh host without hf-timestd, generate a new key, register it
on the PSWS portal under your station ID, drop the private key in a
`magrec`-readable location, and point `ssh_key_file` at that path.

The mag-recorder daemon will work without `mag-usb` installed if
`[simulator].enabled = true` is set in its config — useful for
bringup against the rest of the sigmond stack before the hardware
lands.

### Driver-config pipeline

mag-recorder owns the operator-facing config in
`/etc/mag-recorder/mag-recorder-config.toml`.  At every daemon start
the supervisor renders a private mag-usb config TOML at
`/run/mag-recorder/mag-usb-driver.toml` (provided fresh by systemd's
`RuntimeDirectory=mag-recorder`) and passes it to `mag-usb` via the
`-f <path>` flag that landed in `wittend/mag-usb` sigmond-integration
PR #2.  The I²C address is also pinned belt-and-braces via `-A
0x<addr>` on the same argv, so an out-of-sync driver TOML can't
silently route the binary to the wrong device.

Net effect: `/etc/mag-usb/anything` does not have to exist.  The
operator edits one config; the C binary always reads a fresh
in-lockstep driver TOML.

### Validating the chip-side state

After install, run `mag-recorder validate --json` to surface
host↔chip mismatches via the chip-readback support added in
`wittend/mag-usb` PR #3:

```bash
MAG_RECORDER_VALIDATE_CHIP=1 mag-recorder validate --json
```

The env-gate keeps the default `validate` fast and offline-safe (no
hardware required for CI / build hosts).  When set, validate invokes
`mag-usb -f <driver_toml> -P` and parses the `Chip register readback`
section for `-- MISMATCH` markers or Address-NACK failures, emitting
a `fail` issue if the chip's CC / NOS / TMRC don't match what
mag-recorder thinks it programmed.

## Configuration

The daemon reads `/etc/mag-recorder/mag-recorder-config.toml` (or
the path in `$MAG_RECORDER_CONFIG`).  Three ways to populate it:

### Interactive wizard (default)

When stdout is a TTY and `whiptail` is installed, `mag-recorder
config init` (first-time) and `mag-recorder config edit` (subsequent)
launch a guided whiptail wizard:

```bash
sudo mag-recorder config init      # first run; renders template, then wizard
sudo mag-recorder config edit      # change settings later; wizard pre-fills
```

The wizard walks PSWS-required fields (station ID, callsign, grid,
instrument ID), validates inline (PSWS regex `S` + 6 digits; Maidenhead
grid; I²C address 1..0x7F; etc.), and offers an optional
advanced-tuning section for chip-side knobs (cycle count, NOS,
sampling mode, TMRC, device path).  Per-field help text lives in
`config/help.toml`; pre-fills come from
`/etc/sigmond/coordination.env` `STATION_*` (read-only — only sigmond
itself writes that file) and the current TOML.

Under the hood, the wizard is a shell script
(`scripts/config-wizard.sh`) that talks to `mag-recorder config show
--json --defaults` and `mag-recorder config apply --json -`.  All
schema knowledge stays in `src/mag_recorder/configurator.py`; the
wizard is a UI shell.

### Headless / scripted

For `apt-get`-style first-run interviews, sigmond apply runs, CI:

```bash
sudo mag-recorder config init --non-interactive
```

Renders the template into `/etc/mag-recorder/` with `STATION_*` env-bag
substitutions ([§14.3](https://github.com/mijahauan/sigmond/blob/main/CONTRACT.md)),
no prompts.  Predates the wizard; still the right thing for scripted
deploys.

### Hand-edit

The template at `config/mag-recorder-config.toml.template` documents
every key with defaults; comments survive a hand-edit but not the
wizard (which serializes a clean TOML).  Pick whichever style suits you.

### Tooling integration

The two JSON entry points the wizard uses are also stable surfaces
for sigmond and other tooling:

```bash
mag-recorder config show --json [--defaults]   # → stdout JSON
mag-recorder config apply --json -             # ← stdin JSON, validated, atomic write
```

Validates types against `DEFAULTS`, runs cross-field invariants via
`driver_config.render()` (cycle_count 1..800, i2c_address 1..0x7F,
sampling_mode POLL|CMM), and writes back via `.part`+rename.
Unknown sections / wrong types / out-of-range values are rejected
with exit code 2 and the existing file is untouched.

Sections:

| Section | Purpose |
|---|---|
| `[station]` | PSWS station ID, instrument ID, callsign, grid, location |
| `[mag]` | Path to the `mag-usb` binary, the device path, sample rate, I²C address |
| `[websocket]` | When `enable = true`, runs `mag-usb` with `-W` so it also broadcasts each JSON sample over a WebSocket server (`bind_address`, `port`) |
| `[paths]` | JSONL spool dir, log dir, upload queue dir |
| `[uploader]` | PSWS host, sftp user (defaults to `station.psws_station_id`), SSH key path, bandwidth cap, daily-run UTC offset |
| `[simulator]` | When `enabled = true`, drives the supervisor from a synthetic source instead of spawning `mag-usb` |

`mag-recorder` also honors a small `STATION_*` env-var bag for
sigmond's CONTRACT §14.3 interview (`STATION_CALLSIGN`,
`STATION_GRID`, etc.) — these populate the TOML defaults when an
operator runs `mag-recorder config init` from a coordinated
sigmond apply.

## CLI

| Command | Purpose |
|---|---|
| `mag-recorder daemon [--simulate]` | Long-running 1 Hz recorder; writes daily JSONL to the spool dir |
| `mag-recorder package [--date YYYY-MM-DD] [--overwrite] [--delete-source]` | Bundle one UTC day's JSONL into `OBS<date>T00:00.zip` (default date: yesterday UTC) |
| `mag-recorder upload [--dry-run] [--max-uploads N]` | Drain the queue via SFTP to PSWS; deletes acked zips |
| `mag-recorder inventory --json` | CONTRACT v0.6 inventory |
| `mag-recorder validate --json` | CONTRACT v0.6 validation; exits non-zero if any `fail` issues |
| `mag-recorder version --json` | Version + git provenance |
| `mag-recorder config init [--reconfig] [--non-interactive]` | Render the config template into `/etc/mag-recorder/...` |
| `mag-recorder config edit [--non-interactive]` | Print the current config and flag unset placeholders |

All commands accept `--config <path>` and `--log-level <level>`.

## Daily upload pipeline

`mag-recorder-upload.timer` fires `mag-recorder-upload.service` at
**03:00 UTC** daily (plus 0-15 min randomized jitter so a fleet
doesn't all hit PSWS at exactly the same instant).  The service is
`Type=oneshot` with two `ExecStart=` directives:

1. `mag-recorder package` — bundle yesterday's
   `samples-<date>.jsonl` into `OBS<date>T00:00.zip` in the upload
   queue dir.  Empty days exit 0 with a warning so a fresh-deploy
   morning doesn't fail the unit.
2. `mag-recorder upload` — drain the queue.  All-acked exits 0;
   one-or-more failed exits 1 so systemd surfaces the failure (or
   an `OnFailure=` rule fires).

`Persistent=true` runs a missed firing on next boot rather than
waiting another 24 h.

The unit files are installed by `smd install mag-recorder` but
deliberately **not** auto-enabled by sigmond — sigmond's catalog
omits them from `[systemd].units` so the operator turns the timer
on manually once the hardware is wired up and PSWS uploads are
desired:

```bash
sudo systemctl enable --now mag-recorder-upload.timer
sudo systemctl list-timers mag-recorder-upload.timer
```

Until then the spool grows untouched (or runs in the simulator
path) and zero SFTP traffic leaves the host.

### PSWS upload format

Every daily zip is shipped through `hs-uploader`'s
`PswsMagnetometerSftp` transport.  The wire flow matches the
hf-timestd Grape pipeline byte-for-byte:

```
sftp -b - -i /etc/hs-uploader/keys/id_ed25519 S000082@pswsnetwork.eng.ua.edu << END
put "/var/lib/mag-recorder/upload/OBS2026-05-12T00:00.zip" "OBS2026-05-12T00:00.zip.part"
rename "OBS2026-05-12T00:00.zip.part" "OBS2026-05-12T00:00.zip"
mkdir "cOBS2026-05-12T00:00_#RM3100_#2026-05-13T03-05"
quit
END
```

- **Dataset zip name** uses colons (`OBS<date>T00:00.zip`) per the
  PSWS magnetometer spec.
- **Trigger directory name** uses dashes in its timestamp portion
  (`2026-05-13T03-05` not `2026-05-13T03:05`) because PSWS treats
  that name as a filesystem entry and some processing tools dislike
  colons in directory names.  Matches the Grape upload convention.
- The `.part`-then-rename sequence keeps the server from picking up
  a half-uploaded zip.
- **PSWS station ID is per measurement, not per host.**  PSWS
  treats each measurement (Grape recordings, RM3100 magnetometer,
  etc.) as its own station with its own ID, even when they share
  one operator and one physical site.  Register a separate PSWS
  station for the magnetometer; do not reuse hf-timestd's Grape
  station ID.
- The **SSH key**, however, IS shared.  One operator key authorizes
  uploads to any of that operator's PSWS stations — so on a host
  already running hf-timestd, point `[uploader].ssh_key_file` at
  hf-timestd's Grape key (typically `/home/timestd/.ssh/id_rsa_psws`,
  shared via filesystem ACLs as documented above).

| Field | Value |
|---|---|
| Station ID (Grape, hf-timestd) | `S0xxxxx` — separate registration |
| Station ID (magnetometer, this client) | `S0yyyyy` — **distinct registration** |
| Instrument ID | `RM3100` |
| Upload host | `pswsnetwork.eng.ua.edu` |
| SSH key | shared across the operator's PSWS stations (one registration) |
| Daily artifact | `OBS<YYYY-MM-DD>T00:00.zip` |
| Trigger directory | `c<dataset_name>_#<instrument_id>_#<timestamp>` |

## Sigmond integration

mag-recorder is a **CONTRACT v0.6 §16 non-radiod data-source client**
(`data_path.kind = "other"`).  It does *not* depend on `ka9q-radio`
or `ka9q-python`; the magnetometer pipeline is independent of the
radiod data path.

The `[client]` block in `deploy.toml` registers it with sigmond's
catalog discovery (`/opt/git/sigmond/sigmond/lib/sigmond/discover.py`
globs every `/opt/git/sigmond/*/deploy.toml`):

```
$ smd list
NAME                  LIFECYCLE         VERDICT       INDEX  BEHIND  HEAD DATE
mag-recorder          available         up to date    4      0       2026-05-13
…
```

- `inventory --json` reports `data_path = {kind: "other", details: {device: "/dev/ttyMAG0", ...}}` and `data_sinks` for the JSONL spool + log dir.
- `validate --json` flags missing PSWS station ID, missing SSH key, missing `mag-usb` binary (unless `[simulator].enabled = true`), and missing/`<…>`-placeholder station fields.
- No `[radiod]` block; the `radiod_id`, `data_destination`, and `chain_delay_ns_applied` inventory fields are deliberately omitted per CONTRACT §16.5.

## File layout

| Path | Purpose |
|---|---|
| `src/mag_recorder/cli.py` | argparse entry point |
| `src/mag_recorder/contract.py` | CONTRACT v0.6 inventory + validate JSON builders |
| `src/mag_recorder/config.py` | TOML loader, defaults, env-var fallbacks |
| `src/mag_recorder/configurator.py` | §14 `config init/edit` interview |
| `src/mag_recorder/version.py` | Git provenance embedded in `inventory --json` |
| `src/mag_recorder/core/simulator.py` | Synthetic RM3100+MCP9808 JSONL stream |
| `src/mag_recorder/core/supervisor.py` | JSONL source → ISO-8601-ms re-stamp → daily spool |
| `src/mag_recorder/core/packager.py` | Daily JSONL → `OBS<date>T00:00.zip` (atomic via `.part`+rename) |
| `src/mag_recorder/core/uploader.py` | Queue drain via `hs_uploader.transports.PswsMagnetometerSftp` |
| `systemd/mag-recorder.service` | Continuous daemon (Type=notify, WatchdogSec=30) |
| `systemd/mag-recorder-upload.service` | Daily package+upload oneshot |
| `systemd/mag-recorder-upload.timer` | 03:00 UTC schedule |
| `config/mag-recorder-config.toml.template` | Config schema; rendered by `mag-recorder config init` |
| `deploy.toml` | sigmond install manifest + `[client]` catalog block |
| `docs/PROVENANCE.md` | Origin story, upstream patches, license analysis |

## JSONL line format

Each spool line is a single JSON object with millisecond-precision
ISO-8601 UTC timestamps:

```json
{"ts":"2026-05-12T23:45:01.000Z","rt":23.125,"x":12345.678,"y":-234.500,"z":987.001}
```

| Field | Unit | Source |
|---|---|---|
| `ts` | ISO-8601 UTC, ms precision | re-stamped by the supervisor at receive time |
| `rt` | °C, MCP9808 ambient | passthrough from `mag-usb` `rt` |
| `x`, `y`, `z` | nanoTesla, after the `[mag_orientation]` rotations | passthrough from `mag-usb` |

The supervisor re-stamps because upstream `mag-usb` emits
second-resolution timestamps in `"DD Mon YYYY HH:MM:SS"` format
(no timezone, no fractional second).  Our re-stamp is wall-clock
accurate to within a few ms — sufficient for 1 Hz geomagnetic
data, and a strict upgrade over the stringified upstream
timestamp.

## Development

```bash
uv venv --python 3.11
uv pip install -e .[dev]
.venv/bin/pytest -q     # 34 tests in ~0.15 s
```

Tests cover the contract surface (inventory shape, validate
issues, JSON round-trip), the simulator (determinism, line shape,
timestamps), the supervisor (UTC-midnight spool rotation,
end-to-end source → spool), the packager (atomic .part-rename,
overwrite refusal, source preservation), and the uploader (queue
ordering, dry-run, deletion on ack, stop-on-failure semantics).

PSWS transport tests live in `hs-uploader/tests/test_transport_psws_magnetometer.py`
(16 tests, subprocess.run mocked).

## License

mag-recorder itself is **MIT-licensed** (see [`LICENSE`](LICENSE)).
It is a separate process from `mag-usb` (GPL-3.0) and communicates
via subprocess pipes; the licensing relationship is analyzed in
[`docs/PROVENANCE.md`](docs/PROVENANCE.md).

## Acknowledgments

- **Dave Witten, KD0EAG** — author of upstream
  [`mag-usb`](https://github.com/wittend/mag-usb), the C utility
  that actually talks to the magnetometer.  Without his work this
  client would be reimplementing the I²C-over-Pololu protocol from
  scratch.
- **The HamSCI / TangerineSDR / Grape project** — defined the
  RM3100 carrier board, the PSWS upload server, and the dataset
  conventions (`OBS<date>T<HH:MM>` naming, trigger directory
  format) that this client mirrors.

## Related projects

| Repo | Role |
|---|---|
| [`wittend/mag-usb`](https://github.com/wittend/mag-usb) | upstream C utility; the actual sensor driver |
| [`mijahauan/mag-usb`](https://github.com/mijahauan/mag-usb) | our fork on the `sigmond-integration` branch; source of [PR #1](https://github.com/wittend/mag-usb/pull/1) |
| [`mijahauan/sigmond`](https://github.com/mijahauan/sigmond) | the sigmond orchestrator; `smd list` / `smd install` / etc. |
| [`mijahauan/hs-uploader`](https://github.com/mijahauan/hs-uploader) | shared upload library; hosts the `PswsMagnetometerSftp` transport |
| [`mijahauan/hf-timestd`](https://github.com/mijahauan/hf-timestd) | Grape WWV recorder; sibling PSWS uploader (same SSH key + server) |
