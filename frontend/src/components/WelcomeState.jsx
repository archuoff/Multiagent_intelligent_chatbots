import { avatarColor, initials } from "../lib/avatar";

const GENERIC_PROMPTS = [
  { title: "Get started", description: "Ask about a document, part, or process in this agent's knowledge base." },
  { title: "Find a spec", description: "Look up a requirement, test result, or technical value from ingested documents." },
  { title: "Summarize a topic", description: "Ask for an overview of a process, issue, or training requirement." },
];

const ICONS = [
  <path key="a" d="M2 11.5 5.8 8l2.4 2.2L11 7l3 3.8" />,
  <circle key="b" cx="7" cy="7" r="5" />,
  <path key="c" d="M3 3h8v3H3zM3 8h8v3H3z" />,
];

// Builds 3 short, on-domain example questions from the agent's own
// domain_description (already surfaced by GET /agents) instead of generic
// placeholder text -- falls back to generic prompts only when a
// description hasn't been filled in for that agent yet.
function buildPrompts(agent) {
  const description = (agent.domain_description || "").trim();
  if (!description) return GENERIC_PROMPTS;
  const topic = description.split(/[.,]/)[0].trim();
  return [
    { title: "Explore " + agent.display_name, description: "What does " + agent.display_name + " cover? (" + topic + ")" },
    { title: "Ask a specific question", description: "Ask about a document, spec, or issue related to " + topic + "." },
    { title: "Request a summary", description: "Get a quick summary of a topic within " + topic + "." },
  ];
}

export default function WelcomeState({ agent, onSuggestedPrompt }) {
  const prompts = buildPrompts(agent);

  return (
    <div className="welcome-state">
      <div className="welcome-avatar" style={{ background: avatarColor(agent.agent_id) }}>
        {initials(agent.display_name)}
      </div>
      <h2>Welcome to {agent.display_name}</h2>
      <p>How can I help you today?</p>
      <div className="suggestion-row">
        {prompts.map((prompt, index) => (
          <button
            key={index}
            type="button"
            className="suggestion-card"
            onClick={() => onSuggestedPrompt(prompt.description)}
          >
            <span className="suggestion-icon">
              <svg viewBox="0 0 14 14" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                {ICONS[index % ICONS.length]}
              </svg>
            </span>
            <span className="suggestion-title">{prompt.title}</span>
            <span className="suggestion-description">{prompt.description}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
