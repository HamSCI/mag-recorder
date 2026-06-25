# Provenance

This document records where `mag-recorder` came from, what it is
relative to Dave Witten's upstream `mag-usb`, and the licensing
relationship between the two codebases.

## Origin

`mag-recorder` was created in May 2026 to integrate the HamSCI
[TangerineSDR / Grape](https://hamsci.org/grape) magnetometer
(a PNI RM3100 board on the Pololu USB-to-I²C adapter) into the
sigmond client framework on a Beelink Mini-PC at AC0G's station.
Sigmond is the orchestrator that manages the rest of the HF receive
chain on that host (`ka9q-radio`, `wspr-recorder`, `psk-recorder`,
`hfdl-recorder`, `codar-sounder`, `hf-timestd`, `gpsdo-monitor`);
sigmond expects every client to conform to a documented
[client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
covering install, lifecycle, logging, configuration interview, and
output-sink declaration.

The actual sensor driver — speaking I²C to the RM3100 through a
Pololu adapter, reading the MCP9808 ambient-temperature sensor on
the same bus, and emitting JSONL — is **Dave Witten's `mag-usb`**.
That code already existed, already worked, and embodies a lot of
hardware-specific knowledge (RM3100 register map, Pololu adapter
quirks, gain equations from the PNI factory).  Reimplementing it
in Python would have been pure duplication.

So `mag-recorder` is structured as a thin Python wrapper that:

- spawns `mag-usb` as a subprocess and reads its JSONL stdout
  (or, when no hardware is available, swaps in a synthetic
  source so the rest of the pipeline can be developed and
  demoed),
- re-stamps each line with a millisecond-precision ISO-8601 UTC
  timestamp (upstream emits second-resolution),
- appends to a daily JSONL spool that rotates at UTC midnight,
- bundles each closed day into `OBS<date>T00:00.zip`,
- ships the zip to PSWS via SFTP using the same SSH key and
  trigger-directory convention as the Grape uploader, and
- exposes the sigmond contract surface (`inventory --json`,
  `validate --json`, `config init/edit`, systemd units, deploy
  manifest) so `smd install mag-recorder` and friends work.

None of the I²C, USB, or RM3100-protocol code lives in mag-recorder.
That is `mag-usb`'s job, and we keep it that way.

## Upstream: `wittend/mag-usb`

- Repository: [github.com/wittend/mag-usb](https://github.com/wittend/mag-usb)
- Author: David Witten (KD0EAG)
- License: GPL-3.0
- Documentation: [mag-usb.readthedocs.io](https://mag-usb.readthedocs.io/)

`mag-usb` is itself a fork-of-a-fork: Dave's older Grape2 Raspberry-Pi
implementation, generalized to run on any Linux host with USB
(LattePanda, Beelink, etc.) by routing I²C through a Pololu USB-to-I²C
adapter instead of the Pi's native GPIO/I²C lines.  It has been in
field use under HamSCI for several years.

## Patches contributed back: `HamSCI/mag-usb` PR #1

While integrating `mag-usb` we found and fixed ten independent
issues.  All ten landed on the `sigmond-integration` topic branch
of our fork ([github.com/HamSCI/mag-usb](https://github.com/HamSCI/mag-usb)),
each as its own atomic commit so Dave can cherry-pick selectively.
They were submitted upstream as
[wittend/mag-usb PR #1](https://github.com/wittend/mag-usb/pull/1)
on 2026-05-13.

### Headline fixes

1. **Cadence rewrite (`8b3a6c5`).**  The "occasional missed sample"
   bug Dave mentioned was caused by

       formatOutput(p);
       nanosleep({1, 0}, NULL);

   sleeping 1 s *after* each I²C round-trip finished.  Total drift
   accumulated each cycle, and once it exceeded a whole second the
   matching UTC tick was silently skipped.  The replacement uses
   `clock_nanosleep(CLOCK_REALTIME, TIMER_ABSTIME, ...)` anchored
   on absolute deadlines that advance by exactly +1 s per tick, and
   explicitly logs a `{ "lastStatus": "missed_sample", "deadline": ... }`
   line whenever `formatOutput()` overruns — so a gap is now
   auditable instead of invisible.

2. **Signal handling (`40653ef`).**  `signal_handler_thread()` calls
   `sigwait()` to drive a graceful `shutdown_requested` handshake,
   but `pthread_sigmask()` had only run *inside* the signal thread.
   `pthread_create()` inherits the calling thread's mask, so the
   print and sensor threads were spawned with the default (empty)
   mask — meaning the kernel delivered SIGINT/SIGTERM to one of
   those workers, where the default disposition is "terminate."
   Ctrl-C bypassed the entire graceful shutdown path.  The fix:
   block SIGHUP/SIGABRT/SIGINT in the calling thread *before* any
   `pthread_create` call, so every worker inherits the masked set
   and `sigwait()` is the only place the signals can fire.

### Other bug fixes

3. **udev rule (`34a0507`)** — covered only PID `0x2503` (5397).
   PID `0x2502` (5396, the non-isolated-power variant) got no
   `/dev/ttyMAG0` symlink.  Also added explicit `GROUP="dialout"`
   and `MODE="0660"` so a non-root user in `dialout` can open the
   adapter without sudo.
4. **Typo `magata.fifo` → `magdata.fifo` (`d376aa6`)** — the
   hardcoded default FIFO path disagreed with both `src/config.toml`
   and `docs/Configuration.md`.
5. **Default `portpath` → `/dev/ttyMAG0` (`3e366b1`)** — aligned
   the in-code default with the udev rule that already creates
   that symlink.  `-O /dev/ttyACMn` still works as an explicit
   override when the rule isn't installed.
6. **Pipe-path leak fix (`454d15e`)** — `[output].pipe_in_path` /
   `pipe_out_path` `strdup()`'d into pointers without freeing the
   default strdup'd allocations from `setProgramDefaults()`.
7. **`sprintf` → `snprintf` in `i2c_open` (`fa2011e`)** —
   `p->portpath` is up to PATH_MAX (4096) bytes but the error
   buffer was 1024 bytes.  Long-path overflow.
8. **Hardcoded `0x23` in `i2c_verifyMagSensor` (`e4bb01b`)** — the
   function ignored `p->magAddr` from config and could return 0
   (false success) on a version mismatch when the bus returned
   zeros.  Now uses the configured address and returns distinct
   non-zero sentinels for bus failure vs version mismatch.
9. **Off-by-one in `i2c_pololu_write_to` (`7cc5c24`)** — declared
   `cmd[258]` but the framing is `4 + size` bytes with `size` up
   to 255, so a maximal write would write `cmd[258]` (one past the
   end).  Unreachable from this codebase but real for any
   downstream user of the adapter library.
10. **Doc + help-text drift (`ba735a1`)** — `-h` still printed
    `default: /dev/ttyACM0`; `drdy_delay` is documented as
    microseconds but the implementation does `usleep(value * 1000)`
    (i.e. milliseconds).  Code is right, docs were wrong; doc
    follows code now.

PR #1 was opened against `wittend/mag-usb:master` on 2026-05-13 and
is awaiting Dave's review.  Until it merges, the recommended
`mag-usb` checkout for use with `mag-recorder` is our fork's
`sigmond-integration` branch:

```bash
git clone -b sigmond-integration https://github.com/HamSCI/mag-usb
```

After PR #1 merges, the upstream `wittend/mag-usb:master` will
contain everything we need and the fork can be retired.

## Architectural separation

`mag-recorder` and `mag-usb` are two distinct programs:

| Aspect | `mag-usb` (upstream C) | `mag-recorder` (this repo, Python) |
|---|---|---|
| Language | C | Python 3.11+ |
| License | GPL-3.0 | MIT |
| Process | Subprocess, single binary | Long-running daemon + CLI tools |
| Role | Sensor driver: I²C, RM3100, MCP9808 | Sigmond client: contract, spool, package, upload |
| Knows about | Pololu adapter, RM3100 registers, MCP9808 decoding, orientation rotations, gain math | UTC days, ISO-8601, zips, SFTP, PSWS, hs-uploader, sigmond catalog |
| Output | JSONL on stdout, one line per UTC second | Daily JSONL spool → daily zip → SFTP to PSWS |

The communication boundary is `mag-usb`'s **stdout pipe**.
`mag-recorder` spawns `mag-usb` with `subprocess.Popen`, reads
its newline-delimited JSON, and ignores everything else.  No
shared headers, no shared libraries, no linking, no FFI.  This is
deliberate — both for licensing reasons (next section) and
because subprocess pipes are a stable, debuggable interface that
survives upstream rewrites.

## Licensing analysis (GPL vs MIT)

`mag-usb` is GPL-3.0.  `mag-recorder` is MIT.  These licenses can
coexist because the two programs run as **separate processes**
communicating over an OS-level pipe, which is the canonical example
of "aggregation" under the GPL FAQ:

> [Mere aggregation of two programs means putting them side by side
> on the same CD-ROM or hard disk.](https://www.gnu.org/licenses/gpl-faq.html#MereAggregation)
>
> [If two programs are designed to run as separate processes
> communicating with each other (e.g. by pipes, sockets, or command
> line arguments)…](https://www.gnu.org/licenses/gpl-faq.html#GPLPlugins)
> they are normally considered "separate works" and the GPL on one
> does not propagate to the other.

So:

- Shipping `mag-recorder` (MIT) alongside `mag-usb` (GPL-3.0) on
  the same disk image is **mere aggregation** — neither license
  prevents this.
- `mag-recorder` spawns `mag-usb` as a child process and reads
  its stdout; it does not link against any GPL code, does not
  embed GPL headers, and does not statically incorporate any
  GPL data.  Therefore the GPL of `mag-usb` does not "infect"
  `mag-recorder`.
- A downstream operator can redistribute both programs together as
  long as `mag-usb`'s GPL terms are honored for `mag-usb`'s code
  (i.e. source must be available, GPL notice must be preserved)
  and `mag-recorder`'s MIT terms are honored for `mag-recorder`'s
  code.

The contributions we made *back* to `mag-usb` (PR #1) are GPL-3.0
by definition — we wrote them, but contributing them to a GPL
codebase makes them GPL.  Our own MIT-licensed code in this repo
does not include or vendor any of those patches; they live in
`wittend/mag-usb` (or our fork of it), not here.

If `mag-usb` were ever statically linked into `mag-recorder`, or
if we vendored its source under `mag-recorder/vendor/`, this
analysis would change — at that point `mag-recorder` would have
to relicense to GPL-3.0 or stop incorporating the GPL code.  We
do neither, on purpose.

## Acknowledgments

- **Dave Witten, KD0EAG** for the upstream `mag-usb` codebase and
  for the original Grape2 implementation it descends from.
- **The HamSCI / TangerineSDR / Grape project** for the RM3100
  carrier-board design, the PSWS upload server, and the dataset
  conventions (`OBS<date>T<HH:MM>` naming, trigger directory
  format) that we mirror.
- **Rob Robinett (AI6VN) and the wsprdaemon project** for the
  SFTP-put-then-trigger upload pattern that the PSWS Grape
  pipeline (and now this magnetometer pipeline) inherits.
