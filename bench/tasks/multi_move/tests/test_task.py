import unittest

from slugs import slugify as new_slugify
from text_utils import slugify as old_slugify


class T(unittest.TestCase):
    def test_new_module(self):
        self.assertEqual(new_slugify("Hello World"), "hello-world")

    def test_old_import(self):
        self.assertEqual(old_slugify("Hello World"), "hello-world")

    def test_same_callable(self):
        self.assertIs(old_slugify, new_slugify)
