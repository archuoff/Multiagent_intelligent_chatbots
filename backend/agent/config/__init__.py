"""Minimal agent configuration loader for the future query graph.

The active ingestion pipeline uses ``config/agents`` as the registry. This
module only keeps the early ``backend.agent`` intent-classifier prototype
importable until the full query-pipeline contract is finalized.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Small config shape consumed by ``backend.agent.nodes``."""

    agent_id: str
    name: str
    domain_description: str = ""


def load_all_configs(config_directory: str | Path) -> dict[str, AgentConfig]:
    """Loads simple YAML agent files when PyYAML is available."""
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("PyYAML is required to load backend.agent configs.") from error

    root = Path(config_directory)
    if not root.exists():
        root = Path(__file__).parent
    configs: dict[str, AgentConfig] = {}
    for path in root.glob("*.yaml"):
        payload: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        agent_id = str(payload.get("agent_id") or path.stem)
        configs[agent_id] = AgentConfig(
            agent_id=agent_id,
            name=str(payload.get("name") or payload.get("display_name") or agent_id),
            domain_description=str(payload.get("domain_description") or payload.get("name") or agent_id),
        )
    return configs
