"""The shipped config template must not contradict the code defaults.

⛔ WHY.  supervisor.py has always used "0.0.0.0" when [websocket].bind_address
is absent, and the template shipped "127.0.0.1" -- so every station installed
from the template bound the live feed to loopback, and nothing said so.

On a sigmond station nothing that watches the feed runs in the decoder VM: the
operator's browser is on their laptop and reaches the dashboard through the
Proxmox host.  Bound to loopback the magnetometer page loads and then reports
"disconnected" for ever, on a station whose sensor is recording perfectly at
1 Hz -- and no URL the operator can type fixes it, because there is no address
a browser can reach.  rob hit exactly that on AI6VN, 2026-09-30.

A template that silently overrides a code default is worse than no template.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / 'config' / 'mag-recorder-config.toml.template'


def _template_value(key: str) -> str:
    for line in TEMPLATE.read_text().splitlines():
        bare = line.split('#', 1)[0].strip()
        if bare.startswith(key):
            return bare.split('=', 1)[1].strip().strip('"')
    raise AssertionError(f'{key} not found in {TEMPLATE.name}')


def test_websocket_binds_reachably_by_default():
    assert _template_value('bind_address') == '0.0.0.0', (
        'loopback makes the live feed unreachable from any browser; '
        'the dashboard then reports "disconnected" on a healthy station'
    )


def test_template_agrees_with_the_code_default():
    """The two must not drift: that drift is the whole bug."""
    from mag_recorder.core import supervisor
    src = inspect.getsource(supervisor)
    m = re.search(r'ws\.get\(\s*"bind_address"\s*,\s*"([^"]+)"\s*\)', src)
    assert m, 'could not find the code default for bind_address'
    assert m.group(1) == _template_value('bind_address'), (
        f'code defaults to {m.group(1)!r} but the template ships '
        f'{_template_value("bind_address")!r}'
    )


def test_the_feed_is_still_enabled_by_default():
    # Binding wider is pointless if the server never starts.
    assert _template_value('enable') == 'true'
