import unittest

from jsonio import to_json


class T(unittest.TestCase):
    def test_sorted_keys(self):
        self.assertEqual(to_json({"b": 1, "a": 2}), '{"a": 2, "b": 1}')

    def test_nested(self):
        self.assertEqual(to_json({"x": [1, 2]}), '{"x": [1, 2]}')
