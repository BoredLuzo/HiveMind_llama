import unittest

from limits import clamp


class T(unittest.TestCase):
    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below(self):
        self.assertEqual(clamp(-1, 0, 10), 0)

    def test_above(self):
        self.assertEqual(clamp(11, 0, 10), 10)

    def test_bad_bounds(self):
        with self.assertRaises(ValueError):
            clamp(1, 5, 1)
