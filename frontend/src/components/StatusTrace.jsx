// A compact, animated status pill shown while the pipeline works through its
// real stages (Thinking -> Grepping -> Verifying -> Answering), replacing a
// flat checklist with a single glowing "current step" indicator plus a
// small progress-dot row -- closer to the shimmer-text thinking indicators
// used by modern AI chat products than a debug-log style list.

const TOTAL_STEPS = 4;

const STAGE_ICONS = {
  understanding: (
    <path d="M7 1.5c-2 0-3.3 1.5-3.3 3.1 0 .8.3 1.4.8 1.9-.9.4-1.5 1.3-1.5 2.3 0 1.5 1.2 2.5 2.6 2.5h.1v1.2c0 .5.4.9.9.9h1.6c.5 0 .9-.4.9-.9V11c1.6-.3 2.7-1.6 2.7-3.2 0-.9-.4-1.7-1-2.2.4-.5.6-1.1.6-1.8C11.4 2.3 9.9 1 8 1c-.3 0-.7.2-1 .5z" />
  ),
  searching: (
    <>
      <circle cx="6.2" cy="6.2" r="4" />
      <line x1="9.1" y1="9.1" x2="13" y2="13" />
    </>
  ),
  ranking: (
    <>
      <path d="M8 1.2 13 3.4v3.3c0 3.7-2.3 6.1-5 7.1-2.7-1-5-3.4-5-7.1V3.4Z" />
      <path d="M5.6 7.8l1.6 1.6 3-3.4" />
    </>
  ),
  writing: (
    <>
      <path d="M10.2 2.1l2.7 2.7-7.4 7.4-3.2.5.5-3.2z" />
    </>
  ),
};

export default function StatusTrace({ steps }) {
  if (!steps.length) return null;
  const current = steps[steps.length - 1];
  const stepIndex = Math.min(steps.length, TOTAL_STEPS);

  return (
    <div className="status-pill">
      <span className="status-pill-icon">
        <svg viewBox="0 0 14 14" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
          {STAGE_ICONS[current.stage] || STAGE_ICONS.understanding}
        </svg>
      </span>
      <span key={current.stage} className="status-pill-label">
        {current.label}
      </span>
      <span className="status-pill-dots">
        {Array.from({ length: TOTAL_STEPS }).map((_, index) => (
          <span key={index} className={"status-dot" + (index < stepIndex ? " filled" : "")} />
        ))}
      </span>
    </div>
  );
}
