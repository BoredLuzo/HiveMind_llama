import unittest

from people import sort_people


class T(unittest.TestCase):
    def test_age_then_name(self):
        rows = [
            {"name": "zoe", "age": 30},
            {"name": "amy", "age": 30},
            {"name": "bob", "age": 20},
        ]
        self.assertEqual([p["name"] for p in sort_people(rows)],
                         ["bob", "amy", "zoe"])

    def test_empty(self):
        self.assertEqual(sort_people([]), [])
