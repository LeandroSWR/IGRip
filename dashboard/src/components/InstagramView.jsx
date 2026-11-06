import { useState } from "react";
import { coverPath, isCarousel, isImagePath, slidePaths, tileUrl } from "../media.js";

function collectionKey(item) {
  return `${item.account || ""}\u0000${item.collection || "Collection"}`;
}

export default function InstagramView({ items, account, onAccount, collection, onCollection, onOpen }) {
  const accounts = [...new Set(items.map((item) => item.account).filter(Boolean))];
  const inAccount = account ? items.filter((item) => item.account === account) : items;
  const groups = new Map();
  for (const item of inAccount) {
    if (item.status !== "ok") continue;
    const key = collectionKey(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  const sections = [...groups.entries()].map(([key, posts]) => {
    const [accountName, collectionName] = key.split("\u0000");
    return { key, accountName, collectionName, posts };
  });
  const sharedNames = new Set(
    sections.map((section) => section.collectionName).filter((name, _index, all) => all.indexOf(name) !== all.lastIndexOf(name)),
  );
  const shown = collection ? sections.filter((section) => section.key === collection) : sections;

  function chooseAccount(next) {
    onAccount(next);
    onCollection("");
  }

  return (
    <section>
      <label className="ig-user">
        User
        <select value={account} onChange={(event) => chooseAccount(event.target.value)}>
          <option value="">All</option>
          {accounts.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </select>
      </label>
      <div className="ig-nav">
        <button
          type="button"
          className={`ig-pill ${collection ? "" : "active"}`}
          onClick={() => onCollection("")}
        >
          All
        </button>
        {sections.map((section) => (
          <button
            key={section.key}
            type="button"
            className={`ig-pill ${collection === section.key ? "active" : ""}`}
            onClick={() => onCollection(collection === section.key ? "" : section.key)}
          >
            {sharedNames.has(section.collectionName) && section.accountName
              ? `${section.accountName} / ${section.collectionName}`
              : section.collectionName}{" "}
            <span className="ig-meta">{section.posts.length}</span>
          </button>
        ))}
      </div>
      {shown.map((section) => (
        <section key={section.key} className="ig-collection">
          <div className="ig-head">
            <div className="ig-avatar">{(section.collectionName.trim()[0] || "?").toUpperCase()}</div>
            <div>
              <div className="ig-name">{section.collectionName}</div>
              <div className="ig-meta">
                {!account && section.accountName ? `${section.accountName} · ` : ""}
                {section.posts.length} post{section.posts.length === 1 ? "" : "s"}
              </div>
            </div>
          </div>
          <div className="ig-grid">
            {section.posts.map((item) => (
              <Tile key={item.key} item={item} onOpen={onOpen} />
            ))}
          </div>
        </section>
      ))}
      {shown.length === 0 && <p className="empty">No saved posts in this view.</p>}
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
