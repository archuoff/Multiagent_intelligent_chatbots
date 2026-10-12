import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";

export default function MessageList({ apiBase, agentName, messages, typing }) {
  const containerRef = useRef(null);

  useEffect(() => {
    if (containerRef.current) containerRef.current.scrollTop = containerRef.current.scrollHeight;
  }, [messages, typing]);

  return (
    <div className="messages" ref={containerRef}>
      <div className="messages-inner">
        {messages.length === 0 && !typing ? (
          <div className="empty-state">
            <h3>Ask {agentName} something</h3>
            <p>Answers are generated live from your ingested documents, with citations back to the source.</p>
          </div>
        ) : (
          <>
            {messages.map((message, index) => (
              <MessageBubble key={index} apiBase={apiBase} message={message} />
            ))}
            {typing ? (
              <div className="msg-row assistant">
                <div className="answer-card">
                  <div className="typing-dots">
                    <span></span>
                    <span></span>
                    <span></span>
                  </div>
                </div>
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
