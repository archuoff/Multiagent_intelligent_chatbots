import { useEffect, useRef, useState } from "react";
import { avatarColor, initials } from "../lib/avatar";

// Renders the plain platform header on the landing page, or -- when an
// `agent` is passed in -- the merged chat-view header (logo, agent name,
// a real online/offline status pill, and a disabled theme-toggle
// placeholder) so a chat screen no longer stacks two separate bars.
export default function TopBar({ apiBase, onApiBaseChange, connected, connText, onBrandClick, agent }) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const panelRef = useRef(null);
  const toggleRef = useRef(null);

  useEffect(() => {
    function handleOutsideClick(event) {
      if (
        panelRef.current &&
        !panelRef.current.contains(event.target) &&
        event.target !== toggleRef.current
      ) {
        setSettingsOpen(false);
      }
    }
    document.addEventListener("click", handleOutsideClick);
    return () => document.removeEventListener("click", handleOutsideClick);
  }, []);

  return (
    <div className="topbar">
      <div className="topbar-left">
        <div className="brand" onClick={onBrandClick}>
          <span className="brand-mark">JK</span>
          <div className="brand-titles">
            <h1>JLR Knowledge Platform</h1>
            <p>Tata Technologies</p>
          </div>
        </div>
        {agent ? (
          <div className="topbar-agent">
            <div className="topbar-agent-avatar" style={{ background: avatarColor(agent.agent_id) }}>
              {initials(agent.display_name)}
            </div>
            <span className="topbar-agent-name">{agent.display_name}</span>
          </div>
        ) : null}
      </div>
      <div className="topbar-right">
        {agent ? (
          <span className={"status-pill-badge " + (connected ? "ok" : "bad")} title={connText}>
            <span className="status-pill-badge-dot"></span>
            {connected ? "Online" : "Offline"}
          </span>
        ) : (
          <span className={"dot " + (connected ? "ok" : "bad")} title={connText}></span>
        )}
        {agent ? (
          <button
            type="button"
            className="theme-toggle-btn"
            disabled
            title="Theme toggle (coming soon)"
            aria-label="Theme toggle (coming soon)"
          >
            <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="8" cy="8" r="3.3" />
              <path d="M8 1.3v1.6M8 13.1v1.6M14.7 8h-1.6M2.9 8H1.3M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1M12.6 12.6l-1.1-1.1M4.5 4.5 3.4 3.4" />
            </svg>
          </button>
        ) : null}
        <button
          ref={toggleRef}
          className="settings-toggle"
          title="Settings"
          aria-label="Settings"
          onClick={() => setSettingsOpen((open) => !open)}
        >
          <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="8" r="2.1" />
            <path d="M8 1.6v1.5M8 12.9v1.5M14.4 8h-1.5M3.1 8H1.6M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1M12.4 12.4l-1.1-1.1M4.7 4.7 3.6 3.6" />
          </svg>
        </button>
      </div>
      <div ref={panelRef} className={"settings-panel" + (settingsOpen ? " open" : "")}>
        <label htmlFor="apiBase">Backend URL</label>
        <input
          id="apiBase"
          type="text"
          value={apiBase}
          onChange={(event) => onApiBaseChange(event.target.value)}
          onBlur={(event) => onApiBaseChange(event.target.value, true)}
        />
      </div>
    </div>
  );
}
