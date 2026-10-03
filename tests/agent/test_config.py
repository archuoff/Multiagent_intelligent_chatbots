"""Regression tests for the agent config loader fix (Phase 0).

Covers the confirmed bug: nodes.py used to call
load_all_configs("agents/configs"), a path that never exists, silently
falling back to a stale, mismatched-agent-id file. These tests pin the
correct behavior: read the real config/agents/*.yaml registry, and fail
loudly instead of silently on a bad path.
"""

import unittest
from pathlib import Path

from backend.agent.config import AgentConfig, get_agent_config, load_all_configs

REAL_AGENT_IDS = {
    "adas-agent", "le-agent", "material-net-agent",
    "eds-agent", "benchmarking-agent", "supplier-agent",
}


class LoadAllConfigsTests(unittest.TestCase):
    """Checks the loader reads the real, live config/agents/ registry by default."""

    def test_default_directory_loads_all_six_real_agents(self):
        """The default path resolves to config/agents/, not a nonexistent one."""
        configs = load_all_configs()
        self.assertEqual(set(configs), REAL_AGENT_IDS)

    def test_adas_config_maps_nested_yaml_shape_correctly(self):
        """ui.route and storage.* fields are flattened onto AgentConfig."""
        configs = load_all_configs()
        adas = configs["adas-agent"]
        self.assertEqual(adas.display_name, "ADAS Knowledge Assistant")
        self.assertEqual(adas.route, "/agents/adas")
        self.assertEqual(adas.qdrant_collection, "jlr-adas-agent-chunks")
        self.assertEqual(adas.incoming_raw_folder, "storage/raw/adas-agent")

    def test_supplier_agent_id_is_hyphenated_not_the_old_stale_underscored_value(self):
        """Regression guard for the exact mismatch that broke every real lookup."""
        configs = load_all_configs()
        self.assertIn("supplier-agent", configs)
        self.assertNotIn("supplier_database", configs)

    def test_missing_directory_raises_instead_of_silently_falling_back(self):
        """The old silent fallback-to-a-stale-file behavior is gone."""
        with self.assertRaises(RuntimeError):
            load_all_configs("/nonexistent/path/that/should/never/exist")

    def test_explicit_directory_argument_still_works(self):
        """An explicit path is honored exactly, not just the cached default."""
        real_dir = Path(__file__).resolve().parents[2] / "config" / "agents"
        configs = load_all_configs(real_dir)
        self.assertEqual(set(configs), REAL_AGENT_IDS)


class GetAgentConfigTests(unittest.TestCase):
    """Checks the cached, single-agent accessor nodes.py now uses."""

    def test_resolves_a_real_agent(self):
        config = get_agent_config("supplier-agent")
        self.assertEqual(config.agent_id, "supplier-agent")
        self.assertEqual(config.qdrant_collection, "jlr-supplier-agent-chunks")

    def test_unknown_agent_id_raises_key_error_with_known_agents_listed(self):
        with self.assertRaises(KeyError) as ctx:
            get_agent_config("not-a-real-agent")
        self.assertIn("adas-agent", str(ctx.exception))

    def test_repeated_calls_return_the_same_cached_dict_instance(self):
        """Confirms the lru_cache actually avoids re-reading disk every call."""
        first = get_agent_config("adas-agent")
        second = get_agent_config("adas-agent")
        self.assertIs(first, second)


class AgentConfigFromYamlPayloadTests(unittest.TestCase):
    """Unit-level checks on the payload-mapping classmethod itself."""

    def test_maps_full_nested_payload(self):
        payload = {
            "schema_version": 1, "agent_id": "eds-agent", "display_name": "EDS Assistant",
            "ui": {"route": "/agents/eds"},
            "storage": {"incoming_raw_folder": "storage/raw/eds-agent", "qdrant_collection": "jlr-eds-agent-chunks"},
        }
        config = AgentConfig.from_yaml_payload(payload)
        self.assertEqual(config.route, "/agents/eds")
        self.assertEqual(config.qdrant_collection, "jlr-eds-agent-chunks")
        self.assertEqual(config.domain_description, "EDS Assistant")  # defaults to display_name

    def test_missing_agent_id_raises_during_load_all_configs(self):
        """A malformed file with no agent_id is caught, not silently indexed under ''."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_file = Path(tmp_dir) / "broken.yaml"
            bad_file.write_text("display_name: No ID Here\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_all_configs(tmp_dir)


if __name__ == "__main__":
    unittest.main()
