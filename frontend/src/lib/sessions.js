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
  return { sessionId: newSessionId(), title: "New chat", createdAt: now, updatedAt: now, active: true, messages: [] };
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
    getSessionsForAgent,
    getActiveSession,
  };
}
