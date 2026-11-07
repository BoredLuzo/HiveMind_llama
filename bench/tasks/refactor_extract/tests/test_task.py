import unittest

from calc_even import is_even, process


class T(unittest.TestCase):
    def test_is_even(self):
        self.assertTrue(is_even(4))
        self.assertFalse(is_even(5))

    def test_process_unchanged(self):
        self.assertEqual(process([1, 2, 3, 4]), [2, 4])

    def test_process_empty(self):
        self.assertEqual(process([]), [])
