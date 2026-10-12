// Thin fetch wrappers around the real backend contract (GET /agents, POST /chat,
// the /assets/visual-assets static mount) -- ported from frontend-old/index.html
// with no behavior change, just split out of the single-file script.

export function normalizeApiBase(raw) {
  return (raw || "").trim().replace(/\/+$/, "");
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
