import unittest
from graph import CycleError, topological_sort


class TestGraph(unittest.TestCase):
    def test_simple_dag(self):
        g = {"A": [], "B": ["A"], "C": ["B"]}
        order = topological_sort(g)
        self.assertLess(order.index("A"), order.index("B"))
        self.assertLess(order.index("B"), order.index("C"))

    def test_diamond_dag(self):
        g = {"D": ["B", "C"], "B": ["A"], "C": ["A"], "A": []}
        order = topological_sort(g)
        self.assertLess(order.index("A"), order.index("B"))
        self.assertLess(order.index("A"), order.index("C"))
        self.assertLess(order.index("B"), order.index("D"))
        self.assertLess(order.index("C"), order.index("D"))

    def test_independent_components(self):
        g = {"A": [], "B": ["A"], "X": [], "Y": ["X"]}
        order = topological_sort(g)
        self.assertLess(order.index("A"), order.index("B"))
        self.assertLess(order.index("X"), order.index("Y"))
        self.assertEqual(len(order), 4)

    def test_self_cycle(self):
        g = {"A": ["A"]}
        with self.assertRaises(CycleError) as ctx:
            topological_sort(g)
        cycle = ctx.exception.args[0]
        self.assertEqual(cycle, ["A", "A"])

    def test_three_node_cycle(self):
        g = {"A": ["C"], "B": ["A"], "C": ["B"], "D": ["A"]}
        with self.assertRaises(CycleError) as ctx:
            topological_sort(g)
        cycle = ctx.exception.args[0]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertGreaterEqual(len(cycle), 3)
