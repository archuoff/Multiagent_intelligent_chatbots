import { useEffect, useRef, useState } from "react";

export default function TopBar({ apiBase, onApiBaseChange, connected, connText, onBrandClick }) {
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
      <div className="brand" onClick={onBrandClick}>
        <span className="brand-mark">JK</span>
        <div className="brand-titles">
          <h1>JLR Knowledge Platform</h1>
          <p>Tata Technologies</p>
        </div>
      </div>
      <div className="topbar-right">
        <span className={"dot " + (connected ? "ok" : "bad")} title={connText}></span>
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
