import { useState } from "react";
import { avatarColor, initials } from "../lib/avatar";
import { postChat, safeErrorDetail, sourcesFromContext } from "../lib/api";
import ConversationSidebar from "./ConversationSidebar";
import MessageList from "./MessageList";
import Composer from "./Composer";

export default function ChatView({ apiBase, agent, sessionsApi, onBackToAgents }) {
  const [inputValue, setInputValue] = useState("");
  const [typing, setTyping] = useState(false);

  const sessions = sessionsApi.getSessionsForAgent(agent.agent_id);
  const activeSession = sessionsApi.getActiveSession(agent.agent_id);
  const messages = activeSession ? activeSession.messages : [];

  const description =
    agent.domain_description && agent.domain_description !== agent.display_name ? agent.domain_description : "";

  async function handleSend() {
    const text = inputValue.trim();
    if (!text || !activeSession) return;

    const sessionId = activeSession.sessionId;
    sessionsApi.appendMessage(agent.agent_id, sessionId, { role: "user", content: text });
    setInputValue("");
    setTyping(true);

    const history = [...messages, { role: "user", content: text }]
      .filter((item) => item.role === "user" || item.role === "assistant")
      .map((item) => ({ role: item.role, content: item.content }))
      .slice(-10);

    try {
      const response = await postChat(apiBase, {
        agent_id: agent.agent_id,
        message: text,
        session_id: sessionId,
        conversation_history: history,
      });

      if (!response.ok) {
        const detail = await safeErrorDetail(response);
        sessionsApi.appendMessage(agent.agent_id, sessionId, {
          role: "error",
          content: "Request failed (" + response.status + "): " + detail,
        });
        return;
      }

      const data = await response.json();
      sessionsApi.appendMessage(agent.agent_id, sessionId, {
        role: "assistant",
        content: data.answer,
        sources: sourcesFromContext(data.retrieved_context),
      });
    } catch (error) {
      sessionsApi.appendMessage(agent.agent_id, sessionId, {
        role: "error",
        content: "Could not reach the backend: " + error.message,
      });
    } finally {
      setTyping(false);
    }
  }

  return (
    <div id="chatView" className="open">
      <ConversationSidebar
        sessions={sessions}
        activeSessionId={activeSession ? activeSession.sessionId : null}
        onNewChat={() => sessionsApi.createNewSession(agent.agent_id)}
        onSelectSession={(sessionId) => sessionsApi.setActiveSession(agent.agent_id, sessionId)}
        onBackToAgents={onBackToAgents}
      />
      <div className="chat-main">
        <div className="chat-header">
          <div className="agent-avatar" style={{ background: avatarColor(agent.agent_id) }}>
            {initials(agent.display_name)}
          </div>
          <div className="chat-header-titles">
            <h2>{agent.display_name}</h2>
            <p>{description}</p>
          </div>
        </div>
        <MessageList apiBase={apiBase} agentName={agent.display_name} messages={messages} typing={typing} />
        <Composer value={inputValue} onChange={setInputValue} onSend={handleSend} disabled={typing} />
      </div>
    </div>
  );
}
