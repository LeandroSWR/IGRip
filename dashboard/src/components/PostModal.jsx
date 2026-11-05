import { useEffect, useState } from "react";
import { isImagePath, mediaUrl, slidePaths } from "../media.js";

export default function PostModal({ item, onClose }) {
  const slides = slidePaths(item);
  const [index, setIndex] = useState(0);
  const path = slides[index] || "";
  const many = slides.length > 1;

  useEffect(() => {
    function onKey(event) {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") setIndex((current) => step(current, -1, slides.length));
      if (event.key === "ArrowRight") setIndex((current) => step(current, 1, slides.length));
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, slides.length]);

  const title = `${item.collection ? `${item.collection} · ` : ""}${item.shortcode || "Post"}`;

  return (
    <div className="modal-overlay" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal-box">
        <div className="modal-header">
          <span className="modal-title">{title}</span>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modal-body">
          <div className="modal-stage">
            {many && (
              <>
                <button type="button" className="slide-nav prev" aria-label="Previous slide" onClick={() => setIndex((current) => step(current, -1, slides.length))}>‹</button>
                <button type="button" className="slide-nav next" aria-label="Next slide" onClick={() => setIndex((current) => step(current, 1, slides.length))}>›</button>
                <div className="slide-count">{index + 1} / {slides.length}</div>
              </>
            )}
            {isImagePath(path) ? (
              <img className="modal-image" src={mediaUrl(path)} alt="" />
            ) : (
              <video key={path} className="modal-video" src={mediaUrl(path)} controls preload="metadata" />
            )}
          </div>
          {item.caption && <div className="modal-caption">{item.caption}</div>}
          <dl className="modal-meta">
            <dt>URL</dt>
            <dd>{item.url ? <a href={item.url} target="_blank" rel="noreferrer">{item.url}</a> : "—"}</dd>
            <dt>Shortcode</dt>
            <dd>{item.shortcode || "—"}</dd>
            <dt>Author</dt>
            <dd>{item.author || item.username || "—"}</dd>
            <dt>Type</dt>
            <dd>{item.media_type || "—"}</dd>
            <dt>File</dt>
            <dd>{path || "—"}</dd>
          </dl>
        </div>
      </div>
    </div>
  );
}

function step(index, delta, count) {
  if (count < 2) return index;
  return (index + delta + count) % count;
}
