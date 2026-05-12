"""CONTRACT v0.6 §14 — `config init` / `config edit` interview.

Minimal: `init` writes the template to ``/etc/mag-recorder/...`` with
env-bag defaults filled in (`STATION_*`, `MAG_RECORDER_STATION_ID`);
`edit` shows current values and lets the user override them by
re-running with a non-interactive flag.  No prompts in
``--non-interactive`` mode so sigmond can drive the interview from a
shell session.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from mag_recorder.config import DEFAULT_CONFIG_PATH

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "config" \
    / "mag-recorder-config.toml.template"


_ENV_SUBSTITUTIONS: dict[str, str] = {
    "<YOUR_PSWS_STATION_ID>": "MAG_RECORDER_STATION_ID",
    "<YOUR_CALL>":            "STATION_CALLSIGN",
    "<YOUR_GRID>":            "STATION_GRID",
}


def _resolve_template_path() -> Path:
    """Locate the bundled template.

    Tries package-relative first (development checkout), then falls
    back to /opt/mag-recorder/share/.  Sigmond's `[[install.steps]]
    kind = "render"` copies the template into /etc; this function is
    the source of truth for "where is the original template?".
    """
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


def cmd_config_init(args) -> int:
    """Write a fresh mag-recorder-config.toml from the template."""
    dst = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    if dst.exists() and not getattr(args, "reconfig", False):
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


def cmd_config_edit(args) -> int:
    """Show the current config and which fields still need attention."""
    src = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    if not src.exists():
        print(f"no config found at {src}; run `mag-recorder config init` first")
        return 1

    text = src.read_text(encoding="utf-8")
    print(f"config: {src}\n")
    print(text)

    # Quick scan for unfilled placeholders, mirrors the
    # contract.py `_collect_issues` checks.
    placeholders = [p for p in _ENV_SUBSTITUTIONS if p in text]
    if placeholders:
        print("\n--- still placeholder ---")
        for p in placeholders:
            env_key = _ENV_SUBSTITUTIONS[p]
            print(f"  {p}   (set env {env_key} or edit the file directly)")
        return 1
    return 0
