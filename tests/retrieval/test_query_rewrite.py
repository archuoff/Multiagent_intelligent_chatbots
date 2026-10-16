import unittest

from backend.retrieval.query_rewrite import rewrite_query


class RewriteQueryTests(unittest.TestCase):
    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(rewrite_query("  torque   spec  for X \n"), "torque spec for X")

    def test_caps_length(self):
        self.assertEqual(len(rewrite_query("a" * 600)), 500)

    def test_leaves_normal_query_unchanged(self):
        self.assertEqual(rewrite_query("what is the torque spec for part X"), "what is the torque spec for part X")


if __name__ == "__main__":
    unittest.main()
