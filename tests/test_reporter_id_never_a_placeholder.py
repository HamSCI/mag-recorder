"""A placeholder is not an identity — never stamp one into a sample.

extract_reporter_id() tested its candidates for bare truthiness:

    sid = station.get("psws_station_id")
    if isinstance(sid, str) and sid:
        return sid

"<YOUR_PSWS_STATION_ID>" is a non-empty string, so it passed, and
supervisor.py then wrote it into every sample:

    if cfg.reporter_id:
        restamped["reporter_id"] = cfg.reporter_id

Found on AI6VN 2026-10-03 -- 62456 of 62456 lines in that day's spool, across
three days of files, on the only station in the fleet with a working
magnetometer:

    {"ts":"2026-10-03T17:20:25.768Z","rt":25.75,"x":-16898.086,
     "y":-6543.919,"z":41768.131,"reporter_id":"<YOUR_PSWS_STATION_ID>"}

Untagged is correct here and is exactly what extract_reporter_id's own
docstring already promised as its third resolution step. A fake tag is worse
than no tag: the measurements are the part that cannot be backfilled, and a
reporter_id CAN be filled in later -- but only if nothing fabricated one over
it first. That is the same principle this repo already argues for recording
itself (cli.py, issue #8): identity gates DELIVERY, not COLLECTION.

is_placeholder() -- the suite's one spelling for "the operator has not
answered this yet" -- was already in config.py and already used by
upload_blockers(). extract_reporter_id simply never called it.
"""
import unittest

from mag_recorder.config import extract_reporter_id, is_placeholder


class ReporterIdIsNeverAPlaceholder(unittest.TestCase):

    def test_the_AI6VN_case(self):
        """The exact config that produced 62456 mis-stamped samples."""
        cfg = {"station": {"psws_station_id": "<YOUR_PSWS_STATION_ID>"}}

        self.assertIsNone(extract_reporter_id(cfg),
                          "a template placeholder was returned as an identity")

    def test_a_placeholder_instance_id_falls_through_to_the_station(self):
        cfg = {"instance": {"reporter_id": "<YOUR_REPORTER_ID>"},
               "station": {"psws_station_id": "S000170"}}

        self.assertEqual(extract_reporter_id(cfg), "S000170")

    def test_both_placeholders_yield_untagged(self):
        cfg = {"instance": {"reporter_id": "<YOUR_REPORTER_ID>"},
               "station": {"psws_station_id": "<YOUR_PSWS_STATION_ID>"}}

        self.assertIsNone(extract_reporter_id(cfg))

    # --- the nulls: a real identity must still come through --------------

    def test_a_real_station_id_is_returned(self):
        cfg = {"station": {"psws_station_id": "S000170"}}

        self.assertEqual(extract_reporter_id(cfg), "S000170")

    def test_an_explicit_instance_id_still_wins(self):
        """Resolution order must be unchanged for configured hosts."""
        cfg = {"instance": {"reporter_id": "AC0G/B4"},
               "station": {"psws_station_id": "S000170"}}

        self.assertEqual(extract_reporter_id(cfg), "AC0G/B4")

    def test_a_callsign_containing_brackets_is_an_ANSWER_not_a_template(self):
        """is_placeholder requires the value to be ENTIRELY bracketed. A
        station that legitimately reports as 'AC0G <portable>' must not be
        silently untagged by this fix -- that would trade one data-labelling
        bug for another."""
        cfg = {"station": {"psws_station_id": "AC0G <portable>"}}

        self.assertEqual(extract_reporter_id(cfg), "AC0G <portable>")
        self.assertFalse(is_placeholder("AC0G <portable>"))

    def test_empty_and_missing_are_untagged(self):
        self.assertIsNone(extract_reporter_id({"station": {"psws_station_id": ""}}))
        self.assertIsNone(extract_reporter_id({}))


if __name__ == "__main__":
    unittest.main()
