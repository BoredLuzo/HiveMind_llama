import unittest

from chunks import chunk


class T(unittest.TestCase):
    def test_even(self):
        self.assertEqual(chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]])

    def test_remainder(self):
        self.assertEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_empty(self):
        self.assertEqual(chunk([], 3), [])

    def test_bad_size(self):
        with self.assertRaises(ValueError):
            chunk([1], 0)
