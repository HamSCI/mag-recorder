# mag-recorder — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** mag-recorder `0.1.0` / deploy `0.1.0` / contract `0.8` (2026-06-25).
**Prefix:** `MAG`.

> Application of [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md),
> chosen to exercise the template on the suite's **one non-radiod client**: it
> reads a PNI RM3100 magnetometer **directly over USB-I²C** (Pololu adapter),
> not from `radiod`. That distinction — `data_path.kind = "other"`, not
> `radiod-ka9q-python`, with a `hardware_present` gate per contract §3 — is the
> spine of this doc and recurs as a constraint throughout. Maturity is
> **Active** but **real-hardware validation is pending**: the contract surface,
> simulator, daily packager, and PSWS upload pipeline all work end-to-end
> *without* a sensor, so expect a band of `🟡` on the hardware path while the
> software path is `✅`. The sigmond↔component **interface** requirements are
> specified once in the [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
> (v0.8) and referenced — not restated — here (§8.3). Provenance tags:
> `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]` surfaced by this review.
> Status: ✅ implemented · 🟡 partial/unverified · ⬜ planned.

## 1. Context & problem statement

A HamSCI Personal Space Weather Station (PSWS) wants a local record of the
**3-axis geomagnetic field** — the slow variations that betray geomagnetic
storms, substorms, and Sq current systems — co-located with its HF instruments
so magnetometer data and ionospheric data share a station identity and a
timeline. mag-recorder is that recorder: it samples a PNI **RM3100** 3-axis
magnetometer (plus an MCP9808 ambient-temperature sensor) at 1 Hz, spools the
samples as daily JSONL, and uploads daily datasets to the PSWS network at
`pswsnetwork.eng.ua.edu` in the Grape-style zip convention the HF instruments
already use.

It is the suite's **only non-radiod client**. Every other client subscribes to
`radiod` RTP IQ; mag-recorder instead reads a USB device. The sensor is reached
through a **Pololu Isolated USB-to-I²C adapter** (5396/5397) presenting a
USB CDC-ACM device (udev-pinned to `/dev/ttyMAG0`), and the I²C wire protocol
is owned by Dave Witten's upstream `mag-usb` C utility (GPL-3.0), which
mag-recorder spawns as a subprocess and whose 1 Hz JSONL stdout it consumes.
mag-recorder itself is a thin Python **supervisor** that owns only what sigmond
and PSWS care about: the contract self-describe surface, the JSONL spool with
UTC-midnight rotation, the daily zip packager, and the SFTP upload.

This split — C binary owns the wire, Python owns the contract — and the
"two interchangeable sources behind one supervisor" design (a `SimulatorSource`
and a `MagUsbSubprocessSource` implementing the same iterator) are what let the
whole pipeline be developed, tested (34 tests, ~0.15 s), and demonstrated with
**no hardware on hand**. Real RM3100 + Pololu validation is the headline open
item: until the adapter arrives, the client runs `daemon --simulate` and
`upload --dry-run`, and `hardware_present` lets sigmond mark it core-but-dormant.

## 2. Goals & objectives

- Record RM3100 X/Y/Z field (nT) + MCP9808 ambient temperature (°C) at 1 Hz to
  a durable daily JSONL spool, re-stamped to ISO-8601 ms UTC.
- Package each UTC day into a PSWS Grape-style `OBS<date>T00:00.zip` and upload
  it by SFTP to `pswsnetwork.eng.ua.edu`, on a jittered daily timer.
- Be a **well-behaved non-radiod suite client**: self-describe via
  `inventory`/`validate --json` with `data_path.kind="other"` and an honest
  `hardware_present` gate, so sigmond integrates it without special-casing.
- Run the **entire pipeline without hardware** (simulator source + dry-run
  upload), so development and CI never block on the sensor.
- Keep operator-facing config (`/etc/mag-recorder/`) cleanly separated from the
  rendered driver config the `mag-usb` binary consumes (contract §16.7).
- Degrade legibly: no sensor → dormant, not crashed; no sink/network → spool
  intact, upload retried.

## 3. Non-goals / out of scope

- **Owning the I²C wire protocol.** RM3100/MCP9808 register layout, POLL/DRDY
  timing, cycle-count math live in upstream `mag-usb` (GPL-3.0), spawned as a
  subprocess. (Owner: wittend/mag-usb; HamSCI fork.)
- **Being a radiod client.** It does not tune, subscribe to, or require
  `radiod`/`ka9q-python`; `requires=[]`, `uses=[]`. The radiod data path is
  explicitly **not** this client's path.
- **Producing a timing authority** *or consuming one yet.* It samples on the
  host monotonic clock at 1 Hz; `provides_timing_calibration=false`,
  `uses_timing_calibration=false`. §18 subscription (host-monotonic-anchor
  variant) is a future option, not a v0.1 obligation. (Owner: hf-timestd.)
- **Sub-second / high-rate magnetometry.** Cadence is 1 Hz; faster rates need
  upstream `mag-usb` cadence work and are out of scope.
- **Parsing/scientific reduction of the field data.** mag-recorder is a
  recorder + uploader; geomagnetic analysis is PSWS/downstream scope.
- **Multi-instance operation (now).** The unit is a singleton
  `mag-recorder.service`; per-instance plumbing exists but is dormant (§12).

## 4. Stakeholders & actors

Station operator · the **PNI RM3100** magnetometer + **MCP9808** temp sensor
(I²C 0x23 / 0x1F) · the **Pololu 5396/5397 USB↔I²C adapter** (CDC-ACM,
`/dev/ttyMAG0`, udev-pinned) · the upstream **`mag-usb`** C binary (subprocess,
GPL-3.0; HamSCI fork) · `hs-uploader` (`PswsMagnetometerSftp` transport,
editable sibling) · the **PSWS network** (`pswsnetwork.eng.ua.edu`, SFTP target) ·
sigmond (lifecycle, identity, status, `hardware_present` gating, config wizard
via `sigmond.wizard_dispatch` — mag-recorder is its first consumer) · an
optional local WebSocket consumer (loopback live feed).

## 5. Assumptions & constraints

- `MAG-C-001` `[DOC]` ✅ The client SHALL be **non-radiod**: it declares
  `requires=[]`, `uses=[]`, no `[radiod]` block, and `data_path.kind="other"`
  (NOT `radiod-ka9q-python`); it neither tunes nor subscribes to `radiod`.
- `MAG-C-002` `[CODE]` ✅ The I²C wire protocol SHALL be delegated to the upstream
  `mag-usb` C binary (subprocess, stdout JSONL); mag-recorder SHALL NOT parse
  RM3100/MCP9808 registers itself.
- `MAG-C-003` `[DOC]` 🟡 Real-hardware operation SHALL require a Pololu USB-I²C
  adapter presenting `/dev/ttyMAG0` (udev-pinned) and `mag-usb` on `PATH`;
  absent hardware, the simulator source SHALL stand in. *(hardware validation
  pending.)*
- `MAG-C-004` `[CODE]` ✅ The service SHALL run as a dedicated user (`magrec`) in
  the `dialout` group (CDC-ACM `/dev/ttyMAG0` is `root:dialout 0660`).
- `MAG-C-005` `[CODE]` ✅ Operator config (`/etc/mag-recorder/`) SHALL be separate
  from the rendered driver config the binary consumes; the driver TOML is
  derived from `[mag]`, never edited directly (contract §16.7).
- `MAG-C-006` `[CODE]` ✅ Python ≥3.10; runtime deps limited to `hs-uploader`
  (editable sibling) + a `tomli` shim on <3.11. Sibling installs are editable
  so a `git pull` propagates without reinstall.
- `MAG-C-007` `[DOC]` ✅ The client SHALL be a **singleton per host**
  (`mag-recorder.service`, not templated); per-instance code is dormant until a
  Phase-8 cutover (§12).
- `MAG-C-008` `[CODE]` ✅ Sample cadence SHALL be 1 Hz, wall-clock-aligned;
  higher rates are not supported without upstream `mag-usb` changes.

## 6. Functional requirements

### 6.1 Acquisition (two sources, one supervisor)
- `MAG-F-001` `[DOC]` ✅ SHALL acquire 1 Hz samples from **either** a live
  `MagUsbSubprocessSource` (spawns `mag-usb -O <device> -f <driver.toml>
  -A <addr>`, parses stdout JSON) **or** a `SimulatorSource`, behind one
  iterator interface the supervisor is agnostic to.
- `MAG-F-002` `[CODE]` ✅ SHALL render the `mag-usb` driver TOML from the
  operator's `[mag]` section atomically (`.part`+rename) to
  `/run/mag-recorder/mag-usb-driver.toml` on each daemon start, validating
  `cycle_count` ∈ 1..800, `i2c_address` ∈ 0x01..0x7F, `sampling_mode` ∈
  {POLL,CMM}.
- `MAG-F-003` `[DOC]` 🟡 The synthetic source SHALL emit physically plausible
  X/Y/Z (defaults 21500/1500/47500 nT) + temperature with Gaussian noise at
  1 Hz, usable with no hardware (`daemon --simulate` or `[simulator].enabled`).
  *(stands in for hardware until validated — `MAG-C-003`.)*
- `MAG-F-004` `[CODE]` 🟡 On a `mag-usb` subprocess error the supervisor SHALL
  end so systemd (`Restart=always`) respawns it; non-JSON stdout diagnostics
  SHALL be filtered, not spooled. *(unverified against real binary failures.)*

### 6.2 Spooling
- `MAG-F-010` `[DOC]` ✅ SHALL re-stamp each sample's timestamp to ISO-8601 ms
  UTC (`…T…Z`) at receive time, replacing `mag-usb`'s second-resolution
  `"DD Mon YYYY HH:MM:SS"`.
- `MAG-F-011` `[DOC]` ✅ SHALL append samples as JSONL to
  `/var/lib/mag-recorder/samples-YYYY-MM-DD.jsonl`, one file per UTC day,
  **rotated at UTC midnight** to match the PSWS daily-zip cadence.
- `MAG-F-012` `[CODE]` ✅ Each spooled line SHALL carry `ts`, `rt` (°C), and
  `x`/`y`/`z` (nT); a `reporter_id` field SHALL be added only when a
  per-instance config supplies one (dormant — §12).

### 6.3 Daily packaging
- `MAG-F-020` `[DOC]` ✅ SHALL package a day's JSONL into
  `OBS<YYYY-MM-DD>T00:00.zip` (PSWS naming, literal colons) written atomically
  (`.part`+rename) into the upload queue dir, returning `None` (no-op) when no
  source JSONL exists and refusing to clobber an existing zip unless
  `--overwrite`.
- `MAG-F-021` `[CODE]` ✅ `package` SHALL count source lines for the audit log and
  SHALL leave the source JSONL in place unless `--delete-source` is given.

### 6.4 PSWS upload
- `MAG-F-030` `[DOC]` 🟡 SHALL drain the upload queue oldest-first via
  `hs_uploader.transports.PswsMagnetometerSftp` to `pswsnetwork.eng.ua.edu`
  (`put .part` → `rename` → `mkdir` trigger dir
  `cOBS<date>_#<instrument_id>_#<ts>`), deleting a zip only on ack. *(end-to-end
  in dry-run; live PSWS upload blocked on real data/credentials — `MAG-F-091`.)*
- `MAG-F-031` `[CODE]` ✅ Identity (call, grid, `psws_station_id`, SSH key) SHALL
  be sourced from config mirroring hf-timestd; `uploader.user` SHALL default to
  `psws_station_id` when unset.
- `MAG-F-032` `[DOC]` ✅ Upload SHALL run from a **daily timer**
  (`mag-recorder-upload.timer`, 03:00 UTC + ≤15 min jitter, `Persistent=true`),
  **installed disabled**; the operator enables it once live uploads are wanted.
- `MAG-F-033` `[CODE]` ✅ On a retry/permanent transport result the drain SHALL
  stop and leave remaining zips queued for the next run; `--dry-run` SHALL ship
  nothing and delete nothing.

### 6.5 Live feed (optional)
- `MAG-F-040` `[DOC]` 🟡 With `[websocket].enable`, the daemon SHALL broadcast
  each JSON sample over a WebSocket (default `127.0.0.1:8765`, loopback-only),
  independent of the spool/upload and inert under the simulator. *(unverified
  on hardware.)*

### 6.6 Self-description & config (contract surface)
- `MAG-F-050` `[CODE]` ✅ SHALL implement `inventory --json` / `validate --json` /
  `version --json` per contract v0.8 (see §8.3) with pure-JSON stdout, exit 0
  even configless.
- `MAG-F-051` `[CODE]` ✅ `inventory` SHALL report `hardware_present` (true when
  `/dev/ttyMAG0` exists OR simulator on) so sigmond can mark the client
  core-but-dormant from its own self-describe (contract §3 / Phase D).
- `MAG-F-052` `[CODE]` ✅ `validate` SHALL **fail** on empty/placeholder
  `psws_station_id`, empty `instrument_id`, a missing `mag-usb` binary (unless
  simulator on), and an empty `uploader.user` with no station-ID fallback; and
  **warn** on missing callsign/grid, absent adapter device (unless simulator),
  and a missing SSH key.
- `MAG-F-053` `[DOC]` ✅ SHALL provide `config init|edit|show|apply` (whiptail
  wizard via `sigmond.wizard_dispatch`, stdin fallback) per contract §14,
  with env-var overrides (`STATION_*`, `MAG_RECORDER_*`) honored.

## 7. Quality / non-functional requirements

- `MAG-Q-001` `[CODE]` ✅ The daemon SHALL be `Type=notify` with `WatchdogSec=30`
  and `Restart=always` (RestartSec=5); the supervisor SHALL ping the watchdog
  on each consumed sample so a stalled source restarts the unit.
- `MAG-Q-002` `[CODE]` ✅ Spool, packaged zip, and rendered driver TOML SHALL all
  be written atomically (append for JSONL; `.part`+rename for zip/TOML) so a
  reader/uploader never sees a partial artifact.
- `MAG-Q-003` `[CODE]` ✅ Absent hardware the client SHALL run cleanly (simulator
  + dry-run); `hardware_present=false` SHALL NOT be an error state.
- `MAG-Q-004` `[CODE]` ✅ Upload SHALL degrade gracefully: network/transport
  failure leaves the queue intact and is retried next timer; the spool is
  unaffected. The exit code SHALL be non-zero only when an upload actually
  failed.
- `MAG-Q-005` `[CODE]` ✅ The unit SHALL be sandboxed: `ProtectSystem=strict`,
  `ProtectHome=read-only`, `NoNewPrivileges=true`, `MemoryMax=256M`,
  `MemorySwapMax=0`, with `ReadWritePaths` limited to spool+log and config
  read-only.
- `MAG-Q-006` `[CODE]` ✅ `inventory`/`validate` SHALL tolerate an unreadable SSH
  key path (PermissionError → "probably present") rather than crash, so a
  cross-user key under another service home doesn't break self-describe.
- `MAG-Q-007` `[CODE]` ✅ Spool retention SHALL be ~7 days, log retention ~365
  days, as self-declared `data_sinks` (operator-managed; no built-in eviction
  beyond the suite trim janitor).
- `MAG-Q-008` `[DOC]` ✅ Driver-config separation SHALL be preserved: operator
  edits `[mag]`; the binary's TOML is rendered, never hand-edited (§16.7).

## 8. External interfaces

### 8.1 Inputs
- **Sensor:** PNI RM3100 (I²C 0x23) + MCP9808 (0x1F) via a Pololu 5396/5397
  USB↔I²C adapter → CDC-ACM `/dev/ttyMAG0` (udev-pinned) → `mag-usb` subprocess
  (1 Hz JSONL on stdout). **Not** a radiod/RTP input.
- `/etc/mag-recorder/mag-recorder-config.toml` (template at
  `config/mag-recorder-config.toml.template`). Operator **MUST** set
  `[station].psws_station_id` (S+6 digits) and a non-empty `instrument_id`
  (default `RM3100`); **SHOULD** set `callsign`, `grid_square`. Optional:
  lat/lon/elev; `[mag]` tuning (`device`, `i2c_address`, `cycle_count` 1..800,
  `nos`, `tmrc_rate`, `drdy_delay_ms`, `sampling_mode`, `remote_temp_address`,
  `orientation`); `[uploader]` (`host`, `user`, `ssh_key_file`,
  `bandwidth_limit_kbps`, `daily_run_at_utc`); `[websocket]`; `[simulator]`.
- Env overrides (§14.3): `STATION_CALLSIGN/GRID/LATITUDE/LONGITUDE/ELEVATION_M`,
  `MAG_RECORDER_STATION_ID`, `MAG_RECORDER_DEVICE`, `MAG_RECORDER_SIMULATE`,
  `MAG_RECORDER_VALIDATE_CHIP`. Identity also from `/etc/sigmond/coordination.env`.

### 8.2 Outputs
- **Spool:** `/var/lib/mag-recorder/samples-YYYY-MM-DD.jsonl` (daily;
  `{ts,rt,x,y,z}`; ~8.6 MB/day uncompressed).
- **Upload queue:** `/var/lib/mag-recorder/upload/OBS<date>T00:00.zip`.
- **PSWS:** Grape-style zip via SFTP to `pswsnetwork.eng.ua.edu`.
- **Logs:** `/var/log/mag-recorder/{mag-recorder.log, upload.log}` + journal.
- **Live feed (optional):** WebSocket `127.0.0.1:8765`.
- **Self-describe:** `inventory --json` instance `default` with
  `data_path.kind="other"` (details: device, sensor `PNI RM3100`, transport
  `cdc_acm -> i2c-pololu`, `sample_hz=1`), two file `data_sinks`
  (spool 7 d/10 MB-day, log 365 d/1 MB-day), `hardware_present`,
  `uses_timing_calibration=false`, `provides_timing_calibration=false`,
  `timing_authority_applied=null`.

### 8.3 Contracts / APIs (reference, not restated)
- `MAG-I-001` `[CODE]` ✅ Conforms to **client contract v0.8** as a **non-radiod
  independent-source client (§16.5)**: `deploy.toml` declares
  `contract="0.8"`, `requires=[]`, `uses=[]`, `units=["mag-recorder.service"]`
  (singleton, not templated), `start_priority=200`, `config init|edit` hooks
  (§14). `inventory` declares `data_path.kind="other"` (the load-bearing
  non-radiod distinction), `data_sinks=[file:spool, file:log]`, and
  `hardware_present` (§3 Phase-D self-describe). Full field semantics:
  [CLIENT-CONTRACT.md](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
  §3/§14/§16.
- `MAG-I-002` `[CODE]` ✅ Is **not** a §18 timing party:
  `provides_timing_calibration=false`, `uses_timing_calibration=false`,
  `timing_authority_applied=null` (the v0.7 field carried as null to signal
  "contract-aware, no timing dimension"). Future host-monotonic-anchor
  subscription is optional (§12), not a v0.1 obligation.
- `MAG-I-003` `[DOC]` ✅ The sigmond↔PSWS upload seam (Grape-style zip, SFTP,
  `psws_station_id` + portal key, shared SSH key) is governed by
  [PSWS-INTERFACE-BOUNDARY.md](https://github.com/HamSCI/sigmond/blob/main/docs/PSWS-INTERFACE-BOUNDARY.md);
  the magnetometer transport is `hs_uploader.transports.PswsMagnetometerSftp`.

## 9. Data requirements

JSONL record (canonical):
`{"ts":"2026-05-12T23:45:01.000Z","rt":23.125,"x":12345.678,"y":-234.500,"z":987.001}`
— `ts` ISO-8601 ms UTC (re-stamped at receive); `rt` MCP9808 ambient °C; `x/y/z`
RM3100 field in nT (after `[mag_orientation]` 90°-increment rotations). One file
per UTC day. Volume ≈ 8.6 MB/day uncompressed (1 Hz × ~100 B/line × 86 400 s).
Retention: spool 7 days, logs 365 days (self-declared `data_sinks`,
operator-managed). Upload dataset: `OBS<date>T00:00.zip` containing the day's
JSONL; PSWS trigger directory `cOBS<date>_#<instrument_id>_#<ts>` (colons→dashes
for filesystem safety). Provenance/identity: `psws_station_id`, `instrument_id`
(default `RM3100`), callsign, grid; optional per-record `reporter_id` (dormant).

## 10. Dependencies & development sequence

**Runtime deps:** `mag-usb` C binary (wittend, GPL-3.0; HamSCI fork; spawned via
subprocess — the `-f <config.toml>` flag is added by upstream PR #1),
`hs-uploader` (editable sibling, `PswsMagnetometerSftp`), `tomli` (<3.11 shim).
apt: `openssh-client` (SFTP), `whiptail` (optional wizard UI). Hardware: PNI
RM3100 + MCP9808, Pololu 5396/5397 USB↔I²C adapter, udev rule → `/dev/ttyMAG0`.
No radiod/ka9q-python.

**Development sequence (intended, recovered as requirement):**
1. **Scaffold + contract surface** (`inventory`/`validate`/`version`, deploy.toml,
   catalog registration) — ✅ shipped, 34 tests.
2. **Simulator source** (synthetic 1 Hz JSONL) — ✅, so the rest builds
   hardware-free.
3. **Supervisor + JSONL spool** (re-stamp, UTC-midnight rotation, watchdog) — ✅.
4. **Daily packager** (`OBS<date>T00:00.zip`) — ✅.
5. **PSWS SFTP upload + timer** (installed disabled) — ✅ in dry-run.
6. **Driver-config rendering** + `mag-usb` subprocess source — ✅ (binary wiring
   pending PR #1 merge / real binary).
7. **Real-hardware validation** (RM3100 + Pololu) — 🟡 **pending hardware**;
   then live PSWS upload.
8. **(Future) Phase-8 multi-instance cutover** — dormant code in place.

## 11. Acceptance criteria & verification

- Contract conformance → `mag-recorder validate --json` (exit 0, no `fail`) +
  surfaced via `smd status`; `inventory --json` shows `data_path.kind="other"`
  and correct `hardware_present`.
- Hardware-free pipeline → `daemon --simulate` produces a rotating daily JSONL;
  `package` yields a well-formed `OBS<date>T00:00.zip`; `upload --dry-run`
  reports ship-without-transfer. (34-test suite, ~0.15 s.)
- Spool/packager integrity → atomic-write tests; rotation at UTC midnight;
  ISO-8601 re-stamp.
- Upload robustness → queue intact on simulated transport failure; non-zero exit
  only on real failure.
- Liveness → `Type=notify` + `WatchdogSec=30`; a stalled source restarts the unit.
- **Hardware acceptance (pending)** → with a real RM3100/Pololu attached:
  `hardware_present=true`, plausible nT/°C samples, and a live PSWS-acked upload.

## 12. Risks & open questions

- `MAG-F-090` `[NEW]` 🟡 **Contract-version doc/code drift:** `contract.py`'s
  module docstring and `CLAUDE.md` still say "CONTRACT v0.6", but
  `CONTRACT_VERSION = "0.8"` and `deploy.toml` declare **0.8**. The README's
  status table likewise lags. The docs SHALL be reconciled to 0.8 (or the
  intended version pinned and stated once). *(Surfaced by this review.)*
- `MAG-F-091` `[NEW]` 🟡 **Live PSWS upload unproven:** the transport works in
  dry-run, but no real magnetometer zip has been SFTP-acked by
  `pswsnetwork.eng.ua.edu`. SHALL be validated end-to-end once hardware +
  station credentials are in place (gated on `MAG-C-003`).
- `MAG-F-092` `[NEW]` 🟡 **Real RM3100/Pololu integration untested:** the
  `MagUsbSubprocessSource` path (driver-TOML render, `-f` flag, stdout parse,
  failure handling) has never run against the real binary on hardware; relies on
  upstream PR #1. SHALL be validated; until then `--simulate` is the only proven
  source.
- `MAG-F-093` `[NEW]` ⬜ **Driver-config path inconsistency:** `CLAUDE.md` cites
  `/etc/mag-usb/config.toml` while the daemon renders to
  `/run/mag-recorder/mag-usb-driver.toml` (config default
  `driver_config_path`). The canonical location SHALL be stated once and the
  docs aligned.
- `MAG-F-094` `[NEW]` ⬜ **§18 timing subscription deferred:** mag-recorder is a
  candidate non-radiod subscriber (host-monotonic-anchor variant) but stamps
  sample time off the host clock with `timing_authority_applied=null`. If
  PSWS science later needs UTC-anchored magnetometer time, this SHALL be wired.
- `MAG-F-095` `[NEW]` ⬜ **Multi-instance dormant:** singleton today
  (`mag-recorder.service`); per-instance plumbing exists but the unit isn't
  templated. SHALL be either activated (Phase-8 cutover) or documented as a
  deliberate singleton.
- **Upstream dependency risk:** the `-f <config.toml>` flag mag-recorder relies
  on lives in an **open** upstream PR (#1 at wittend/mag-usb); the HamSCI fork
  carries it meanwhile. Track the merge.

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| MAG-I-001 (non-radiod §16.5, `data_path.kind="other"`) | Clients: mag-recorder | `inventory`/`validate --json` | #6:31 (sensor integ.) |
| MAG-F-051 (`hardware_present` §3 gate) | Clients: mag-recorder | inventory on hw-absent host | #6 (Phase-D self-describe) |
| MAG-F-030 (PSWS upload) | — | dry-run + live SFTP ack | #6:40 (→PSWS) |
| MAG-F-091 (live PSWS upload) | *(new — file)* | live PSWS ack | #6:40 |
| MAG-F-092 (real RM3100/Pololu) | *(new — file)* | hardware integration test | #6:31 |
| MAG-F-090 (contract-version drift) | *(new — file)* | doc review vs `CONTRACT_VERSION` | — |
| MAG-F-094 (§18 subscription) | *(new — file)* | UTC-anchored sample test | #6:50 (timing) |

*New rows (MAG-F-090…095) are this review's surfaced gaps; promote the
hardware-validation items to the #18 mag-recorder epic.*
