# Plan: mag-usb v0.0.9 adoption, MQTT/TLS enablement, timing provenance

**Date:** 2026-08-10 (Michael + Claude)
**Context:** `bin/mag-usb` is pinned to `sigmond-integration-retired-20260807`
(v0.0.6 lineage) because upstream v0.0.9 never calls
`setCycleCountRegs()`/`setNOSReg()` — CC/NOS never reach the chip and gain
stays at the GAIN_150 default (−1.31% vs our lineage; worse from cold start).
See `mag-usb-upstream.md` banner and `upstream-report-2026-08.md` §4.
Upstream issues filed 2026-08-07 (wittend/mag-usb #7 #8 #9); as of 2026-08-10
Dave has neither committed nor commented. Decision (Michael): don't wait —
PR the offered fixes upstream AND stage a patched build for sigmond now.
A separate driver: mag products carry host-clock timestamps with no timing
provenance (hf-timestd split spec §11 open item) — fixed here as W4.

## W1 — upstream PRs to wittend/mag-usb  [PRs OPEN 2026-08-10]

Branches on `HamSCI/mag-usb` (mirror of wittend/master), PRs to
`wittend/master`. Small, single-purpose, merge-friendly.

- [x] **PR-A (fixes #8, adoption blocker):** call `setCycleCountRegs()` +
      `setNOSReg()` in master's init path (re-site of our original PR #1,
      which v0.0.6 lineage carries at `i2c.c:324-325`). After it: CC=400
      programmed on-chip, gain 148 — byte-parity physics with v0.0.6 lineage.
- [x] **PR-B (fixes #7 §2.1):** TLS verification in `mqtt_client.c` —
      `SSL_CTX_set_verify(SSL_VERIFY_PEER)`, default trust store + optional
      CA-file config key, SNI, `SSL_set1_host()`, `SSL_get_verify_result()`
      check after connect.
- [x] **PR-C (fixes #7 §2.2):** inbound parser hardening — cap
      remaining-length decode at 4 bytes, check `malloc` return, require
      `rem_len >= 2` before reading `msg[0..1]`.
- Reconnect (§2.3), CONNECT length limit (§2.4) and fleet defaults (§2.5)
  stay as issue notes — not in these PRs.
- Acceptance: each branch builds clean (cmake, Debian 13/gcc 14), PR-A
  verified by `-P` register readback where hardware permits; PRs reference
  the issues and stay identical to what sigmond ships (W2).

## W2 — sigmond adoption of patched v0.0.9  [DONE 2026-08-10 — see docs/epoch-2026-08-10-v009-adoption.md]

- [x] Tag `HamSCI/mag-usb` = wittend/master (6e660577) + PR-A/B/C patches
      as `v0.0.9-sigmond.1` (release staging, NOT fork revival — content
      identical to in-flight PRs; drop the tag when Dave merges).
- [x] Repoint `scripts/build-mag-usb.sh` `MAG_USB_REF`; rebuild bundled
      binary per `sigmond/docs/native-binaries.md`; provenance diff.
- [x] Bracketed A/B on B4's RM3100 incl. a **cold-start leg** (power-cycled
      chip must read parity with v0.0.6 — the case the 2026-08-07 A/B could
      not see). Expect ratio ≈ 1.000; document as a timestamped epoch note
      either way (noise floor change sd ~18 → ~14 nT is expected).
- [x] Update `mag-usb-upstream.md` banner to the new state.
- Requires: B4 shell (no working ssh from B3 as of 2026-08-10) + ~2 min
  mag-recorder stop, coordinated with rob.

## W3 — MQTT enablement in mag-recorder  [AFTER W2]

- [ ] `[mqtt]` config surface in mag-recorder-config.toml: enable (default
      false), broker host/port (default 8883), CA path, per-station
      `client_id`/`topic` derived from station identity, credentials.
- [ ] Render mag-usb's config from it; spool stays the authoritative
      archive, MQTT is best-effort live visualisation (upstream-report §3).
- [ ] Open decision (Michael/rob): broker placement — station-local
      mosquitto vs central (gw2). Does not block the config surface.
- Gate: ships only with a TLS-verifying binary (PR-B content).

## W4 — timing provenance sidecar  [DONE 2026-08-10]

- [x] `hamsci_dsp.timing`: add a sysclock-frame provenance helper — same
      block shape as `to_timing_authority`, sourced from chrony tracking
      (`timing_source = "CHRONY_<refid>"`, σ from tracking RMS/root
      dispersion, explicit fallback block when chronyc is unavailable).
      Fleet-uniform: any host-clock instrument client can use it.
- [x] mag-recorder: TimingSidecar (core/timing_sidecar.py) polls chrony
      every 60 s, writes timing-YYYY-MM-DD.jsonl on material change +
      10-min heartbeat; packager bundles it into the daily OBS zip.
- Reference: hf-timestd `docs/design/HF_TIMESTD_SPLIT_DESIGN.md` §11
  (magnetometer provenance gap).

## Sequencing

W1 now (B3-only). W4 next (B3-only). W2 when B4 access exists. W3 after W2.
