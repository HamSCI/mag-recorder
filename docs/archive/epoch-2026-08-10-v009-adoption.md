# Archive epoch: mag-usb v0.0.6 → v0.0.9-sigmond.1 (2026-08-10)

**Stop window:** 20:38:20 – 20:49:14 UTC (spool gap ~11 min).
**Binary:** `sigmond-integration-retired-20260807` (76c7b7c, v0.0.6 lineage)
→ **`v0.0.9-sigmond.1`** (8595c24 = wittend v0.0.9 6e660577 + PRs
wittend#10/#11/#12). Same chip physics by design: CC=400 programmed at
init, gain 148.

## Bracketed A/B (B4 RM3100, 90/90/90/60 s legs, `~hamsci/w2-ab/`)

| leg | binary | chip state | x ratio vs pre | y | z |
|---|---|---|---|---|---|
| pre | v0.0.6 | warm | 1.000000 | 1.000000 | 1.000000 |
| warm | candidate | warm | 1.000180 | 0.999958 | 1.000095 |
| cold | candidate | **power-cycled** | 0.988380 | 1.000398 | 1.037089 |
| post | v0.0.6 | power-cycled | 0.988063 | 1.000388 | 1.038086 |

**Binary parity: PASS.** warm≡pre (≤0.02%) and cold≡post (≤0.1%) — the
candidate reads identically to v0.0.6 in both chip states, including the
cold-start case the 2026-08-07 A/B could not test (chip retains CC across
binary swaps; only a power cycle exposes the init path).

## ⚠ Environmental field step at the replug — NOT a binary effect

pre/warm vs cold/post differ: **z +1122 nT (+3.7%), x −370 nT (−1.2%),
|B| +0.8%** — introduced by the physical unplug/replug of the Pololu
adapter (v0.0.6 itself reads the shifted values). Too large/fast for
geophysics; the sensor or nearby magnetic material moved. **Annotate the
B4 archive: step at ~2026-08-10 20:43 UTC, persists after restart.**
Sensor mounting to be inspected; if it is re-seated later, that is a
second annotated step, not a reversal of this one.

## Also deployed in the same window

Timing-provenance sidecar (mag-recorder f49697a + hamsci-dsp 0.4.0):
`timing-YYYY-MM-DD.jsonl` beside the spool, bundled into the daily OBS
zip. First live line 20:49:15: `CHRONY_FUSE / T4, stratum 1,
sigma 670 µs`.
