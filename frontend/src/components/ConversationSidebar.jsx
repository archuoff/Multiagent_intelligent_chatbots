import { useState } from "react";

export default function ConversationSidebar({
  sessions,
  activeSessionId,
  onNewChat,
  onSelectSession,
  onDeleteSession,
  onBackToAgents,
}) {
  const [collapsed, setCollapsed] = useState(false);

  function handleDelete(event, sessionId) {
    event.stopPropagation();
    if (window.confirm("Delete this chat? This can't be undone.")) {
      onDeleteSession(sessionId);
    }
  }

  if (collapsed) {
    return (
      <div className="conversation-sidebar collapsed">
        <button
          type="button"
          className="sidebar-collapse-btn"
          title="Expand sidebar"
          aria-label="Expand sidebar"
          onClick={() => setCollapsed(false)}
        >
          &raquo;
        </button>
        <button type="button" className="sidebar-icon-btn" title="New chat" aria-label="New chat" onClick={onNewChat}>
          +
        </button>
        <button
          type="button"
          className="sidebar-icon-btn"
          title="Back to all agents"
          aria-label="Back to all agents"
          onClick={onBackToAgents}
        >
          &larr;
        </button>
      </div>
    );
  }

  return (
    <div className="conversation-sidebar">
      <div className="sidebar-top-row">
        <button className="new-chat-btn" onClick={onNewChat}>
          + New chat
        </button>
        <button
          type="button"
          className="sidebar-collapse-btn"
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
          onClick={() => setCollapsed(true)}
        >
          &laquo;
        </button>
      </div>
      <div className="sidebar-section-label">Chats</div>
      <div className="conversation-list">
        {sessions.length === 0 ? <div className="sidebar-empty">No conversations yet</div> : null}
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
