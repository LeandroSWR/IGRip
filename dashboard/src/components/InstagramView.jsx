import { useState } from "react";
import { coverPath, isCarousel, isImagePath, slidePaths, tileUrl } from "../media.js";

export default function InstagramView({ items, collection, onCollection, onOpen }) {
  const names = [...new Set(items.map((item) => item.collection).filter(Boolean))];
  const shown = collection ? items.filter((item) => item.collection === collection) : items;
  const groups = new Map();
  for (const item of shown) {
    if (item.status !== "ok") continue;
    const name = item.collection || "Collection";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }

  return (
    <section>
      <div className="ig-nav">
        <button
          type="button"
          className={`ig-pill ${collection ? "" : "active"}`}
          onClick={() => onCollection("")}
        >
          All
        </button>
        {names.map((name) => {
          const count = items.filter((item) => item.collection === name && item.status === "ok").length;
          return (
            <button
              key={name}
              type="button"
              className={`ig-pill ${collection === name ? "active" : ""}`}
              onClick={() => onCollection(collection === name ? "" : name)}
            >
              {name} <span className="ig-meta">{count}</span>
            </button>
          );
        })}
      </div>
      {[...groups.entries()].map(([name, posts]) => (
        <section key={name} className="ig-collection">
          <div className="ig-head">
            <div className="ig-avatar">{(name.trim()[0] || "?").toUpperCase()}</div>
            <div>
              <div className="ig-name">{name}</div>
              <div className="ig-meta">{posts.length} post{posts.length === 1 ? "" : "s"}</div>
            </div>
          </div>
          <div className="ig-grid">
            {posts.map((item) => (
              <Tile key={item.key} item={item} onOpen={onOpen} />
            ))}
          </div>
        </section>
      ))}
      {groups.size === 0 && <p className="empty">No saved posts in this view.</p>}
    </section>
  );
}

function Tile({ item, onOpen }) {
  const cover = coverPath(item);
  const [broken, setBroken] = useState(false);
  const slides = slidePaths(item);
  const mark = isCarousel(item) ? `▦ ${slides.length}` : isImagePath(cover) ? "" : "▶";

  return (
    <button type="button" className="ig-tile" onClick={() => onOpen(item)}>
      {cover && !broken ? (
        <img src={tileUrl(cover)} alt="" loading="lazy" decoding="async" onError={() => setBroken(true)} />
      ) : (
        <span className="ig-fallback">{item.shortcode || "▶"}</span>
      )}
      {mark && <span className="ig-mark">{mark}</span>}
    </button>
  );
}
