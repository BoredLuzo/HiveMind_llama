import unittest

from tags import add_tag


class T(unittest.TestCase):
    def test_calls_are_independent(self):
        self.assertEqual(add_tag("x"), ["x"])
        self.assertEqual(add_tag("y"), ["y"])

    def test_explicit_list_is_used(self):
        self.assertEqual(add_tag("z", ["a"]), ["a", "z"])
