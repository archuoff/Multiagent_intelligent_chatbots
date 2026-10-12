export default function ConversationSidebar({
  sessions,
  activeSessionId,
  onNewChat,
  onSelectSession,
  onDeleteSession,
  onBackToAgents,
}) {
  function handleDelete(event, sessionId) {
    event.stopPropagation();
    if (window.confirm("Delete this chat? This can't be undone.")) {
      onDeleteSession(sessionId);
    }
  }

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
            <span className="conversation-item-title">{session.title || "New chat"}</span>
            <button
              type="button"
              className="conversation-delete-btn"
              title="Delete chat"
              aria-label="Delete chat"
              onClick={(event) => handleDelete(event, session.sessionId)}
            >
              &times;
            </button>
          </div>
        ))}
      </div>
      <button className="back-to-agents-btn" onClick={onBackToAgents}>
        &larr; Back
      </button>
    </div>
  );
}
