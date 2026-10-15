import { useState } from "react";
import { dedupeSources, visualAssetUrl } from "../lib/api";

function SourcePreviewImage({ apiBase, source }) {
  const [failed, setFailed] = useState(false);
  const assetUrl = visualAssetUrl(apiBase, source.image_path);
  const caption = source.visual_object_type ? source.visual_object_type + " · " + source.image_path : source.image_path;

  if (failed) {
    return (
      <div className="visual-preview">
        Visual asset:{" "}
        <a href={assetUrl} target="_blank" rel="noopener noreferrer">
          {source.image_path}
        </a>
      </div>
    );
  }

  return (
    <div className="visual-preview">
      <img
        src={assetUrl}
        alt={source.visual_object_type ? source.visual_object_type + " visual asset" : "Retrieved visual asset"}
        loading="lazy"
        onError={() => setFailed(true)}
      />
      <div className="visual-caption">{caption}</div>
    </div>
  );
}

function sourceLabel(source, index) {
  if (source.document_title) return source.document_title;
  return "Source " + (index + 1);
}

export default function SourceList({ apiBase, sources }) {
  const unique = dedupeSources(sources);
  if (unique.length === 0) return null;

  return (
    <div className="sources">
      <div className="sources-title">Sources</div>
      <div className="source-chip-row">
        {unique.map((source, index) => (
          <div key={index} className="source-chip" title={source.document_id || ""}>
            <span className="source-chip-index">{index + 1}</span>
            <span className="source-chip-text">
              {sourceLabel(source, index)}
              {source.location ? <span className="source-chip-loc"> &middot; {source.location}</span> : null}
            </span>
          </div>
        ))}
      </div>
      {unique.map((source, index) => (source.image_path ? <SourcePreviewImage key={index} apiBase={apiBase} source={source} /> : null))}
    </div>
  );
}
