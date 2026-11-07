import unittest

from nested import flatten


class T(unittest.TestCase):
    def test_deep(self):
        self.assertEqual(flatten([1, [2, [3]], 4]), [1, 2, 3, 4])

    def test_empty(self):
        self.assertEqual(flatten([]), [])

    def test_empty_inner(self):
        self.assertEqual(flatten([[], [1], []]), [1])
