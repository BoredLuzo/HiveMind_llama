import unittest

from main import compute
from mathx import square


class T(unittest.TestCase):
    def test_square(self):
        self.assertEqual(square(4), 16)

    def test_compute_uses_square(self):
        self.assertEqual(compute(5), 25)

    def test_compute_source_wired(self):
        import inspect

        import main
        self.assertIn("square", inspect.getsource(main.compute))
