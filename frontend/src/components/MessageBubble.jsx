import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { normalizeCitations, normalizeInlineLists, visualAssetUrl } from "../lib/api";
import SourceList from "./SourceList";

const IMAGE_PATH_PATTERN = /storage\/visual-assets\/[^\s]+?\.(?:png|jpg|jpeg|webp)/gi;

// Splits answer text around any storage/visual-assets/...png|jpg|jpeg|webp
// reference so it can render as safe React elements (an inline image card)
// instead of frontend-old's string-built innerHTML -- same visible result,
// no HTML-injection surface. Surrounding text segments are then rendered as
// markdown (the model's numbered sections/bullets/bold come through as real
// structure instead of one flat paragraph of plain text).
function splitAnswerText(text) {
  const parts = [];
  let lastIndex = 0;
  for (const match of text.matchAll(IMAGE_PATH_PATTERN)) {
    if (match.index > lastIndex) parts.push({ type: "text", value: text.slice(lastIndex, match.index) });
    parts.push({ type: "image", path: match[0].replace(/\\/g, "/") });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) parts.push({ type: "text", value: text.slice(lastIndex) });
  return parts;
}

function AnswerText({ apiBase, content }) {
  const parts = splitAnswerText(normalizeCitations(normalizeInlineLists(content)));
  return (
    <div className="answer-text">
      {parts.map((part, index) =>
        part.type === "text" ? (
          <ReactMarkdown key={index} remarkPlugins={[remarkGfm]}>
            {part.value}
          </ReactMarkdown>
        ) : (
          <div key={index} className="retrieved-image-card">
            <img src={visualAssetUrl(apiBase, part.path)} alt="Retrieved document visual" />
            <div className="retrieved-image-path">{part.path}</div>
          </div>
        ),
      )}
    </div>
  );
}

export default function MessageBubble({ apiBase, message }) {
  if (message.role === "user") {
    return (
      <div className="msg-row user">
        <div className="bubble-user">{message.content}</div>
      </div>
    );
  }

  if (message.role === "error") {
    return (
      <div className="msg-row assistant">
        <div className="answer-card error-card">
          <div className="answer-text">{message.content}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="msg-row assistant">
      <div className="answer-flow">
        <AnswerText apiBase={apiBase} content={message.content} />
        <SourceList apiBase={apiBase} sources={message.sources} />
      </div>
    </div>
  );
}
