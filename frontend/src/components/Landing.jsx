import AgentCard from "./AgentCard";

export default function Landing({ agents, connected, onRetry, onOpenAgent }) {
  return (
    <div id="landingView">
      <div className="landing-inner">
        <div className="landing-hero">
          <div className="landing-eyebrow">Knowledge assistants</div>
          <h2>Choose an assistant to get started</h2>
          <p>Ask a domain question and get a real, source-cited answer from your knowledge base.</p>
        </div>
        {agents.length > 0 ? (
          <div className="agent-grid">
            {agents.map((agent) => (
              <AgentCard key={agent.agent_id} agent={agent} onOpen={onOpenAgent} />
            ))}
          </div>
        ) : (
          <div className="landing-status">
            {connected ? (
              "Loading agents..."
            ) : (
              <>
                Could not reach the backend at this URL.
                <br />
                Confirm the API is running and the URL in Settings is correct.
                <br />
                <button className="retry-btn" onClick={onRetry}>
                  Retry
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
