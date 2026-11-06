import { useMemo, useState } from "react";

const PAGE_SIZE = 100;

export default function LogView({ items, onOpen }) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [author, setAuthor] = useState("");
  const [type, setType] = useState("");
  const [error, setError] = useState("");
  const [sortCol, setSortCol] = useState("timestamp");
  const [sortDir, setSortDir] = useState(-1);
  const [page, setPage] = useState(1);

  const authors = useMemo(
    () => [...new Set(items.map((item) => item.author || item.username).filter(Boolean))].sort(),
    [items],
  );
  const errors = useMemo(
    () => [...new Set(items.map((item) => item.error_category).filter(Boolean))].sort(),
    [items],
  );

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    const rows = items.filter((item) => {
      if (status && item.status !== status) return false;
      if (author && (item.author || item.username) !== author) return false;
      if (type && item.media_type !== type && !(type === "image" && item.media_type === "image_only")) return false;
      if (error && item.error_category !== error) return false;
      if (!query) return true;
      const haystack = [
        item.shortcode, item.author, item.username, item.title, item.caption,
        item.error_detail, item.error_category, item.saved_path, item.collection, item.account,
      ].join(" ").toLowerCase();
      return haystack.includes(query);
    });
    rows.sort((a, b) => compare(a[sortCol], b[sortCol]) * sortDir);
    return rows;
  }, [items, search, status, author, type, error, sortCol, sortDir]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount);
  const slice = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  function toggleSort(column) {
    if (sortCol === column) setSortDir((dir) => dir * -1);
    else {
      setSortCol(column);
      setSortDir(-1);
    }
    setPage(1);
  }

  return (
    <section>
      <Stats items={items} />
      <div className="filter-bar">
        <input
          type="text"
          value={search}
          placeholder="Search shortcode, author, caption, error…"
          onChange={(event) => { setSearch(event.target.value); setPage(1); }}
        />
        <Filter label="Status" value={status} onChange={setStatus} options={[["", "All"], ["ok", "OK"], ["failed", "Failed"], ["dry_run", "Dry run"]]} />
        <Filter label="Author" value={author} onChange={setAuthor} options={[["", "All"], ...authors.map((name) => [name, name])]} />
        <Filter label="Type" value={type} onChange={setType} options={[["", "All"], ["video", "Video"], ["carousel", "Carousel"], ["image", "Image"], ["unknown", "Unknown"]]} />
        <Filter label="Error" value={error} onChange={setError} options={[["", "All"], ...errors.map((name) => [name, name])]} />
        <button type="button" className="clear-btn" onClick={() => { setSearch(""); setStatus(""); setAuthor(""); setType(""); setError(""); setPage(1); }}>
          ✕ Clear
        </button>
        <span className="results-info">{filtered.length} of {items.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <Sortable label="Author / Caption" column="author" sortCol={sortCol} sortDir={sortDir} onSort={toggleSort} />
              <Sortable label="Shortcode" column="shortcode" sortCol={sortCol} sortDir={sortDir} onSort={toggleSort} />
              <Sortable label="Type" column="media_type" sortCol={sortCol} sortDir={sortDir} onSort={toggleSort} />
              <Sortable label="Status" column="status" sortCol={sortCol} sortDir={sortDir} onSort={toggleSort} />
              <Sortable label="Error" column="error_category" sortCol={sortCol} sortDir={sortDir} onSort={toggleSort} />
              <Sortable label="Date" column="timestamp" sortCol={sortCol} sortDir={sortDir} onSort={toggleSort} />
              <th></th>
            </tr>
          </thead>
          <tbody>
            {slice.map((item, index) => (
              <tr key={item.key || index}>
                <td>{(safePage - 1) * PAGE_SIZE + index + 1}</td>
                <td>
                  <div>{item.author || item.username || "—"}</div>
                  {item.account && (
                    <div className="caption">{item.collection ? `${item.account} / ${item.collection}` : item.account}</div>
                  )}
                  {item.caption && <div className="caption">{item.caption}</div>}
                </td>
                <td className="mono">{item.shortcode || "—"}</td>
                <td>{item.media_type || "—"}</td>
                <td>{item.status || "—"}</td>
                <td>{item.error_category || "—"}</td>
                <td>{item.timestamp ? new Date(item.timestamp).toLocaleString() : "—"}</td>
                <td>
                  {item.status === "ok" && item.saved_path ? (
                    <button type="button" className="btn-icon" onClick={() => onOpen(item)}>▶ Play</button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <div className="pagination">
          <button type="button" disabled={safePage === 1} onClick={() => setPage(safePage - 1)}>‹ Prev</button>
          <span>{safePage} / {pageCount}</span>
          <button type="button" disabled={safePage === pageCount} onClick={() => setPage(safePage + 1)}>Next ›</button>
        </div>
      )}
    </section>
  );
}

function Stats({ items }) {
  const count = (predicate) => items.filter(predicate).length;
  const cards = [
    ["Total", items.length, ""],
    ["Downloaded", count((item) => item.status === "ok"), "ok"],
    ["Failed", count((item) => item.status === "failed"), "fail"],
    ["Videos", count((item) => item.media_type === "video"), ""],
    ["Images", count((item) => item.media_type === "image" || item.media_type === "image_only"), ""],
    ["Carousels", count((item) => item.media_type === "carousel"), ""],
  ];
  return (
    <div className="stats-grid">
      {cards.map(([label, value, tone]) => (
        <div key={label} className={`stat-card ${tone}`}>
          <div className="sc-label">{label}</div>
          <div className="sc-value">{value}</div>
        </div>
      ))}
    </div>
  );
}

function Filter({ label, value, onChange, options }) {
  return (
    <label>
      {label}
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([optionValue, optionLabel]) => (
          <option key={optionValue || "all"} value={optionValue}>{optionLabel}</option>
        ))}
      </select>
    </label>
  );
}

function Sortable({ label, column, sortCol, sortDir, onSort }) {
  const arrow = sortCol === column ? (sortDir < 0 ? " ↓" : " ↑") : "";
  return (
    <th>
      <button type="button" className="sort-btn" onClick={() => onSort(column)}>
        {label}{arrow}
      </button>
    </th>
  );
}

function compare(left, right) {
  const a = left ?? "";
  const b = right ?? "";
  if (typeof a === "number" || (!Number.isNaN(Number(a)) && !Number.isNaN(Number(b)) && a !== "" && b !== "")) {
    return Number(a) - Number(b);
  }
  return String(a).localeCompare(String(b));
}
