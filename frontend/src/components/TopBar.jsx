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
        <div className="brand-titles">
          <h1>TATA TECHNOLOGIES</h1>
          <p>JLR Knowledge Platform</p>
        </div>
      </div>
      <div className="topbar-right">
        <div className="conn-status">
          <span className={"dot " + (connected ? "ok" : "bad")}></span>
          <span>{connText}</span>
        </div>
        <button ref={toggleRef} className="settings-toggle" onClick={() => setSettingsOpen((open) => !open)}>
          Settings
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
