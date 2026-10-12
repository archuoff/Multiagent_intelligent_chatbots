export default function Composer({ value, onChange, onSend, disabled }) {
  function handleKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSend();
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          placeholder="Ask a question..."
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
        />
        <button onClick={onSend} disabled={disabled}>
          Send
        </button>
      </div>
    </div>
  );
}
