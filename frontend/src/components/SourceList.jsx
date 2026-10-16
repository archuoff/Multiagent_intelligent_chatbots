import { useState } from "react";
import { dedupeSources, visualAssetUrl } from "../lib/api";
import Lightbox from "./Lightbox";

function SourcePreviewImage({ apiBase, source, onEnlarge }) {
  const [failed, setFailed] = useState(false);
  const assetUrl = visualAssetUrl(apiBase, source.image_path);
  const label = source.visual_object_type || "Figure";

  if (failed) {
    return (
      <div className="visual-preview visual-preview-failed">
        <span className="visual-preview-fallback-icon">
          <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.3">
            <rect x="1.5" y="2.5" width="13" height="11" rx="2" />
            <circle cx="5.3" cy="6.3" r="1.1" />
            <path d="M2 11.5 5.8 8l2.4 2.2L11 7l3 3.8" />
          </svg>
        </span>
        <span>Image unavailable</span>
      </div>
    );
  }

  return (
    <button type="button" className="visual-preview" onClick={() => onEnlarge(assetUrl, label)}>
      <img
        src={assetUrl}
        alt={label}
        loading="lazy"
        onError={() => setFailed(true)}
      />
      <div className="visual-caption">{label}</div>
    </button>
  );
}

function sourceLabel(source, index) {
  if (source.document_title) return source.document_title;
  return "Source " + (index + 1);
}

export default function SourceList({ apiBase, sources }) {
  const unique = dedupeSources(sources);
  const [lightbox, setLightbox] = useState(null);
  if (unique.length === 0) return null;

  function openLightbox(src, alt) {
    setLightbox({ src, alt });
  }

  const withImages = unique.filter((source) => source.image_path);

  return (
    <div className="sources">
      <div className="sources-title">Sources</div>
      <div className="source-chip-row">
        {unique.map((source, index) => (
          <div key={index} id={"source-chip-" + (index + 1)} className="source-chip" title={source.document_id || ""}>
            <span className="source-chip-index">{index + 1}</span>
            <span className="source-chip-text">
              {sourceLabel(source, index)}
              {source.location ? <span className="source-chip-loc"> &middot; {source.location}</span> : null}
            </span>
          </div>
        ))}
      </div>
      {withImages.length ? (
        <div className="visual-preview-grid">
          {withImages.map((source, index) => (
            <SourcePreviewImage key={index} apiBase={apiBase} source={source} onEnlarge={openLightbox} />
          ))}
        </div>
      ) : null}
      {lightbox ? <Lightbox src={lightbox.src} alt={lightbox.alt} onClose={() => setLightbox(null)} /> : null}
    </div>
  );
}
