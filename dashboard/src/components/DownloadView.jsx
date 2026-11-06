import { useEffect, useRef, useState } from "react";
import {
  browseFolder,
  downloadStatus,
  loadSavedCollections,
  refreshSavedCollections,
  loadSettings,
  saveSettings,
  startDownload,
  startSavedDownload,
  stopDownload,
} from "../api.js";

const TEMPLATES = [
  "{shortcode}",
  "{author}_{shortcode}",
  "{date}_{shortcode}",
  "{index:04d}_{shortcode}",
  "{upload_date}_{shortcode}",
  "{author}_{upload_date}_{shortcode}",
];

function lineFor(event) {
  switch (event.type) {
    case "collecting":
      return event.message || `Reading the collection… ${event.found || 0} posts`;
    case "start":
      return `Starting ${event.to_process} of ${event.total} posts`;
    case "downloading":
      return `[${event.index + 1}/${event.total}] ${event.shortcode || event.url}`;
    case "progress": {
      const who = event.author ? ` ${event.author}` : "";
      const detail = event.status === "failed" && event.error_category ? ` (${event.error_category})` : "";
      return `  ${event.status}${who}${detail}`;
    }
    case "cookie_fallback":
      return `  retrying ${event.shortcode || "post"} with cookies`;
    case "skipped_disk":
      return `  ${event.shortcode} already on disk`;
    case "skip_info":
      return `Skipping ${event.skipped} already logged, ${event.remaining} remaining`;
    case "rate_limited":
      return `Rate limited, waiting ${event.wait_seconds}s`;
    case "rate_limit_abort":
      return "Stopped after repeated rate limits";
    case "warning":
      return event.message;
    case "stopped":
      return "Stopped";
    case "error":
      return event.message;
    case "done": {
      const summary = event.summary || {};
      return `Done. OK ${summary.ok || 0}, failed ${summary.failed || 0}`;
    }
    default:
      return "";
  }
}

function CollectionCount({ item }) {
  const have = item.saved ?? 0;
  const failed = item.failed ?? 0;
  const total = Number.isFinite(item.count) ? item.count : null;
  return (
    <div className="saved-count">
      {total != null ? `${have}/${total} downloaded` : `${have} downloaded`}
      {failed > 0 && <span className="saved-failed"> · {failed} failed</span>}
    </div>
  );
}

export default function DownloadView({ onLibraryChanged }) {
  const [form, setForm] = useState(null);
  const [collection, setCollection] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [retryFailed, setRetryFailed] = useState(true);
  const [message, setMessage] = useState("");
  const [running, setRunning] = useState(false);
  const [lines, setLines] = useState([]);
  const [progress, setProgress] = useState({ index: 0, total: 0, ok: 0, failed: 0 });
  const [saved, setSaved] = useState([]);
  const [savedUser, setSavedUser] = useState("");
  const [savedMessage, setSavedMessage] = useState("");
  const [loadingSaved, setLoadingSaved] = useState(false);
  const [picker, setPicker] = useState(null);
  const [listing, setListing] = useState(null);
  const since = useRef(0);
  const logRef = useRef(null);
  const finished = useRef(false);

  useEffect(() => {
    loadSettings()
      .then(setForm)
      .catch((error) => setMessage(error.message));
  }, []);

  useEffect(() => {
    if (!form) return undefined;
    let cancel = false;
    loadSavedCollections()
      .then((data) => {
        if (cancel) return;
        setSaved(data.collections || []);
        setSavedUser(data.username || "");
      })
      .catch((error) => {
        if (!cancel) setSavedMessage(error.message);
      });
    return () => {
      cancel = true;
    };
  }, [form?.cookies]);

  useEffect(() => {
    if (!running) return undefined;
    let timer = 0;
    let cancelled = false;

    async function poll() {
      try {
        const status = await downloadStatus(since.current);
        if (cancelled) return;
        since.current = status.next;
        if (status.events?.length) {
          const extra = status.events.map(lineFor).filter(Boolean);
          setLines((current) => [...current, ...extra]);
          const last = [...status.events].reverse().find((event) => event.total);
          if (last) {
            setProgress((current) => ({
              index: last.index != null ? last.index + 1 : current.index,
              total: last.total || current.total,
              ok: current.ok + status.events.filter((event) => event.status === "ok").length,
              failed: current.failed + status.events.filter((event) => event.status === "failed").length,
            }));
          }
          if (status.events.some((event) => event.type === "done" || event.type === "stopped" || event.type === "error")) {
            finished.current = true;
          }
        }
        setRunning(status.running);
        if (!status.running && finished.current) {
          finished.current = false;
          onLibraryChanged();
          const fresh = await loadSavedCollections();
          if (!cancelled) {
            setSaved(fresh.collections || []);
            setSavedUser(fresh.username || "");
          }
        }
      } catch (error) {
        if (!cancelled) setMessage(error.message);
      }
      if (!cancelled) timer = window.setTimeout(poll, 800);
    }

    poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [running, onLibraryChanged]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines]);

  function update(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function remember(next) {
    setForm(next);
    try {
      const saved = await saveSettings(next);
      setForm(saved);
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function openPicker(field, mode) {
    setPicker({ field, mode });
    setMessage("");
    try {
      const start = form?.[field] || "";
      setListing(await browseFolder(start || undefined));
    } catch (error) {
      setListing(null);
      setMessage(error.message);
    }
  }

  async function enterDir(path) {
    try {
      setListing(await browseFolder(path));
    } catch (error) {
      setMessage(error.message);
    }
  }

  function chooseFile(path) {
    const next = { ...form, [picker.field]: path };
    setPicker(null);
    remember(next);
  }

  function chooseFolder() {
    const next = { ...form, [picker.field]: listing.path };
    setPicker(null);
    remember(next);
  }

  async function refreshSaved() {
    setLoadingSaved(true);
    setSavedMessage("");
    try {
      const data = await refreshSavedCollections();
      setSaved(data.collections || []);
      setSavedUser(data.username || "");
      setSavedMessage(data.collections?.length ? "" : "No saved collections on that page.");
    } catch (error) {
      setSavedMessage(error.message);
    } finally {
      setLoadingSaved(false);
    }
  }

  async function downloadSaved(item) {
    setCollection(item.name || "");
    setMessage("");
    setLines([]);
    setProgress({ index: 0, total: 0, ok: 0, failed: 0 });
    since.current = 0;
    finished.current = false;
    try {
      await startSavedDownload({ url: item.url, name: item.name, retry_failed: retryFailed });
      setRunning(true);
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function onStart() {
    setMessage("");
    setLines([]);
    setProgress({ index: 0, total: 0, ok: 0, failed: 0 });
    since.current = 0;
    finished.current = false;
    try {
      await startDownload({
        ...form,
        collection,
        dry_run: dryRun,
        retry_failed: retryFailed,
      });
      setRunning(true);
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function onStop() {
    try {
      await stopDownload();
    } catch (error) {
      setMessage(error.message);
    }
  }

  if (!form) {
    return <p className="loaded-hint">{message || "Loading preferences…"}</p>;
  }

  const pct = progress.total ? Math.min(100, Math.round((progress.index / progress.total) * 100)) : 0;

  return (
    <section className="download">
      <div className="saved-head">
        <h2>Saved collections{savedUser ? ` · ${savedUser}` : ""}</h2>
        <button type="button" className="btn btn-secondary" disabled={running || loadingSaved} onClick={refreshSaved}>
          {loadingSaved ? "Reading page…" : "Refresh"}
        </button>
      </div>
      {savedMessage && <p className="form-error">{savedMessage}</p>}
      {saved.length > 0 && (
        <div className="saved-list">
          {saved.map((item) => (
            <div className="saved-row" key={item.url}>
              <div>
                <div className="saved-name">{item.name}</div>
                <CollectionCount item={item} />
              </div>
              <button type="button" className="btn btn-primary" disabled={running || loadingSaved} onClick={() => downloadSaved(item)}>
                Download
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="download-grid">
        <label>
          URL file
          <div className="path-row">
            <input value={form.url_file || ""} onChange={(event) => update("url_file", event.target.value)} onBlur={(event) => remember({ ...form, url_file: event.target.value })} />
            <button type="button" className="btn btn-secondary" onClick={() => openPicker("url_file", "file")}>Browse</button>
          </div>
        </label>
        <label>
          Collection name
          <input value={collection} onChange={(event) => setCollection(event.target.value)} placeholder="Haircut" />
        </label>
        <label>
          Output folder
          <div className="path-row">
            <input value={form.output_dir || ""} onChange={(event) => update("output_dir", event.target.value)} onBlur={(event) => remember({ ...form, output_dir: event.target.value })} />
            <button type="button" className="btn btn-secondary" onClick={() => openPicker("output_dir", "dir")}>Browse</button>
          </div>
        </label>
        <label>
          Cookies file
          <div className="path-row">
            <input value={form.cookies || ""} onChange={(event) => update("cookies", event.target.value)} onBlur={(event) => remember({ ...form, cookies: event.target.value })} placeholder="Optional" />
            <button type="button" className="btn btn-secondary" onClick={() => openPicker("cookies", "file")}>Browse</button>
          </div>
        </label>
        <label>
          Cookies mode
          <select value={form.cookies_mode || "fallback"} onChange={(event) => remember({ ...form, cookies_mode: event.target.value })}>
            <option value="fallback">fallback</option>
            <option value="always">always</option>
            <option value="never">never</option>
          </select>
        </label>
        <label>
          Filename template
          <input
            list="filename-templates"
            value={form.filename_template || "{shortcode}"}
            onChange={(event) => update("filename_template", event.target.value)}
            onBlur={(event) => remember({ ...form, filename_template: event.target.value })}
          />
          <datalist id="filename-templates">
            {TEMPLATES.map((template) => <option key={template} value={template} />)}
          </datalist>
        </label>
      </div>

      <div className="download-options">
        <label className="check"><input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} /> Dry run</label>
        <label className="check"><input type="checkbox" checked={retryFailed} onChange={(event) => setRetryFailed(event.target.checked)} /> Retry previously failed</label>
      </div>

      <div className="download-actions">
        <button type="button" className="btn btn-primary" disabled={running} onClick={onStart}>Start download</button>
        <button type="button" className="btn btn-secondary" disabled={!running} onClick={onStop}>Stop</button>
        {message && <span className="form-error">{message}</span>}
      </div>

      <div className="progress-block">
        <div className="progress-meta">
          <span>{running ? "Downloading" : lines.length ? "Idle" : "Ready"}</span>
          <span>OK {progress.ok} · Failed {progress.failed}{progress.total ? ` · ${pct}%` : ""}</span>
        </div>
        <div className="progress-track"><div className="progress-fill" style={{ width: `${pct}%` }} /></div>
        <pre ref={logRef} className="download-log">{lines.join("\n")}</pre>
      </div>

      {picker && listing && (
        <div className="modal-overlay" onClick={() => setPicker(null)}>
          <div className="modal-box picker" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <strong className="mono">{listing.path}</strong>
              <button type="button" className="modal-close" onClick={() => setPicker(null)} aria-label="Close">×</button>
            </div>
            <div className="picker-list">
              {listing.parent && (
                <button type="button" onClick={() => enterDir(listing.parent)}>..</button>
              )}
              {listing.dirs.map((dir) => (
                <button type="button" key={dir.path} onClick={() => enterDir(dir.path)}>{dir.name}/</button>
              ))}
              {picker.mode === "file" && listing.files.map((file) => (
                <button type="button" key={file.path} onClick={() => chooseFile(file.path)}>{file.name}</button>
              ))}
            </div>
            {picker.mode === "dir" && (
              <div className="modal-body">
                <button type="button" className="btn btn-primary" onClick={chooseFolder}>Use this folder</button>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
