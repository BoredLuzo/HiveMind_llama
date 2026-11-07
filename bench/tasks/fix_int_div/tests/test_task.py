import unittest

from stats import average


class T(unittest.TestCase):
    def test_even(self):
        self.assertEqual(average(2, 4), 3.0)

    def test_fraction(self):
        self.assertEqual(average(1, 2), 1.5)

    def test_negative(self):
        self.assertEqual(average(-1, 0), -0.5)
