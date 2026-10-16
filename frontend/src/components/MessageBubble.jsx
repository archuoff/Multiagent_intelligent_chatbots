import { isValidElement, cloneElement, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { normalizeInlineLists, visualAssetUrl } from "../lib/api";
import SourceList from "./SourceList";
import Lightbox from "./Lightbox";

const IMAGE_PATH_PATTERN = /storage\/visual-assets\/[^\s]+?\.(?:png|jpg|jpeg|webp)/gi;
const CITATION_PATTERN = /(\[\d{1,2}\])/g;

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

function scrollToSource(number) {
  const target = document.getElementById("source-chip-" + number);
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("source-chip-highlight");
  setTimeout(() => target.classList.remove("source-chip-highlight"), 1400);
}

function CitationMarker({ number }) {
  return (
    <sup className="citation-marker" onClick={() => scrollToSource(number)} role="button" tabIndex={0}>
      {number}
    </sup>
  );
}

// Walks already-parsed ReactMarkdown children (strings and nested elements)
// and replaces "[n]" citation markers with a clickable <sup> element --
// done as a post-processing pass over the parsed tree rather than splitting
// the raw markdown text beforehand, so list/table structure around a
// citation is never broken by the split.
function linkifyCitations(node, keyPrefix) {
  if (typeof node === "string") {
    const pieces = node.split(CITATION_PATTERN);
    return pieces.map((piece, index) => {
      const match = piece.match(/^\[(\d{1,2})\]$/);
      if (match) return <CitationMarker key={keyPrefix + "-c" + index} number={match[1]} />;
      return piece;
    });
  }
  if (Array.isArray(node)) {
    return node.map((child, index) => linkifyCitations(child, keyPrefix + "-" + index));
  }
  if (isValidElement(node)) {
    return cloneElement(node, { children: linkifyCitations(node.props.children, keyPrefix + "-e") });
  }
  return node;
}

function citationAwareComponent(Tag) {
  return function CitationAware(props) {
    const { children, ...rest } = props;
    return <Tag {...rest}>{linkifyCitations(children, Tag)}</Tag>;
  };
}

const MARKDOWN_COMPONENTS = {
  p: citationAwareComponent("p"),
  li: citationAwareComponent("li"),
  td: citationAwareComponent("td"),
  th: citationAwareComponent("th"),
  strong: citationAwareComponent("strong"),
  em: citationAwareComponent("em"),
};

function InlineImage({ apiBase, path, onEnlarge }) {
  const url = visualAssetUrl(apiBase, path);
  return (
    <button type="button" className="retrieved-image-card" onClick={() => onEnlarge(url)}>
      <img src={url} alt="Retrieved document visual" />
    </button>
  );
}

function AnswerText({ apiBase, content }) {
  const parts = splitAnswerText(normalizeInlineLists(content));
  const [lightbox, setLightbox] = useState(null);
  const imageParts = parts.filter((part) => part.type === "image");

  return (
    <div className="answer-text">
      {parts.map((part, index) =>
        part.type === "text" ? (
          <ReactMarkdown key={index} remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
            {part.value}
          </ReactMarkdown>
        ) : null,
      )}
      {imageParts.length ? (
        <div className="inline-image-grid">
          {imageParts.map((part, index) => (
            <InlineImage key={index} apiBase={apiBase} path={part.path} onEnlarge={setLightbox} />
          ))}
        </div>
      ) : null}
      {lightbox ? <Lightbox src={lightbox} onClose={() => setLightbox(null)} /> : null}
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
