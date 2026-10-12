import { useCallback, useEffect, useState } from "react";
import TopBar from "./components/TopBar";
import Landing from "./components/Landing";
import ChatView from "./components/ChatView";
import { loadAgents, normalizeApiBase } from "./lib/api";
import { useSessions } from "./lib/sessions";
import "./App.css";

export default function App() {
  const [apiBase, setApiBase] = useState("http://localhost:8000");
  const [agents, setAgents] = useState([]);
  const [connected, setConnected] = useState(false);
  const [connText, setConnText] = useState("Connecting...");
  const [activeAgentId, setActiveAgentId] = useState(null);

  const sessionsApi = useSessions();

  const fetchAgents = useCallback(async () => {
    setConnText("Connecting...");
    try {
      const result = await loadAgents(normalizeApiBase(apiBase));
      setAgents(result);
      setConnected(true);
      setConnText("Connected (" + result.length + " agent" + (result.length === 1 ? "" : "s") + ")");
    } catch {
      setAgents([]);
      setConnected(false);
      setConnText("Disconnected");
    }
  }, [apiBase]);

  useEffect(() => {
    // Initial load only; fetchAgents is intentionally re-invoked later from
    // the settings panel (handleApiBaseChange), not re-run by this effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchAgents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleApiBaseChange(value, shouldReload) {
    setApiBase(value);
    if (shouldReload) fetchAgents();
  }

  function openAgent(agentId) {
    sessionsApi.ensureAgentSessions(agentId);
    setActiveAgentId(agentId);
  }

  function backToAgents() {
    setActiveAgentId(null);
  }

  const activeAgent = agents.find((agent) => agent.agent_id === activeAgentId) || null;

  return (
    <div className="app-shell">
      <TopBar
        apiBase={apiBase}
        onApiBaseChange={handleApiBaseChange}
        connected={connected}
        connText={connText}
        onBrandClick={backToAgents}
      />
      {activeAgent ? (
        <ChatView
          apiBase={normalizeApiBase(apiBase)}
          agent={activeAgent}
          sessionsApi={sessionsApi}
          onBackToAgents={backToAgents}
        />
      ) : (
        <Landing agents={agents} connected={connected} onRetry={fetchAgents} onOpenAgent={openAgent} />
      )}
    </div>
  );
}
