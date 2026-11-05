export default function TopBar({ view, onView, onPickFiles }) {
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-icon">📷</div>
        <span className="brand-text">IG RIP</span>
      </div>
      <div className="topbar-actions">
        <div className="view-toggle">
          <button
            type="button"
            className={view === "instagram" ? "active" : ""}
            onClick={() => onView("instagram")}
          >
            Instagram
          </button>
          <button
            type="button"
            className={view === "log" ? "active" : ""}
            onClick={() => onView("log")}
          >
            Log
          </button>
          <button
            type="button"
            className={view === "download" ? "active" : ""}
            onClick={() => onView("download")}
          >
            Download
          </button>
        </div>
        <label className="btn btn-secondary btn-sm">
          📁 Load JSON
          <input
            type="file"
            accept=".json,application/json"
            multiple
            hidden
            onChange={(event) => {
              onPickFiles(event.target.files);
              event.target.value = "";
            }}
          />
        </label>
      </div>
    </header>
  );
}
