"""CONTRACT v0.8 §14 — `config init` / `config edit` interview.

Three operator-facing modes:

1. **Interactive (default)** — when stdout is a TTY and ``whiptail``
   is available, exec the shell wizard at
   ``scripts/config-wizard.sh``.  The wizard uses ``config show
   --json --defaults`` and ``config apply --json -`` to round-trip
   through this module; the wizard owns the UI, this module owns
   the schema, validation, and TOML write.

2. **Legacy fallback** — when whiptail is missing or stdout isn't a
   TTY, ``init`` writes the template with ``STATION_*`` env-bag
   substitutions and ``edit`` prints the current file with a hint
   about which placeholders still need attention.  Predates the
   wizard; still the right thing for first-time installs on a host
   without whiptail.

3. **--non-interactive** — same as the legacy fallback, used by
   sigmond's first-run interview and any scripted deploy.  Never
   prompts; never execs the wizard.

The wizard / sigmond interop seam:

  $ mag-recorder config show --json [--defaults] [--config /path]
        → prints the effective config as JSON to stdout
  $ mag-recorder config apply --json - [--config /path]
        → reads a JSON dict on stdin, validates, writes the TOML

These are the only two surfaces the wizard touches.  All schema
knowledge stays in this module + mag_recorder.config.DEFAULTS.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mag_recorder.config import DEFAULTS, DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "config" \
    / "mag-recorder-config.toml.template"

_WIZARD_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" \
    / "config-wizard.sh"

_HELP_TOML_PATH = Path(__file__).resolve().parent.parent.parent / "config" \
    / "help.toml"


_ENV_SUBSTITUTIONS: dict[str, str] = {
    "<YOUR_PSWS_STATION_ID>": "MAG_RECORDER_STATION_ID",
    "<YOUR_CALL>":            "STATION_CALLSIGN",
    "<YOUR_GRID>":            "STATION_GRID",
}


# ---------------------------------------------------------------------------
# Template + env-bag rendering (legacy fallback path)
# ---------------------------------------------------------------------------

def _resolve_template_path() -> Path:
    """Locate the bundled template (dev checkout, then deploy share)."""
    if _TEMPLATE_PATH.is_file():
        return _TEMPLATE_PATH
    fallback = Path("/opt/mag-recorder/share/mag-recorder-config.toml.template")
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        f"template not found in either {_TEMPLATE_PATH} or {fallback}; "
        "is mag-recorder installed?"
    )


def _render_from_env(template_text: str) -> str:
    out = template_text
    for placeholder, env_key in _ENV_SUBSTITUTIONS.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            out = out.replace(placeholder, val)
    return out


def _legacy_init(dst: Path, reconfig: bool) -> int:
    if dst.exists() and not reconfig:
        print(f"refusing to overwrite existing config: {dst}")
        print("re-run with --reconfig to replace, or use `config edit` instead")
        return 1
    src = _resolve_template_path()
    rendered = _render_from_env(src.read_text(encoding="utf-8"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8")
    try:
        dst.chmod(0o644)
    except PermissionError:
        pass
    print(f"wrote {dst}")
    print("edit station + uploader fields before starting the daemon.")
    return 0


def _legacy_edit(src: Path) -> int:
    if not src.exists():
        print(f"no config found at {src}; run `mag-recorder config init` first")
        return 1
    text = src.read_text(encoding="utf-8")
    print(f"config: {src}\n")
    print(text)
    placeholders = [p for p in _ENV_SUBSTITUTIONS if p in text]
    if placeholders:
        print("\n--- still placeholder ---")
        for p in placeholders:
            env_key = _ENV_SUBSTITUTIONS[p]
            print(f"  {p}   (set env {env_key} or edit the file directly)")
        return 1
    return 0


# ---------------------------------------------------------------------------
# config show --json [--defaults] / config apply --json -
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return a new dict: ``overlay`` keys win, recursing into nested dicts."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def cmd_config_show(args) -> int:
    """Emit the current config as JSON on stdout.

    With ``--defaults`` the output is ``DEFAULTS`` deep-merged with
    whatever's in the TOML file, so the wizard sees every key with
    a sensible value even on a freshly-installed host where the
    operator hasn't touched a thing.

    Without ``--defaults`` only the keys actually present in the
    TOML file appear; useful for downstream tooling that wants to
    know "what did the operator override?"
    """
    config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    if getattr(args, "defaults", False):
        # load_config() already does the deep-merge with DEFAULTS internally,
        # but its return value is the effective config -- exactly what we want.
        effective = load_config(config_path) if config_path.is_file() \
                    else copy.deepcopy(DEFAULTS)
        out = effective
    else:
        if not config_path.is_file():
            out = {}
        else:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(config_path, "rb") as f:
                out = tomllib.load(f)
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_config_apply(args) -> int:
    """Read a JSON dict on stdin, validate, atomically write the TOML.

    The wizard collects answers in shell variables, builds one JSON
    document, and pipes it here.  Single Python-side write keeps
    the validation surface small (one entry point, one schema check,
    one .part-rename) and avoids per-field IPC.

    Validation:
      - For every key the operator provided, the value's type must
        match the type DEFAULTS declares for that key.
      - Numeric ranges checked against driver_config.render()'s
        explicit guards (cycle_count 1..800, i2c_address 1..0x7F,
        sampling_mode 'POLL'|'CMM') by calling render() on a tmp
        path -- any ValueError there fails the apply.
      - Unknown top-level sections are rejected so a typo doesn't
        silently land in the TOML.

    On success the file is written atomically (.part + rename) so a
    crash mid-write leaves the previous file intact.
    """
    config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"config apply: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print(f"config apply: top-level JSON must be an object, got {type(payload).__name__}",
              file=sys.stderr)
        return 2

    # Reject unknown top-level sections.
    unknown = set(payload.keys()) - set(DEFAULTS.keys())
    if unknown:
        print(f"config apply: unknown section(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    # Per-key type check against DEFAULTS.  Allow None values (= "use
    # the default").  Numeric tightening (int vs float) follows DEFAULTS.
    for section, fields in payload.items():
        if not isinstance(fields, dict):
            print(f"config apply: [{section}] must be a table, got {type(fields).__name__}",
                  file=sys.stderr)
            return 2
        for key, val in fields.items():
            default = DEFAULTS[section].get(key)
            if default is None or val is None:
                continue
            if isinstance(default, bool):
                if not isinstance(val, bool):
                    print(f"config apply: [{section}].{key} expects bool, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2
            elif isinstance(default, int) and not isinstance(default, bool):
                # int OR float (the wizard sometimes hands us 1.0 for an int slot)
                if not isinstance(val, (int, float)) or isinstance(val, bool):
                    print(f"config apply: [{section}].{key} expects number, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2
                fields[key] = int(val) if isinstance(default, int) else float(val)
            elif isinstance(default, float):
                if not isinstance(val, (int, float)) or isinstance(val, bool):
                    print(f"config apply: [{section}].{key} expects number, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2
                fields[key] = float(val)
            elif isinstance(default, str):
                if not isinstance(val, str):
                    print(f"config apply: [{section}].{key} expects string, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2

    # Deep-merge with the existing file so partial payloads (the
    # common wizard case -- "change just these 4 fields") preserve
    # everything the operator had set previously.  Default if the
    # file is missing: start from DEFAULTS.
    existing = load_config(config_path) if config_path.is_file() else copy.deepcopy(DEFAULTS)
    merged = _deep_merge(existing, payload)

    # Cross-field invariants: round-trip through driver_config.render()
    # against a throwaway path.  Catches the cycle_count/i2c_address/
    # sampling_mode range guards in one call.
    from mag_recorder.core.driver_config import render as _render_driver
    import tempfile
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", prefix="mag-usb-driver-validate.", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            _render_driver(merged, tmp_path)
        except ValueError as exc:
            print(f"config apply: rejected by driver_config.render(): {exc}",
                  file=sys.stderr)
            return 2
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    # Write back as TOML.  Hand-emit so we don't take a dep on
    # tomli-w / tomlkit just for this; the schema is shallow (string
    # / int / float / bool / nested-dict, never arrays-of-tables).
    text = _serialize_toml(merged)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o644)
    except PermissionError:
        pass
    tmp.replace(config_path)
    print(f"wrote {config_path}")
    return 0


def _toml_scalar(v: Any) -> str:
    """Render one TOML scalar (string/int/float/bool)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # f-string keeps trailing zeros; force the decimal for floats.
        s = repr(v)
        if "." not in s and "e" not in s and "E" not in s:
            s += ".0"
        return s
    if isinstance(v, str):
        # Basic string: escape backslash + double quote, no multiline.
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"unsupported TOML scalar type: {type(v).__name__}")


def _serialize_toml(d: dict, parent: str = "") -> str:
    """Serialize ``d`` to a deterministic TOML string.

    Top-level scalars first (none in our schema), then ``[section]``
    blocks in DEFAULTS key order so the rendered file matches the
    template's section ordering and an operator diff'ing successive
    writes sees minimal churn.  Recurses into nested dicts as
    ``[parent.child]`` headers (used today only by
    [mag.orientation]).
    """
    # Stable order: DEFAULTS first (so the rendered file matches the
    # template's section ordering), then any extra keys we don't
    # recognize in alphabetical order.
    keys_in_default_order = [k for k in DEFAULTS.keys() if k in d]
    extras = sorted(k for k in d.keys() if k not in DEFAULTS)
    ordered_keys = keys_in_default_order + extras

    out_lines: list[str] = []
    for section_name in ordered_keys:
        section = d[section_name]
        if not isinstance(section, dict):
            # Top-level scalars (none in our schema today).
            out_lines.append(f"{section_name} = {_toml_scalar(section)}")
            continue

        header = f"{parent}.{section_name}" if parent else section_name
        out_lines.append("")
        out_lines.append(f"[{header}]")

        # Within a section: scalar keys first (defaults order), then
        # nested-dict children as their own `[section.child]` blocks.
        scalars = {k: v for k, v in section.items() if not isinstance(v, dict)}
        children = {k: v for k, v in section.items() if isinstance(v, dict)}

        # Preserve DEFAULTS key order inside the section too.
        default_section = DEFAULTS.get(section_name, {})
        default_keys = [k for k in default_section.keys() if k in scalars]
        extra_keys = sorted(k for k in scalars.keys() if k not in default_section)
        for k in default_keys + extra_keys:
            out_lines.append(f"{k} = {_toml_scalar(scalars[k])}")

        for child_name, child in children.items():
            child_text = _serialize_toml({child_name: child}, parent=header)
            out_lines.append(child_text)

    return "\n".join(out_lines).lstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# config init / config edit dispatcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Wizard dispatch: delegate to sigmond.wizard_dispatch when sigmond is
# importable (the canonical shared lib, since mag-recorder + psk-recorder +
# wspr-recorder all need the same dispatch contract); fall back to the
# original local implementation when sigmond isn't installed (older
# deploys, standalone-host operators who skipped `pip install -e
# /opt/git/sigmond/sigmond`).  Local fallback keeps the previous behaviour
# exactly so mag-recorder still works standalone.
# ---------------------------------------------------------------------------

try:
    import sigmond.wizard_dispatch as _sigmond_wd
    # Pin the API contract version.  A future incompatible bump in
    # sigmond should fail loudly here rather than silently dispatch
    # through the wrong call signature.
    assert _sigmond_wd.SIGMOND_WIZARD_DISPATCH_API == "1", (
        f"sigmond.wizard_dispatch API "
        f"{_sigmond_wd.SIGMOND_WIZARD_DISPATCH_API!r} != '1' "
        f"(expected by mag-recorder)"
    )
    # Locate the shell-side helpers next to the Python module so the
    # wizard script can `source` them regardless of where sigmond
    # ended up on this host.
    _SIGMOND_WIZARD_LIB_SH: Optional[Path] = (
        Path(_sigmond_wd.__file__).resolve().parent / "wizard_dispatch.sh"
    )
    if not _SIGMOND_WIZARD_LIB_SH.is_file():
        _SIGMOND_WIZARD_LIB_SH = None
except (ImportError, AssertionError):
    _sigmond_wd = None
    _SIGMOND_WIZARD_LIB_SH = None


def _wizard_available(args=None) -> bool:
    """True iff we should exec the shell wizard for this run.

    When sigmond is importable, defers to sigmond.wizard_dispatch.
    is_wizard_available(args, _WIZARD_PATH) so all three clients
    (mag-recorder, psk-recorder, wspr-recorder) honour the same gate.

    When sigmond isn't installed, falls back to the original
    standalone check.  The behaviour is bit-for-bit the same as the
    pre-extraction local helper; sigmond's version just made it shared.
    """
    if _sigmond_wd is not None:
        # Callers always have `args` in scope today; defensive default
        # for any future caller that might forget to pass it.
        if args is None:
            args = argparse.Namespace(non_interactive=False)
        return _sigmond_wd.is_wizard_available(args, _WIZARD_PATH)

    # Local fallback (verbatim from pre-extraction).
    if not _WIZARD_PATH.is_file():
        return False
    if not os.access(_WIZARD_PATH, os.X_OK):
        return False
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return False
    return shutil.which("whiptail") is not None


def _exec_wizard(args, mode: str) -> int:
    """Hand off to the shell wizard, preserving --config and stdin/stdout."""
    extra_env: dict = {
        # Tell the wizard where the help sidecar is so it doesn't have
        # to guess (matters when mag-recorder is installed editable
        # from /opt/git/sigmond/mag-recorder and run from /usr/local/bin).
        "MAG_RECORDER_HELP_TOML": str(_HELP_TOML_PATH),
        # The binary path: wizard shells out to `mag-recorder config
        # show/apply` and needs to call the same binary the caller
        # used (so a non-default --config keeps working).
        "MAG_RECORDER_CLI": sys.argv[0],
    }
    extra_args = [mode]
    config_arg = getattr(args, "config", None)
    if config_arg:
        extra_args += ["--config", str(config_arg)]

    if _sigmond_wd is not None:
        # Hand the wizard script the path to sigmond's shell helpers
        # so it can `source` the shared preflight + loggers without
        # hard-coding /opt/git/sigmond/...  Falls through to the
        # script's own :- default when this env var isn't set
        # (direct-invocation safety net).
        if _SIGMOND_WIZARD_LIB_SH is not None:
            extra_env["SIGMOND_WIZARD_LIB_SH"] = str(_SIGMOND_WIZARD_LIB_SH)
        # parse=None: mag-recorder's wizard pipes JSON directly into
        # `mag-recorder config apply` itself; we don't parse stdout.
        result = _sigmond_wd.exec_wizard(
            _WIZARD_PATH,
            extra_env=extra_env,
            parse=None,
            extra_args=extra_args,
        )
        if result.error:
            print(f"wizard exec failed: {result.error}", file=sys.stderr)
            return 1
        return result.returncode

    # Local fallback (sigmond not importable).
    cmd = [str(_WIZARD_PATH)] + extra_args
    env = os.environ.copy()
    env.update(extra_env)
    try:
        return subprocess.call(cmd, env=env)
    except FileNotFoundError as exc:
        print(f"wizard exec failed: {exc}", file=sys.stderr)
        return 1


def cmd_config_init(args) -> int:
    """First-run config wizard / template render."""
    dst = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    non_interactive = getattr(args, "non_interactive", False)

    if non_interactive:
        return _legacy_init(dst, getattr(args, "reconfig", False))

    if _wizard_available(args):
        # Make sure something exists for the wizard to load -- if the
        # operator never ran the legacy template render, write the
        # template first so `config show --defaults` has a real file
        # to start from.  Idempotent: refuses to clobber an existing
        # file unless --reconfig.
        if not dst.exists() or getattr(args, "reconfig", False):
            rv = _legacy_init(dst, getattr(args, "reconfig", False))
            if rv != 0:
                return rv
        return _exec_wizard(args, "init")

    return _legacy_init(dst, getattr(args, "reconfig", False))


def cmd_config_edit(args) -> int:
    """Edit-existing wizard / show-file fallback."""
    src = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    if not src.exists():
        print(f"no config found at {src}; run `mag-recorder config init` first")
        return 1

    if getattr(args, "non_interactive", False):
        return _legacy_edit(src)

    if _wizard_available(args):
        return _exec_wizard(args, "edit")

    return _legacy_edit(src)


# ---------------------------------------------------------------------------
# Wired into cli.py via argparse sub-commands
# ---------------------------------------------------------------------------

def add_show_apply_subparsers(cfg_sub: argparse._SubParsersAction,
                              *, common=None) -> None:
    """Register `config show` and `config apply` on the config subparser.

    Called from cli.py so the argparse tree stays in one place.
    ``common`` is the local ``_add_common(sub)`` helper, hand-passed
    so we don't have to import private helpers across modules.
    """
    sub_show = cfg_sub.add_parser("show",
        help="emit the current config as JSON")
    sub_show.add_argument("--json", action="store_true",
        help="emit JSON (the only output format today)")
    sub_show.add_argument("--defaults", action="store_true",
        help="merge with DEFAULTS so every key is present")
    if common:
        common(sub_show)

    sub_apply = cfg_sub.add_parser("apply",
        help="apply a JSON dict from stdin to the config file")
    sub_apply.add_argument("--json", action="store_true",
        help="read JSON from stdin (the only input format today)")
    sub_apply.add_argument("path", nargs="?", default="-",
        help="path to JSON file, or '-' for stdin (default)")
    if common:
        common(sub_apply)
