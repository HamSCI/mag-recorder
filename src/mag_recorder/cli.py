"""mag-recorder CLI entry point.

Subcommands:
    inventory   --json   CONTRACT v0.6 inventory
    validate    --json   CONTRACT v0.6 validation
    version     --json   version + git info
    config init|edit     §14 configuration interview
    daemon               long-running supervisor (with --simulate)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path


def _resolve_log_level() -> int:
    """§11 precedence: MAG_RECORDER_LOG_LEVEL -> CLIENT_LOG_LEVEL -> INFO."""
    for env_key in ("MAG_RECORDER_LOG_LEVEL", "CLIENT_LOG_LEVEL"):
        val = os.environ.get(env_key, "").upper().strip()
        if val and hasattr(logging, val):
            return getattr(logging, val)
    return logging.INFO


def _install_sighup_handler() -> None:
    """§11: re-read log level from env on SIGHUP."""
    def _on_sighup(signum, frame):
        level = _resolve_log_level()
        logging.getLogger().setLevel(level)
        logging.getLogger(__name__).info(
            "SIGHUP: log level set to %s", logging.getLevelName(level)
        )
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_sighup)


def _add_common(sub):
    sub.add_argument("--config", type=Path, default=None,
                     help="path to mag-recorder-config.toml")
    sub.add_argument("--log-level", default=None,
                     help="override log level (DEBUG/INFO/WARNING/ERROR)")


def main():
    contract_quiet = any(
        arg in ("inventory", "validate", "version") for arg in sys.argv[1:3]
    )

    root = logging.getLogger()
    root.setLevel(logging.WARNING if contract_quiet else _resolve_log_level())
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(levelname)s:%(name)s:%(message)s")
        )
        root.addHandler(handler)

    parser = argparse.ArgumentParser(
        prog="mag-recorder",
        description="RM3100 magnetometer recorder + PSWS uploader (sigmond client)",
    )
    sub = parser.add_subparsers(dest="command", help="command to run")

    sub_inv = sub.add_parser("inventory", help="CONTRACT v0.6 inventory")
    sub_inv.add_argument("--json", action="store_true", default=True)
    _add_common(sub_inv)

    sub_val = sub.add_parser("validate", help="CONTRACT v0.6 validation")
    sub_val.add_argument("--json", action="store_true", default=True)
    _add_common(sub_val)

    sub_ver = sub.add_parser("version", help="version info")
    sub_ver.add_argument("--json", action="store_true", default=True)
    _add_common(sub_ver)

    sub_dae = sub.add_parser("daemon", help="run recorder daemon")
    sub_dae.add_argument("--simulate", action="store_true",
                         help="use the simulator instead of mag-usb (no hardware needed)")
    _add_common(sub_dae)

    sub_cfg = sub.add_parser("config", help="initialize or edit configuration")
    cfg_sub = sub_cfg.add_subparsers(dest="config_command")

    sub_init = cfg_sub.add_parser("init", help="write a fresh config from template")
    sub_init.add_argument("--reconfig", action="store_true",
                          help="overwrite existing config")
    sub_init.add_argument("--non-interactive", action="store_true",
                          help="use env-var defaults, do not prompt")
    _add_common(sub_init)

    sub_edit = cfg_sub.add_parser("edit", help="review the current config")
    sub_edit.add_argument("--non-interactive", action="store_true",
                          help="show current values, do not prompt")
    _add_common(sub_edit)

    args = parser.parse_args()
    if args.log_level and not contract_quiet:
        level_name = args.log_level.upper()
        if hasattr(logging, level_name):
            root.setLevel(getattr(logging, level_name))

    handlers = {
        "inventory": _handle_inventory,
        "validate":  _handle_validate,
        "version":   _handle_version,
        "daemon":    _handle_daemon,
        "config":    _handle_config,
    }
    fn = handlers.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)


def _handle_config(args):
    from mag_recorder import configurator
    sub = getattr(args, "config_command", None)
    if sub == "init":
        sys.exit(configurator.cmd_config_init(args))
    if sub == "edit":
        sys.exit(configurator.cmd_config_edit(args))
    print("usage: mag-recorder config {init|edit} [--non-interactive]")
    sys.exit(2)


def _handle_inventory(args):
    from mag_recorder.config import DEFAULT_CONFIG_PATH, load_config
    from mag_recorder.contract import build_inventory, CONTRACT_VERSION

    config_path = args.config or Path(
        os.environ.get("MAG_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        payload = {
            "client":           "mag-recorder",
            "version":          "0.1.0",
            "contract_version": CONTRACT_VERSION,
            "config_path":      str(config_path),
            "instances":        [],
            "issues": [{
                "severity": "fail",
                "instance": "all",
                "message":  f"config not found: {config_path}",
            }],
        }
        print(json.dumps(payload, indent=2))
        return

    print(json.dumps(build_inventory(config, config_path), indent=2))


def _handle_validate(args):
    from mag_recorder.config import DEFAULT_CONFIG_PATH, load_config
    from mag_recorder.contract import build_validate

    config_path = args.config or Path(
        os.environ.get("MAG_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        payload = {
            "ok":          False,
            "config_path": str(config_path),
            "issues": [{
                "severity": "fail",
                "instance": "all",
                "message":  f"config not found: {config_path}",
            }],
        }
        print(json.dumps(payload, indent=2))
        sys.exit(1)
        return

    payload = build_validate(config, config_path)
    print(json.dumps(payload, indent=2))
    if not payload["ok"]:
        sys.exit(1)


def _handle_version(args):
    from mag_recorder import __version__
    from mag_recorder.version import GIT_INFO

    payload = {"client": "mag-recorder", "version": __version__}
    if GIT_INFO:
        payload["git"] = GIT_INFO
    print(json.dumps(payload, indent=2))


def _handle_daemon(args):
    _install_sighup_handler()
    logger = logging.getLogger("mag_recorder.daemon")

    from mag_recorder.config import DEFAULT_CONFIG_PATH, load_config
    from mag_recorder.core.supervisor import (
        SupervisorConfig, run_supervisor, make_source,
    )

    config_path = args.config or Path(
        os.environ.get("MAG_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    config = load_config(config_path)

    force_sim = args.simulate or \
        os.environ.get("MAG_RECORDER_SIMULATE", "").lower() in ("1", "true", "yes")
    logger.info("starting mag-recorder daemon (config=%s, simulate=%s)",
                config_path, force_sim)

    stop_event = threading.Event()
    def _on_stop(signum, frame):
        logger.info("received signal %d; shutting down", signum)
        stop_event.set()
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT,  _on_stop)

    # sd_notify ping when available -- swallow ImportError so the
    # daemon still runs in development outside systemd.
    watchdog_ping = None
    notify_ready = None
    try:
        from systemd import daemon as sd  # type: ignore[import-not-found]
        watchdog_ping = lambda: sd.notify("WATCHDOG=1")  # noqa: E731
        notify_ready  = lambda: sd.notify("READY=1")     # noqa: E731
    except ImportError:
        logger.debug("python-systemd not installed; running without sd_notify")

    source = make_source(config, force_simulate=force_sim)
    sup_cfg = SupervisorConfig(
        spool_dir     = Path(config["paths"]["spool_dir"]),
        source        = source,
        watchdog_ping = watchdog_ping,
    )

    if notify_ready is not None:
        notify_ready()
    run_supervisor(sup_cfg, stop_event=stop_event)


if __name__ == "__main__":
    main()
