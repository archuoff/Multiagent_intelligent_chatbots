import { avatarColor, initials } from "../lib/avatar";

export default function AgentCard({ agent, onOpen }) {
  const description =
    agent.domain_description && agent.domain_description !== agent.display_name
      ? agent.domain_description
      : "Ask this assistant about its domain knowledge base.";

  return (
    <div className="agent-card" onClick={() => onOpen(agent.agent_id)}>
      <div className="agent-avatar" style={{ background: avatarColor(agent.agent_id) }}>
        {initials(agent.display_name)}
      </div>
      <h3>{agent.display_name}</h3>
      <p>{description}</p>
      <div className="cta">Open assistant &rarr;</div>
    </div>
  );
}
