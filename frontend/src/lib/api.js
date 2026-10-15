// Thin fetch wrappers around the real backend contract (GET /agents, POST /chat,
// the /assets/visual-assets static mount) -- ported from frontend-old/index.html
// with no behavior change, just split out of the single-file script.

export function normalizeApiBase(raw) {
  return (raw || "").trim().replace(/\/+$/, "");
}

// The synthesis LLM is instructed to put each numbered list item on its own
// line, but it does not reliably comply -- it sometimes collapses a list into
// one inline run like "...: 1. A [1] 2. B [2] 3. C [3]". Markdown requires a
// newline before each list marker to render it as a list at all, so a prompt
// fix alone can't guarantee this; this normalizer deterministically reflows
// that specific pattern (a citation bracket immediately followed by the next
// "N. ") onto separate lines before the text reaches ReactMarkdown. Text that
// already has real line breaks is untouched (the \]\s\d+\.\s check is false).
export function normalizeInlineLists(text) {
  if (!text || !/\]\s\d+\.\s/.test(text)) return text;
  return text
    .replace(/:\s(\d+\.\s)/, ":\n$1")
    .replace(/\]\s(\d+\.\s)/g, "]\n$1");
}

const SUPERSCRIPT_DIGITS = { 0: "⁰", 1: "¹", 2: "²", 3: "³", 4: "⁴",
  5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹" };

// Turns bracketed citation markers like "[1]" or "[12]" into small Unicode
// superscript numerals (e.g. "¹") -- reads as a footnote instead of a
// clunky inline bracket, with no extra markup or library needed since these
// are just plain characters that flow through markdown rendering untouched.
export function normalizeCitations(text) {
  if (!text) return text;
  return text.replace(/\[(\d{1,2})\]/g, (_match, digits) =>
    digits.split("").map((digit) => SUPERSCRIPT_DIGITS[digit]).join(""));
}

export async function loadAgents(apiBase) {
  const response = await fetch(apiBase + "/agents");
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

export async function postChat(apiBase, payload) {
  return fetch(apiBase + "/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// Streams one answer from POST /chat/stream as Server-Sent Events. EventSource
// can't be used here since it only supports GET, not a POST with a JSON body,
// so the response body's ReadableStream is parsed by hand instead. Yields
// {event, data} objects in arrival order: "sources" (once, with
// retrieved_context), "delta" (repeated, {text}), then "done" ({answer,
// session_id, status}) or "error" ({detail}).
export async function* streamChat(apiBase, payload) {
  const response = await fetch(apiBase + "/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) {
    const detail = await safeErrorDetail(response);
    throw new Error("Request failed (" + response.status + "): " + detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const record = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseRecord(record);
        if (parsed) yield parsed;
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseRecord(record) {
  let eventName = "message";
  const dataLines = [];
  for (const line of record.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

export async function safeErrorDetail(response) {
  try {
    const body = await response.json();
    return body.detail || JSON.stringify(body);
  } catch {
    return response.statusText || "unknown error";
  }
}

export function visualAssetUrl(apiBase, path) {
  if (!path) return "";
  const normalized = String(path).replace(/\\/g, "/");
  const marker = "storage/visual-assets/";
  if (normalized.startsWith(marker)) {
    return apiBase + "/assets/visual-assets/" + normalized.slice(marker.length);
  }
  return normalized;
}

export function citationLocation(citation) {
  if (!citation) return "";
  if (citation.page_number != null) return "page " + citation.page_number;
  if (citation.sheet_name) return "sheet " + citation.sheet_name;
  if (citation.slide_number != null) return "slide " + citation.slide_number;
  return "";
}

export function sourcesFromContext(retrievedContext) {
  return (retrievedContext || []).map((context) => ({
    document_title: context.document_title,
    document_id: context.citation ? context.citation.document_id : null,
    location: citationLocation(context.citation),
    image_path: context.image_path,
    visual_node_type: context.visual_node_type,
    visual_object_type: context.visual_object_type,
    ocr_status: context.ocr_status,
    image_description_status: context.image_description_status,
    vlm_confidence: context.vlm_confidence,
  }));
}

export function dedupeSources(sources) {
  if (!sources) return [];
  const seen = new Set();
  const unique = [];
  sources.forEach((source) => {
    const key = (source.document_title || source.document_id || "") + "|" + (source.location || "");
    if (seen.has(key)) return;
    seen.add(key);
    unique.push(source);
  });
  return unique;
}
