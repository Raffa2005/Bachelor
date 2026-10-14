import unittest

from tools.smoke_inference import (
    _check_fallback_signature,
    _policy_value_signature,
)


class FallbackSmokeTest(unittest.TestCase):
    def test_reads_root_value_and_both_policy_branches(self):
        block = [
            "info string e2e4 (34) N: 0 (P: 25.10%) (Q: 0.12)",
            "info string d2d4+[AB] (85) N: 0 (P: 74.90%) (Q: 0.12)",
            "info string node (2) N: 1 (Q: -0.12345)",
        ]
        self.assertEqual(_policy_value_signature(block),
                         (-0.12345, {"e2e4": 25.10, "d2d4+": 74.90}))

    def test_requires_network_outputs(self):
        with self.assertRaises(RuntimeError):
            _policy_value_signature(["bestmove e2e4"])

    def test_accepts_display_rounding(self):
        _check_fallback_signature((0.1, {"e2e4": 50.0}),
                                  (0.10001, {"e2e4": 50.01}))

    def test_rejects_mismatched_value_or_policy(self):
        paired = (0.1, {"e2e4": 50.0, "d2d4": 50.0})
        for standalone in ((0.2, paired[1]),
                           (0.1, {"e2e4": 80.0, "d2d4": 20.0}),
                           (0.1, {"e2e4": 50.0})):
            with self.subTest(standalone=standalone), self.assertRaises(RuntimeError):
                _check_fallback_signature(paired, standalone)


if __name__ == "__main__":
    unittest.main()
