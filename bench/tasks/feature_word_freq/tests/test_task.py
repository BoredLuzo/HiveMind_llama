import unittest

from freq import word_freq


class T(unittest.TestCase):
    def test_counts_and_case(self):
        self.assertEqual(word_freq("Hello, hello! World."),
                         {"hello": 2, "world": 1})

    def test_empty(self):
        self.assertEqual(word_freq(""), {})

    def test_numbers(self):
        self.assertEqual(word_freq("a1 a1 b"), {"a1": 2, "b": 1})
