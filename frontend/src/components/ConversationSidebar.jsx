import { useMemo, useState } from "react";
import { groupSessions } from "../lib/sessions";

function ConversationGroup({ group, activeSessionId, onSelectSession, onRequestDelete, onTogglePin }) {
  const [collapsed, setCollapsed] = useState(false);

  function handlePin(event, sessionId) {
    event.stopPropagation();
    onTogglePin(sessionId);
  }

  function handleDeleteClick(event, session) {
    event.stopPropagation();
    onRequestDelete(session);
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
                onClick={(event) => handleDeleteClick(event, session)}
              >
                <svg viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M2.5 4h9M5.3 4V2.6h3.4V4M3.4 4l.5 8.2a1 1 0 0 0 1 .9h4.2a1 1 0 0 0 1-.9l.5-8.2" />
                </svg>
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DeleteChatModal({ session, onConfirm, onCancel }) {
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <h3>Delete this chat?</h3>
        <p>
          "{session.title || "New chat"}" will be permanently deleted. This can't be undone.
        </p>
        <div className="modal-actions">
          <button type="button" className="modal-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="modal-btn-danger" onClick={onConfirm}>
            Delete
          </button>
        </div>
      </div>
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
  const [pendingDelete, setPendingDelete] = useState(null);

  const filtered = useMemo(() => {
    const trimmed = query.trim().toLowerCase();
    if (!trimmed) return sessions;
    return sessions.filter((session) => (session.title || "").toLowerCase().includes(trimmed));
  }, [sessions, query]);

  const groups = useMemo(() => groupSessions(filtered), [filtered]);

  function confirmDelete() {
    onDeleteSession(pendingDelete.sessionId);
    setPendingDelete(null);
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
            onRequestDelete={setPendingDelete}
            onTogglePin={onTogglePin}
          />
        ))}
      </div>
      <div className="sidebar-profile-chip">
        <span className="sidebar-profile-avatar">DU</span>
        <span className="sidebar-profile-name">Demo User</span>
        <button
          type="button"
          className="sidebar-profile-back-btn"
          title="Back to all agents"
          aria-label="Back to all agents"
          onClick={onBackToAgents}
        >
          <svg viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 2.5 4.5 7 9 11.5" />
          </svg>
        </button>
      </div>
      {pendingDelete ? (
        <DeleteChatModal session={pendingDelete} onConfirm={confirmDelete} onCancel={() => setPendingDelete(null)} />
      ) : null}
    </div>
  );
}
