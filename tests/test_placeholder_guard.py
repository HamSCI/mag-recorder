"""EX_CONFIG (78) idle-unconfigured guard — placeholder detection."""

import unittest

from mag_recorder.config import unconfigured_placeholders


class TestUnconfiguredPlaceholders(unittest.TestCase):

    def test_template_values_detected(self):
        config = {"station": {
            "psws_station_id": "<YOUR_PSWS_STATION_ID>",
            "callsign":        "<YOUR_CALL>",
            "grid_square":     "<YOUR_GRID>",
        }}
        stale = unconfigured_placeholders(config)
        self.assertEqual(len(stale), 3)
        self.assertTrue(any("psws_station_id" in s for s in stale))

    def test_configured_station_passes(self):
        config = {"station": {
            "psws_station_id": "S000418",
            "callsign":        "AC0G",
            "grid_square":     "EM38ww",
        }}
        self.assertEqual(unconfigured_placeholders(config), [])

    def test_partial_configuration_flags_remainder(self):
        config = {"station": {
            "psws_station_id": "S000418",
            "callsign":        "<YOUR_CALL>",
            "grid_square":     "EM38ww",
        }}
        stale = unconfigured_placeholders(config)
        self.assertEqual(stale, ["station.callsign=<YOUR_CALL>"])

    def test_missing_station_block_is_not_placeholder(self):
        # Empty/missing values are a different failure mode (validate
        # reports them); the 78 guard fires only on template markers.
        self.assertEqual(unconfigured_placeholders({}), [])
