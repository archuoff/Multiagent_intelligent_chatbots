# Agent Registry

This folder contains one minimal, version-controlled configuration file for
each JLR knowledge agent. It is the shared source of truth for stable agent
identity and storage routing.

Confirmed fields only:

- `agent_id`: immutable lowercase storage/retrieval identifier; it must match
  the raw-folder suffix and Qdrant collection convention used by ingestion.
- `display_name`: human-readable agent name for the future portal.
- `ui.route`: planned React route; it does not require the frontend to exist.
- `storage.incoming_raw_folder`: current location of uploaded/source files.
- `storage.qdrant_collection`: deterministic vector collection created during indexing.

Access rules, confidential-content policies, upload permissions, and SME
escalation rules are intentionally absent until business owners define them.
They must be added as separate policy sections later, not guessed here.

The older `backend/agent/config/` files serve a separate future query-agent
configuration purpose. Do not merge the two configurations until the complete
query pipeline contract is finalized.
