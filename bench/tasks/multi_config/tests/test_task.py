import os
import unittest

from config import mode
from service import describe


class T(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("APP_MODE", None)

    def test_env_value(self):
        os.environ["APP_MODE"] = "dev"
        self.assertEqual(mode(), "dev")
        self.assertEqual(describe(), "mode=dev")

    def test_default(self):
        os.environ.pop("APP_MODE", None)
        self.assertEqual(mode(), "prod")

    def test_backward_constant(self):
        from config import MODE
        self.assertEqual(MODE, "prod")
