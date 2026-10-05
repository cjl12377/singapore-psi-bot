import unittest

import tests  # noqa: F401  (sets the environment)
from location import AREA_REGION, _AREAS, locate


class LocateTest(unittest.TestCase):
    def test_known_places(self):
        cases = [
            ((1.3236, 103.9273), ("Bedok", "east")),
            ((1.3404, 103.7090), ("Jurong West", "west")),
            ((1.4360, 103.7860), ("Woodlands", "north")),
            ((1.3510, 103.8485), ("Bishan", "central")),            # Bishan MRT
            ((1.3521, 103.8198), ("Central Water Catchment", "north")),  # island centre
            ((1.2839, 103.8515), ("Downtown Core", "south")),
        ]
        for (lat, lon), expected in cases:
            self.assertEqual(locate(lat, lon), expected, (lat, lon))

    def test_outside_bounding_box(self):
        self.assertIsNone(locate(1.4927, 103.7414))  # Johor Bahru
        self.assertIsNone(locate(51.5, -0.12))       # London

    def test_open_sea_inside_box_is_rejected(self):
        self.assertIsNone(locate(1.15, 104.10))

    def test_every_area_has_a_region(self):
        regions = {"central", "north", "south", "east", "west"}
        for area in _AREAS:
            self.assertIn(AREA_REGION.get(area["name"]), regions, area["name"])


if __name__ == "__main__":
    unittest.main()
