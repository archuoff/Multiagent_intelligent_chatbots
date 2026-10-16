// Conversation history, stored in the browser's localStorage only -- there is
// no backend session store yet (no DB table, no checkpointer-backed API), so
// this is the one source of truth for history today. Same storage key/shape
// as frontend-old/index.html so existing browser data isn't orphaned by this
// port. Deliberately isolated behind this one module: swapping to a real
// Postgres-backed API later (once real login exists) should only mean
// rewriting the functions in this file, not the components that call them.

import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "jlr-chat-sessions-v1";
const TITLE_MAX_LENGTH = 42;

const DAY_MS = 24 * 60 * 60 * 1000;

// Buckets a list of sessions into Pinned + recency-based groups for the
// sidebar. Plain date math against "now" -- no date library needed for
// four coarse buckets.
export function groupSessions(sessions) {
  const pinned = sessions.filter((item) => item.pinned);
  const rest = sessions.filter((item) => !item.pinned);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();

  const groups = { today: [], yesterday: [], previous7: [], older: [] };
  for (const item of rest) {
    const updated = new Date(item.updatedAt || item.createdAt).getTime();
    const daysAgo = Math.floor((startOfToday - updated) / DAY_MS);
    if (updated >= startOfToday) groups.today.push(item);
    else if (daysAgo === 1) groups.yesterday.push(item);
    else if (daysAgo <= 7) groups.previous7.push(item);
    else groups.older.push(item);
  }

  return [
    { key: "pinned", label: "Pinned", sessions: pinned },
    { key: "today", label: "Today", sessions: groups.today },
    { key: "yesterday", label: "Yesterday", sessions: groups.yesterday },
    { key: "previous7", label: "Previous 7 Days", sessions: groups.previous7 },
    { key: "older", label: "Older", sessions: groups.older },
  ].filter((group) => group.sessions.length > 0);
}

function newSessionId() {
  return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-" + Math.random().toString(16).slice(2);
}

function loadStoredSessions() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function persistSessions(sessions) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
  } catch {
    // Ignore localStorage quota/private-mode failures; the backend session_id
    // still works for the current tab even if nothing persists across reloads.
  }
}

function makeSession() {
  const now = new Date().toISOString();
  return { sessionId: newSessionId(), title: "New chat", createdAt: now, updatedAt: now, active: true, pinned: false, messages: [] };
}

export function useSessions() {
  const [sessions, setSessions] = useState(loadStoredSessions);

  useEffect(() => {
    persistSessions(sessions);
  }, [sessions]);

  const ensureAgentSessions = useCallback((agentId) => {
    setSessions((prev) => {
      if (prev[agentId] && prev[agentId].length > 0) return prev;
      return { ...prev, [agentId]: [makeSession()] };
    });
  }, []);

  const createNewSession = useCallback((agentId) => {
    const session = makeSession();
    setSessions((prev) => {
      const rest = (prev[agentId] || []).map((item) => ({ ...item, active: false }));
      return { ...prev, [agentId]: [session, ...rest] };
    });
    return session.sessionId;
  }, []);

  const setActiveSession = useCallback((agentId, sessionId) => {
    setSessions((prev) => {
      const list = prev[agentId] || [];
      return { ...prev, [agentId]: list.map((item) => ({ ...item, active: item.sessionId === sessionId })) };
    });
  }, []);

  const appendMessage = useCallback((agentId, sessionId, message) => {
    setSessions((prev) => {
      const list = prev[agentId] || [];
      const updated = list.map((item) => {
        if (item.sessionId !== sessionId) return item;
        const messages = [...item.messages, message];
        let title = item.title;
        if ((!title || title === "New chat") && message.role === "user") {
          title = message.content.slice(0, TITLE_MAX_LENGTH) + (message.content.length > TITLE_MAX_LENGTH ? "..." : "");
        }
        return { ...item, messages, title, updatedAt: new Date().toISOString() };
      });
      return { ...prev, [agentId]: updated };
    });
  }, []);

  const togglePin = useCallback((agentId, sessionId) => {
    setSessions((prev) => {
      const list = prev[agentId] || [];
      return { ...prev, [agentId]: list.map((item) => (item.sessionId === sessionId ? { ...item, pinned: !item.pinned } : item)) };
    });
  }, []);

  const deleteSession = useCallback((agentId, sessionId) => {
    setSessions((prev) => {
      const remaining = (prev[agentId] || []).filter((item) => item.sessionId !== sessionId);
      if (remaining.length === 0) return { ...prev, [agentId]: [makeSession()] };
      if (!remaining.some((item) => item.active)) remaining[0] = { ...remaining[0], active: true };
      return { ...prev, [agentId]: remaining };
    });
  }, []);

  const getSessionsForAgent = useCallback((agentId) => sessions[agentId] || [], [sessions]);

  const getActiveSession = useCallback(
    (agentId) => {
      const list = sessions[agentId] || [];
      return list.find((item) => item.active) || list[0] || null;
    },
    [sessions],
  );

  return {
    ensureAgentSessions,
    createNewSession,
    setActiveSession,
    appendMessage,
    deleteSession,
    togglePin,
    getSessionsForAgent,
    getActiveSession,
  };
}
