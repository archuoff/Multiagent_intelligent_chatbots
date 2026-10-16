import { useEffect, useRef } from "react";

export default function Composer({ value, onChange, onSend, disabled }) {
  const textareaRef = useRef(null);

  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = Math.min(node.scrollHeight, 160) + "px";
  }, [value]);

  function handleKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSend();
    }
  }

  return (
    <div className="composer-wrap">
      <svg className="composer-wave" viewBox="0 0 800 120" preserveAspectRatio="none" aria-hidden="true">
        <path d="M0 70 C 150 20, 300 100, 450 55 C 600 15, 700 85, 800 45 L 800 120 L 0 120 Z" fill="#dbeafe" opacity="0.55" />
        <path d="M0 90 C 180 50, 320 115, 480 80 C 640 50, 720 100, 800 70 L 800 120 L 0 120 Z" fill="#ede9fe" opacity="0.55" />
        <path d="M0 105 C 200 80, 360 118, 520 95 C 660 76, 740 110, 800 95 L 800 120 L 0 120 Z" fill="#f3e8ff" opacity="0.6" />
      </svg>
      <div className="composer">
        <div className="composer-icon-row">
          <button type="button" className="composer-icon-btn" disabled title="Attach a file (coming soon)" aria-label="Attach a file (coming soon)">
            <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
              <path d="M11 4.2 5.8 9.4a2 2 0 1 0 2.8 2.8l5.1-5.1a3.3 3.3 0 1 0-4.7-4.7L3.9 7.5" />
            </svg>
          </button>
          <button type="button" className="composer-icon-btn" disabled title="Voice input (coming soon)" aria-label="Voice input (coming soon)">
            <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
              <rect x="6" y="1.5" width="4" height="7" rx="2" />
              <path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.3" />
            </svg>
          </button>
        </div>
        <textarea
          ref={textareaRef}
          rows={1}
          placeholder="Ask a question..."
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
        />
        <button className="composer-send" onClick={() => onSend()} disabled={disabled} aria-label="Send" title="Send">
          <svg viewBox="0 0 20 20" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 10h13.5M11 4.5 17 10l-6 5.5" />
          </svg>
        </button>
      </div>
    </div>
  );
}
