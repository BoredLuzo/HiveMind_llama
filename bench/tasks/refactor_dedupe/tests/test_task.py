import unittest

from validators import _validate_text, validate_city, validate_name


class T(unittest.TestCase):
    def test_helper(self):
        self.assertEqual(_validate_text("  x  "), "x")

    def test_both_validators(self):
        self.assertEqual(validate_name("  amy "), "amy")
        self.assertEqual(validate_city(" berlin"), "berlin")

    def test_invalid_both(self):
        for fn in (validate_name, validate_city):
            with self.assertRaises(ValueError):
                fn("   ")
