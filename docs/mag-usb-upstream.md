# mag-usb upstream / fork management

`mag-usb` is the small C utility that talks to the Pololu USB-I2C
adapter and reads RM3100 magnetometer samples for `mag-recorder` to
package and upload. Its source lives outside this repo. This doc
captures the upstream/fork relationship and the maintenance cadence,
so the next person to touch `bin/mag-usb` or
`scripts/build-mag-usb.sh` knows which repo to pull from and why.

## Repo topology

```
wittend/mag-usb              (upstream — Dave Witten)
       ↑ PRs accepted on Dave's schedule
       |
mijahauan/mag-usb            (AC0G's fork — authoritative for sigmond)
   - main                ← fast-forwards from wittend/main
   - sigmond-integration ← branch that sigmond pins to
       ↑ rebased onto main when upstream moves
       |
mag-recorder/bin/mag-usb     (this repo — bundled binary)
mag-recorder/bin/mag-usb.provenance  (records the exact upstream SHA)
```

`scripts/build-mag-usb.sh` defaults to
`https://github.com/mijahauan/mag-usb.git` on the `sigmond-integration`
branch. Operators or maintainers can override with `MAG_USB_URL=...`
and `MAG_USB_REF=...` for testing.

## Why pin to the fork (not to upstream directly)

Even when `mijahauan/mag-usb sigmond-integration` and `wittend/main`
are byte-identical, sigmond stays pinned to the fork. The fork costs
near-nothing to maintain at parity, and it buys:

- **A stable contract.** `sigmond-integration` is a branch name we
  control. Upstream can rename `main`, force-push, archive the repo,
  or accept a contentious PR — none of that breaks sigmond builds.
- **Low hotfix latency.** A bug found in production gets fixed on
  `sigmond-integration` and rebuilt today; the upstream PR happens
  on AC0G's schedule, not as a blocker.
- **Opt-in to upstream features.** When Dave adds something new
  (e.g. a planned MQTT mode), the fork lets sigmond evaluate and
  adopt on its own timeline rather than inheriting the change on
  the next `git pull`.
- **Auditable provenance.** `bin/mag-usb.provenance` records the
  exact `mijahauan/mag-usb` SHA. "What's in this binary?" is always
  a one-line answer.

## Sync cadence

### Upstream → fork (when wittend/main moves)

On a workstation with the fork checked out:

```sh
cd /path/to/mag-usb     # the mijahauan fork
git fetch origin
git checkout main
git merge --ff-only origin/main
git checkout sigmond-integration
git rebase main           # zero-op if nothing diverges, otherwise resolve
git push origin sigmond-integration
```

If `sigmond-integration` has only upstream-mergeable commits and you've
been upstreaming as you go, the rebase is a no-op and your fork stays
trivially in sync.

### Fork → sigmond (when sigmond ships a new build)

On bee1 (or wherever you build for x86_64 bookworm):

```sh
sudo /opt/git/sigmond/mag-recorder/scripts/build-mag-usb.sh --force
cd /opt/git/sigmond/mag-recorder
git diff bin/mag-usb.provenance        # confirm SHA and toolchain version movement
git add bin/mag-usb bin/mag-usb.provenance
git commit -m "mag-usb: rebuild bundled binary (rev <SHA>)"
git push
```

The `.provenance` diff is the audit trail — it shows exactly which
upstream SHAs and toolchain versions produced the new binary.

### Fork → upstream (contribution back to wittend)

Cherry-pick or PR from `sigmond-integration` → `wittend/main`. Does
not affect sigmond's pinning. The goal over time is to keep the diff
between `sigmond-integration` and `wittend/main` as small as possible,
so the fork is more "stable release branch" than "long-lived divergent
fork."

## Handling upcoming upstream changes (MQTT, etc.)

When Dave merges a new feature into `wittend/main`:

1. **Decide if sigmond wants it.** MQTT, for instance, would add a
   runtime dep (e.g. `mosquitto-clients`) and a config surface in
   `mag-recorder-config.toml`. Worth a deliberate decision, not a
   silent inheritance.
2. **If yes:** pull into `sigmond-integration`, update
   `scripts/build-mag-usb.sh` (build flags, apt deps),
   `mag-recorder/deploy.toml` (`[[deps.apt]]` if new runtime deps),
   the config template, and possibly the client `contract` version.
3. **If yes but optional:** ask Dave to make it a build flag (e.g.
   `-DENABLE_MQTT=OFF`). Then `build-mag-usb.sh` chooses sigmond's
   profile while staying source-compatible with upstream.
4. **If not yet:** don't pull. `sigmond-integration` stays where it
   is, sigmond keeps shipping the older binary, and the decision can
   be revisited later.

## When the fork might go away

If `sigmond-integration` is at parity with `wittend/main` for several
months and Dave's release/branch policy is stable, an alternative is
to pin sigmond to a *tagged release* on the fork
(e.g. `mijahauan/mag-usb @ v0.0.7`). Still our fork, but a more
conservative ref than a branch tip. Pinning directly to
`wittend/<ref>` is not recommended for the reasons in §"Why pin to
the fork."

## See also

- `sigmond/docs/native-binaries.md` — the `bin/`, `scripts/`,
  `.provenance`, `install.sh` contract used by every sigmond client
  that bundles a native binary.
- `scripts/build-mag-usb.sh` — the actual build pipeline.
- `bin/mag-usb.provenance` — current binary's upstream SHA + build host.
