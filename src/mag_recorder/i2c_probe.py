"""Probe the magnetometer's I2C addresses and write what answers.

Install runs ``mag-recorder probe-i2c`` on every run, a reflash included.
The RM3100 sits at 0x20-0x23 and the MCP9808 at 0x18-0x1F, depending on
the carrier board's strapping.  Nothing here is hard-coded or carried over:
the address that answers wins.  The probe never blocks an install, so every
path returns 0.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

DEFAULT_CONFIG = Path("/etc/mag-recorder/mag-recorder-config.toml")
MAG_RANGE = range(0x20, 0x24)
TEMP_RANGE = range(0x18, 0x20)

_ADDR_RE = re.compile(r"Address:\s*0x([0-9A-Fa-f]{1,2})\b")


def parse_scan(text: str) -> list[int]:
    """Every ``Address: 0xNN`` in ``mag-usb -S`` output."""
    return [int(m.group(1), 16) for m in _ADDR_RE.finditer(text)]


def _pick(addresses, rng, what):
    hits = sorted({a for a in addresses if a in rng})
    lo, hi = rng[0], rng[-1]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, (f"no {what} answered in 0x{lo:02X}-0x{hi:02X}; "
                      f"leaving its address unchanged")
    names = ", ".join(f"0x{a:02X}" for a in hits)
    return None, (f"several devices answered for the {what} in "
                  f"0x{lo:02X}-0x{hi:02X} ({names}); leaving its address unchanged")


def choose(addresses: list[int]) -> tuple[int | None, int | None, list[str]]:
    """(magnetometer, temperature, warnings); ambiguity picks None."""
    mag, w1 = _pick(addresses, MAG_RANGE, "magnetometer")
    temp, w2 = _pick(addresses, TEMP_RANGE, "temperature sensor")
    return mag, temp, [w for w in (w1, w2) if w]


def _hex(v: int) -> str:
    return f"0x{v:02X}"


def write_addresses(config_path: Path, mag: int | None,
                    temp: int | None) -> list[str]:
    """Edit the live address lines under ``[mag]``, keeping every comment."""
    config_path = Path(config_path)
    lines = config_path.read_text().splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines)
                  if l.strip() == "[mag]"), None)
    if start is None:
        return ["no [mag] table in the config; nothing written"]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break

    def live(key):
        pat = re.compile(rf"^(\s*{key}\s*=\s*)(0[xX][0-9A-Fa-f]+|\d+)(.*)$",
                         re.S)
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if m:
                return i, m
        return None, None

    done = []
    for key, val in (("i2c_address", mag), ("remote_temp_address", temp)):
        if val is None:
            continue
        i, m = live(key)
        if i is not None:
            lines[i] = f"{m.group(1)}{_hex(val)}{m.group(3)}"
            if not lines[i].endswith("\n"):
                lines[i] += "\n"
        else:
            anchor = live("i2c_address")[0]
            if anchor is None:
                # after the last non-blank line of the table
                anchor = max(j for j in range(start, end)
                             if lines[j].strip())
            if not lines[anchor].endswith("\n"):
                lines[anchor] += "\n"
            lines.insert(anchor + 1, f"{key} = {_hex(val)}\n")
            end += 1
        done.append(f"{key} = {_hex(val)}")
    config_path.write_text("".join(lines))
    return done


def probe(config_path: Path, *, device: Path = Path("/dev/ttyMAG0"),
          mag_usb: str = "/usr/local/bin/mag-usb",
          runner=subprocess.run, timeout: int = 10) -> int:
    """Scan, choose, write.  Always returns 0."""
    if not Path(device).exists():
        print(f"probe-i2c: {device} absent; skipping the I2C probe")
        return 0
    try:
        r = runner([mag_usb, "-S"], capture_output=True, text=True,
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"probe-i2c: WARN mag-usb -S timed out after {timeout} s; "
              f"addresses unchanged")
        return 0
    except OSError as e:
        print(f"probe-i2c: WARN could not run {mag_usb}: {e}; "
              f"addresses unchanged")
        return 0
    out = (r.stdout or "") + (r.stderr or "")
    found = parse_scan(out)
    if r.returncode != 0 and not found:
        print(f"probe-i2c: WARN mag-usb -S failed (rc {r.returncode}): "
              f"{out.strip()[:200]}; addresses unchanged")
        return 0
    print("probe-i2c: answered: "
          + (", ".join(_hex(a) for a in found) or "nothing"))
    mag, temp, warns = choose(found)
    for w in warns:
        print(f"probe-i2c: WARN {w}")
    try:
        for line in write_addresses(Path(config_path), mag, temp):
            print(f"probe-i2c: wrote {line} to {config_path}")
    except OSError as e:
        print(f"probe-i2c: WARN could not write {config_path}: {e}")
    return 0
