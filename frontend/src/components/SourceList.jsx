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

export default function SourceList({ apiBase, sources }) {
  const unique = dedupeSources(sources);
  if (unique.length === 0) return null;

  return (
    <div className="sources">
      <div className="sources-title">Sources</div>
      {unique.map((source, index) => (
        <div key={index}>
          <div className="source-item">
            <span className="src-dot"></span>
            <span>
              <span className="src-title">{source.document_title || source.document_id || "Untitled source"}</span>
              {source.location ? <span className="src-loc"> &middot; {source.location}</span> : null}
              {source.visual_object_type ? <span className="src-loc"> &middot; {source.visual_object_type}</span> : null}
            </span>
          </div>
          {source.image_path ? <SourcePreviewImage apiBase={apiBase} source={source} /> : null}
        </div>
      ))}
    </div>
  );
}
