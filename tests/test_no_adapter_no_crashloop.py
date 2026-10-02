"""A station with no magnetometer must not crash-loop the unit.

mag-usb exits in about a second when the Pololu adapter is absent:

    I2C adapter device '/dev/ttyMAG0' not available (error -2)

With `Restart=always` and nothing guarding the start, systemd burns all ten of
`StartLimitBurst` and parks the unit in start-limit-hit.  Measured on WB6CXC-7
2026-10-02 -- boot 16:27:21, `Active: failed (Result: start-limit-hit)` by
16:28:35, 74 seconds -- and W3USR-06 was in the identical state, so this is a
class defect across dasi2 stations built without the sensor, not one station's.

sigmond already knows these stations have no magnetometer: catalog.toml
declares mag-recorder `hardware_gated` and harmonize.dormant_reason stops
`smd start` from starting it.  But that gate guards the smd VERB and systemd
never consults it -- the unit is WantedBy=multi-user.target with
UnitFilePreset=enabled.  The guard has to exist on systemd's side too.

⚠ THE CONDITION AND THE UDEV RULE ARE ONE FIX, NOT TWO.  A condition is
evaluated only when something tries to start the unit, so the condition alone
would mean "plug the sensor in, then reboot".  SYSTEMD_WANTS in the udev rule
is what makes attaching the adapter start the recorder.  Tested together,
because either one alone is a regression.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT = REPO / "systemd" / "mag-recorder.service"
RULES = REPO / "install" / "99-PololuI2C.rules"

DEVICE = "/dev/ttyMAG0"


def _directives(path, section):
    """Directive lines of one ini section, comments and blanks stripped."""
    out, cur = [], None
    for line in path.read_text().splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s
            continue
        if not s or s.startswith("#"):
            continue
        if cur == section:
            out.append(s)
    return out


class NoAdapterNoCrashLoop(unittest.TestCase):

    def test_the_unit_refuses_to_start_without_the_adapter(self):
        unit = [d for d in _directives(UNIT, "[Unit]")
                if d.startswith("ConditionPathExists=")]

        self.assertEqual(unit, [f"ConditionPathExists={DEVICE}"],
                         "without this the unit crash-loops to start-limit-hit "
                         "on every station that has no magnetometer")

    def test_the_condition_names_the_same_path_the_udev_rule_creates(self):
        """If these two ever drift, the unit silently never starts -- which
        looks exactly like the hardware being broken."""
        symlinks = set(re.findall(r'SYMLINK\+="([^"]+)"', RULES.read_text()))
        cond = [d.split("=", 1)[1] for d in _directives(UNIT, "[Unit]")
                if d.startswith("ConditionPathExists=")]

        self.assertTrue(cond, "no ConditionPathExists to check")
        self.assertEqual({Path(cond[0]).name}, symlinks,
                         f"unit waits for {cond[0]} but udev creates {symlinks}")

    def test_attaching_the_adapter_starts_the_recorder(self):
        """The other half.  Without SYSTEMD_WANTS the condition turns
        'plug the sensor in' into 'plug the sensor in, then reboot'."""
        text = RULES.read_text()
        rules = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]

        self.assertTrue(rules, "no udev rules found")
        for ln in rules:
            self.assertIn('TAG+="systemd"', ln,
                          f"rule not tagged for systemd: {ln}")
            self.assertIn("mag-recorder.service", ln,
                          f"rule does not start the recorder on attach: {ln}")

    def test_both_adapter_variants_are_still_covered(self):
        """The null: Pololu ships 5396 (2502) and 5397 (2503), and AI6VN's
        working adapter is one of them.  A fix that covered only one variant
        would pass every test above while breaking a live station."""
        text = RULES.read_text()
        for pid in ("2502", "2503"):
            self.assertIn(f'ATTRS{{idProduct}}=="{pid}"', text,
                          f"adapter variant {pid} lost")

    def test_restart_prevent_exit_status_is_unchanged(self):
        """Guard against a tempting over-fix.  It is NOT enough to make the
        daemon exit 78 on a missing device: a USB glitch mid-run would then
        park the unit permanently on a station that DOES have a sensor.  The
        condition plus udev handles the real case without that risk, so 78
        stays reserved for genuine config faults."""
        svc = _directives(UNIT, "[Service]")

        self.assertIn("RestartPreventExitStatus=78", svc)
        self.assertIn("Restart=always", svc)


if __name__ == "__main__":
    unittest.main()
