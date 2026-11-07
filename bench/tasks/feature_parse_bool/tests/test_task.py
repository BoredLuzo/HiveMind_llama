import unittest

from bools import parse_bool


class T(unittest.TestCase):
    def test_true_values(self):
        for value in ("true", "TRUE", "yes", "1", 1, True):
            self.assertIs(parse_bool(value), True, repr(value))

    def test_false_values(self):
        for value in ("false", "No", "0", 0, False):
            self.assertIs(parse_bool(value), False, repr(value))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_bool("maybe")
