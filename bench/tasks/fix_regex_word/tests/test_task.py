import unittest

from words2 import contains_word


class T(unittest.TestCase):
    def test_whole_word(self):
        self.assertTrue(contains_word("a cat sat", "cat"))

    def test_substring_is_not_a_word(self):
        self.assertFalse(contains_word("concatenate", "cat"))

    def test_case_insensitive(self):
        self.assertTrue(contains_word("A CAT SAT", "cat"))
