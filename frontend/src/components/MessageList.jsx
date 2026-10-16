import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";
import StatusTrace from "./StatusTrace";
import WelcomeState from "./WelcomeState";

export default function MessageList({ apiBase, agent, messages, typing, statusSteps = [], onSuggestedPrompt }) {
  const containerRef = useRef(null);

  useEffect(() => {
    if (containerRef.current) containerRef.current.scrollTop = containerRef.current.scrollHeight;
  }, [messages, typing, statusSteps]);

  return (
    <div className="messages" ref={containerRef}>
      <div className="messages-inner">
        {messages.length === 0 && !typing ? (
          <WelcomeState agent={agent} onSuggestedPrompt={onSuggestedPrompt} />
        ) : (
          <>
            {messages.map((message, index) => (
              <MessageBubble key={index} apiBase={apiBase} message={message} />
            ))}
            {typing ? (
              <div className="msg-row assistant">
                {statusSteps.length ? (
                  <div className="answer-card status-card">
                    <StatusTrace steps={statusSteps} />
                  </div>
                ) : (
                  <div className="answer-card">
                    <div className="typing-dots">
                      <span></span>
                      <span></span>
                      <span></span>
                    </div>
                  </div>
                )}
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
