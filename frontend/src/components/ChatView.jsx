import { useState } from "react";
import { avatarColor, initials } from "../lib/avatar";
import { streamChat, sourcesFromContext } from "../lib/api";
import ConversationSidebar from "./ConversationSidebar";
import MessageList from "./MessageList";
import Composer from "./Composer";

export default function ChatView({ apiBase, agent, sessionsApi, onBackToAgents }) {
  const [inputValue, setInputValue] = useState("");
  const [typing, setTyping] = useState(false);
  const [statusSteps, setStatusSteps] = useState([]);
  const [streamingMessage, setStreamingMessage] = useState(null);

  const sessions = sessionsApi.getSessionsForAgent(agent.agent_id);
  const activeSession = sessionsApi.getActiveSession(agent.agent_id);
  const messages = activeSession ? activeSession.messages : [];
  const displayMessages = streamingMessage ? [...messages, streamingMessage] : messages;

  const description =
    agent.domain_description && agent.domain_description !== agent.display_name ? agent.domain_description : "";

  async function handleSend() {
    const text = inputValue.trim();
    if (!text || !activeSession) return;

    const sessionId = activeSession.sessionId;
    sessionsApi.appendMessage(agent.agent_id, sessionId, { role: "user", content: text });
    setInputValue("");
    setTyping(true);
    setStatusSteps([]);

    const history = [...messages, { role: "user", content: text }]
      .filter((item) => item.role === "user" || item.role === "assistant")
      .map((item) => ({ role: item.role, content: item.content }))
      .slice(-10);

    let sources = [];
    let answerText = "";

    try {
      for await (const { event, data } of streamChat(apiBase, {
        agent_id: agent.agent_id,
        message: text,
        session_id: sessionId,
        conversation_history: history,
      })) {
        if (event === "status") {
          setStatusSteps((prev) => (prev.some((step) => step.stage === data.stage) ? prev : [...prev, data]));
        } else if (event === "sources") {
          sources = sourcesFromContext(data.retrieved_context);
        } else if (event === "delta") {
          answerText += data.text;
          setTyping(false);
          setStatusSteps([]);
          setStreamingMessage({ role: "assistant", content: answerText, sources });
        } else if (event === "error") {
          throw new Error(data.detail || "Streaming failed.");
        }
        // "done" carries the same final answer already assembled from delta
        // events -- nothing further to apply beyond what's already streamed in.
      }
      sessionsApi.appendMessage(agent.agent_id, sessionId, { role: "assistant", content: answerText, sources });
    } catch (error) {
      sessionsApi.appendMessage(agent.agent_id, sessionId, {
        role: "error",
        content: answerText ? answerText + "\n\n(Response was interrupted: " + error.message + ")" : "Could not reach the backend: " + error.message,
      });
    } finally {
      setTyping(false);
      setStatusSteps([]);
      setStreamingMessage(null);
    }
  }

  return (
    <div id="chatView" className="open">
      <ConversationSidebar
        sessions={sessions}
        activeSessionId={activeSession ? activeSession.sessionId : null}
        onNewChat={() => sessionsApi.createNewSession(agent.agent_id)}
        onSelectSession={(sessionId) => sessionsApi.setActiveSession(agent.agent_id, sessionId)}
        onDeleteSession={(sessionId) => sessionsApi.deleteSession(agent.agent_id, sessionId)}
        onBackToAgents={onBackToAgents}
      />
      <div className="chat-main">
        <div className="chat-header">
          <div className="chat-header-inner">
            <div className="agent-avatar" style={{ background: avatarColor(agent.agent_id) }}>
              {initials(agent.display_name)}
            </div>
            <div className="chat-header-titles">
              <h2>{agent.display_name}</h2>
              <p>{description}</p>
            </div>
          </div>
        </div>
        <MessageList
          apiBase={apiBase}
          agentName={agent.display_name}
          messages={displayMessages}
          typing={typing}
          statusSteps={statusSteps}
        />
        <Composer value={inputValue} onChange={setInputValue} onSend={handleSend} disabled={typing || !!streamingMessage} />
      </div>
    </div>
  );
}
