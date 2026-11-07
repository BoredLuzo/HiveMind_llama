import unittest

from words import first_word


class T(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(first_word(""), "")

    def test_whitespace(self):
        self.assertEqual(first_word("   "), "")

    def test_normal(self):
        self.assertEqual(first_word(" hello world "), "hello")
