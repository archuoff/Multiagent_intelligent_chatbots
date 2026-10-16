import { useEffect } from "react";

// Full-screen overlay for viewing an image larger than its inline thumbnail.
// Shared by inline answer images and source-citation previews so clicking
// either opens the same enlarge experience. Closes on backdrop click, the
// close button, or Escape.
export default function Lightbox({ src, alt, onClose }) {
  useEffect(() => {
    function handleKeyDown(event) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  if (!src) return null;

  return (
    <div className="lightbox-backdrop" onClick={onClose}>
      <button className="lightbox-close" aria-label="Close" onClick={onClose}>
        &times;
      </button>
      <img className="lightbox-image" src={src} alt={alt || "Enlarged view"} onClick={(event) => event.stopPropagation()} />
    </div>
  );
}
