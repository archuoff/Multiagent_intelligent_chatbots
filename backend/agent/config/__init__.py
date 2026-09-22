"""Agent configuration loader for the query pipeline.

Reads the same registry the ingestion pipeline already writes to
(``config/agents/*.yaml``) so agent routing, display names, and Qdrant
collection names can never drift between ingestion and querying.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class AgentConfig(BaseModel):
    """Config shape consumed by backend.agent nodes and the query/retrieval layer."""

    schema_version: int
    agent_id: str
    display_name: str
    route: str
    incoming_raw_folder: str
    qdrant_collection: str
    domain_description: str = ""
    system_prompt: str = ""

    @classmethod
    def from_yaml_payload(cls, payload: dict[str, Any]) -> "AgentConfig":
        """Maps config/agents/*.yaml's nested ui/storage shape into a flat AgentConfig."""
        agent_id = str(payload.get("agent_id") or "")
        display_name = str(payload.get("display_name") or agent_id)
        ui = payload.get("ui") or {}
        storage = payload.get("storage") or {}
        return cls(
            schema_version=int(payload.get("schema_version") or 1),
            agent_id=agent_id,
            display_name=display_name,
            route=str(ui.get("route") or ""),
            incoming_raw_folder=str(storage.get("incoming_raw_folder") or ""),
            qdrant_collection=str(storage.get("qdrant_collection") or ""),
            domain_description=str(payload.get("domain_description") or display_name),
            system_prompt=str(payload.get("system_prompt") or ""),
        )


def _default_config_directory() -> Path:
    """Resolves the real agent registry that ingestion already uses."""
    return Path(__file__).resolve().parents[3] / "config" / "agents"


def load_all_configs(config_directory: str | Path | None = None) -> dict[str, AgentConfig]:
    """Loads every agent's config, keyed by agent_id.

    Raises rather than silently falling back when the directory is missing,
    so a misconfigured path fails loudly instead of loading stale data.
    """
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("PyYAML is required to load backend.agent configs.") from error

    root = Path(config_directory) if config_directory is not None else _default_config_directory()
    if not root.exists():
        raise RuntimeError(f"Agent config directory does not exist: {root}")

    configs: dict[str, AgentConfig] = {}
    for path in sorted(root.glob("*.yaml")):
        payload: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = AgentConfig.from_yaml_payload(payload)
        if not config.agent_id:
            raise RuntimeError(f"Agent config file is missing agent_id: {path}")
        configs[config.agent_id] = config
    return configs


@lru_cache(maxsize=1)
def _cached_configs() -> dict[str, AgentConfig]:
    """Caches the full registry so repeated lookups don't re-read disk on every query."""
    return load_all_configs()


def get_agent_config(agent_id: str) -> AgentConfig:
    """Returns one agent's config, cached after the first load."""
    configs = _cached_configs()
    if agent_id not in configs:
        raise KeyError(f"Unknown agent_id: {agent_id!r}. Known agents: {sorted(configs)}")
    return configs[agent_id]
