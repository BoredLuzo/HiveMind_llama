import math
import unittest

from report import total_area
from shapes import circle_area, rectangle_area


class T(unittest.TestCase):
    def test_rectangle(self):
        self.assertEqual(rectangle_area(2, 3), 6)

    def test_mixed(self):
        shapes = [{"type": "circle", "r": 1}, {"type": "rect", "w": 2, "h": 3}]
        self.assertAlmostEqual(total_area(shapes), math.pi + 6)

    def test_circle_unchanged(self):
        self.assertAlmostEqual(circle_area(2), 4 * math.pi)
