export default function ConversationSidebar({ sessions, activeSessionId, onNewChat, onSelectSession, onBackToAgents }) {
  return (
    <div className="conversation-sidebar">
      <button className="new-chat-btn" onClick={onNewChat}>
        + New chat
      </button>
      <div className="conversation-list">
        {sessions.map((session) => (
          <div
            key={session.sessionId}
            className={"conversation-item" + (session.sessionId === activeSessionId ? " active" : "")}
            onClick={() => onSelectSession(session.sessionId)}
            title={session.title}
          >
            {session.title || "New chat"}
          </div>
        ))}
      </div>
      <button className="back-to-agents-btn" onClick={onBackToAgents}>
        &larr; All agents
      </button>
    </div>
  );
}
