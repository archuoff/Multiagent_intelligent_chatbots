"""Regression tests for the native-text audit baseline used by corpus validation."""

import unittest
import xml.etree.ElementTree as ET

from tools.audit_extraction import xml_text


class AuditMetricsTests(unittest.TestCase):
    """Ensures Office formatting does not manufacture missing-token findings."""

    def test_formatting_runs_preserve_words(self):
        """Adjacent runs form a single word and separate paragraphs stay separate."""
        root = ET.fromstring('<root xmlns:a="urn:example"><a:p><a:r><a:t>R</a:t></a:r><a:r><a:t>epresentation</a:t></a:r></a:p><a:p><a:r><a:t>Next</a:t></a:r></a:p></root>')
        self.assertEqual(xml_text(root), "Representation\nNext")

    def test_explicit_breaks_preserve_word_boundaries(self):
        """Line breaks within paragraphs must not concatenate distinct words."""
        root = ET.fromstring('<root xmlns:w="urn:example"><w:p><w:r><w:t>Sensor</w:t><w:br/><w:t>mount</w:t></w:r></w:p></root>')
        self.assertEqual(xml_text(root), "Sensor mount")
