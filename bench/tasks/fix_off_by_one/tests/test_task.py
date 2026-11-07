import unittest

from range_sum import count_items


class T(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(count_items([]), 0)

    def test_one(self):
        self.assertEqual(count_items(["a"]), 1)

    def test_three(self):
        self.assertEqual(count_items([1, 2, 3]), 3)
