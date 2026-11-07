import os
import unittest

from paths import join_path


class T(unittest.TestCase):
    def test_matches_os_join(self):
        self.assertEqual(join_path("data", "x.txt"), os.path.join("data", "x.txt"))

    def test_trailing_separator(self):
        self.assertEqual(join_path("data/", "x.txt"), os.path.join("data/", "x.txt"))

    def test_nested(self):
        self.assertEqual(join_path("a", "b/c.txt"), os.path.join("a", "b/c.txt"))
