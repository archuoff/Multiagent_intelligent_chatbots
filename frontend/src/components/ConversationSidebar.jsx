import { useMemo, useState } from "react";
import { groupSessions } from "../lib/sessions";

function ConversationGroup({ group, activeSessionId, onSelectSession, onDeleteSession, onTogglePin }) {
  const [collapsed, setCollapsed] = useState(false);

  function handleDelete(event, sessionId) {
    event.stopPropagation();
    if (window.confirm("Delete this chat? This can't be undone.")) {
      onDeleteSession(sessionId);
    }
  }

  function handlePin(event, sessionId) {
    event.stopPropagation();
    onTogglePin(sessionId);
  }

  return (
    <div className="conversation-group">
      <button type="button" className="conversation-group-header" onClick={() => setCollapsed((value) => !value)}>
        <span>{group.label}</span>
        <span className={"conversation-group-caret" + (collapsed ? " collapsed" : "")}>&#9662;</span>
      </button>
      {collapsed ? null : (
        <div className="conversation-list">
          {group.sessions.map((session) => (
            <div
              key={session.sessionId}
              className={"conversation-item" + (session.sessionId === activeSessionId ? " active" : "")}
              onClick={() => onSelectSession(session.sessionId)}
              title={session.title}
            >
              <span className="conversation-item-icon">
                <svg viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.2">
                  <path d="M1.5 3h11v6.2h-7L2.5 11V9.2H1.5Z" />
                </svg>
              </span>
              <span className="conversation-item-title">{session.title || "New chat"}</span>
              <button
                type="button"
                className={"conversation-pin-btn" + (session.pinned ? " pinned" : "")}
                title={session.pinned ? "Unpin" : "Pin"}
                aria-label={session.pinned ? "Unpin chat" : "Pin chat"}
                onClick={(event) => handlePin(event, session.sessionId)}
              >
                <svg viewBox="0 0 14 14" width="12" height="12" fill={session.pinned ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.2">
                  <path d="M7 1.5 9 5l3 .9-2.2 2.3.4 3.3L7 10l-3.2 1.5.4-3.3L2 5.9 5 5Z" />
                </svg>
              </button>
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
      )}
    </div>
  );
}

export default function ConversationSidebar({
  sessions,
  activeSessionId,
  onNewChat,
  onSelectSession,
  onDeleteSession,
  onTogglePin,
  onBackToAgents,
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const trimmed = query.trim().toLowerCase();
    if (!trimmed) return sessions;
    return sessions.filter((session) => (session.title || "").toLowerCase().includes(trimmed));
  }, [sessions, query]);

  const groups = useMemo(() => groupSessions(filtered), [filtered]);

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
      <div className="sidebar-search">
        <svg viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.3">
          <circle cx="6" cy="6" r="4" />
          <line x1="9" y1="9" x2="12.5" y2="12.5" />
        </svg>
        <input
          type="text"
          placeholder="Search chats"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="conversation-groups">
        {groups.length === 0 ? <div className="sidebar-empty">No conversations yet</div> : null}
        {groups.map((group) => (
          <ConversationGroup
            key={group.key}
            group={group}
            activeSessionId={activeSessionId}
            onSelectSession={onSelectSession}
            onDeleteSession={onDeleteSession}
            onTogglePin={onTogglePin}
          />
        ))}
      </div>
      <button className="back-to-agents-btn" onClick={onBackToAgents}>
        &larr; Back
      </button>
    </div>
  );
}
