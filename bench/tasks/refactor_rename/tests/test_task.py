import unittest

from pricing import calc_total, total_price


class T(unittest.TestCase):
    def test_new_name(self):
        self.assertEqual(total_price([1, 2, 3]), 6)

    def test_old_name_still_works(self):
        self.assertEqual(calc_total([2, 2]), 4)

    def test_empty(self):
        self.assertEqual(total_price([]), 0)
