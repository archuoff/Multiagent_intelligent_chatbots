"""Tests for the high-precision noise/plausibility heuristic.

Precision matters more than recall here: the dangerous failure mode is
over-rejecting real technical content, so every "this is flagged" case has
a paired "this legitimate lookalike is not" case, and the legitimate list is
deliberately the larger of the two.
"""

import unittest

from backend.ingestion.extraction.text_plausibility import is_probably_noise, noise_signals


class LegitimateTechnicalTextTests(unittest.TestCase):
    """None of these real-world-shaped strings should ever be flagged."""

    LEGITIMATE_EXAMPLES = [
        "Volume resistivity 1.0 × 10^12 Ω·cm at 23 °C",
        "Torque: 12 ± 1 Nm (M6 × 1.0)",
        "Part LR-084-7721 / Rev. C",
        "PA6-GF30 — UL94 V-0",
        "Contents ..................................... Page 5",
        "ENGINEERING SPECIFICATION SHEET",
        "Nm | °C | kg/m³ | W/m·K",
        "Surface finish grade: Ra 1.6 µm",
        "Tolerance ±0.05 mm on all dimensions unless noted",
        "Section 4.2.1 — Fastener torque specifications",
    ]

    def test_legitimate_technical_text_is_not_flagged(self):
        for text in self.LEGITIMATE_EXAMPLES:
            with self.subTest(text=text):
                self.assertFalse(is_probably_noise(text), f"False positive on: {text!r} -> {noise_signals(text)}")

    def test_empty_and_whitespace_only_text_is_not_flagged(self):
        self.assertFalse(is_probably_noise(""))
        self.assertFalse(is_probably_noise("   \n\t  "))


class KnownCorruptionModeTests(unittest.TestCase):
    """Each of these is a real PDF-extraction failure pattern and must be caught."""

    def test_cid_glyph_artifacts_are_flagged(self):
        self.assertIn("cid_artifact", noise_signals("Volume (cid:12)(cid:87) resistivity"))

    def test_replacement_characters_are_flagged(self):
        self.assertIn("replacement_or_control_char", noise_signals("Surface finish ��� grade"))

    def test_control_characters_are_flagged(self):
        self.assertIn("replacement_or_control_char", noise_signals("Torque spec\x01\x02\x03 value"))

    def test_vowelless_run_is_flagged(self):
        self.assertIn("vowelless_run", noise_signals("The value is xkrqzmnpvt today"))

    def test_letter_spaced_heading_needs_a_second_signal_to_flag(self):
        """A tracked-out heading is a real but non-corrupting artifact --
        single-character tokens alone (a soft signal) shouldn't flag it."""
        spaced = "J A G U A R"
        self.assertFalse(is_probably_noise(spaced))

    def test_letter_spacing_combined_with_another_soft_signal_is_flagged(self):
        spaced_and_symbolic = "J A G U A R # $ % ^ & * ( ) < > ~ ` | \\ { } [ ]"
        self.assertTrue(is_probably_noise(spaced_and_symbolic))

    def test_repeated_character_run_is_flagged(self):
        self.assertIn("repeated_character_run", noise_signals("Value: 90#####################C"))

    def test_dot_leader_repeated_dots_are_not_flagged(self):
        self.assertNotIn("repeated_character_run", noise_signals("Torque spec .................... 12 Nm"))

    def test_mixed_script_token_alone_is_not_enough_to_flag(self):
        """A single soft signal alone (mixed script) is intentionally not
        sufficient on its own -- it needs a second soft signal, same as the
        letter-spacing case above."""
        self.assertFalse(is_probably_noise("Material grade абвsteel type"))

    def test_mixed_script_combined_with_another_soft_signal_is_flagged(self):
        symbolic_and_mixed_script = "абвsteel # $ % ^ & * ( ) < > ~ ` | \\ { } [ ]"
        self.assertIn("mixed_script_token", noise_signals(symbolic_and_mixed_script))
        self.assertTrue(is_probably_noise(symbolic_and_mixed_script))


if __name__ == "__main__":
    unittest.main()
