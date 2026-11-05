/** Load every collection log the Python server can see. */

export async function loadLibrary() {
  const response = await fetch("/api/collections", { cache: "no-store" });
  if (!response.ok) {
    throw new Error("Could not load collections");
  }
  const data = await response.json();
  const items = [];
  for (const collection of data.collections || []) {
    for (const href of collection.logs || []) {
      const logResponse = await fetch(href);
      if (!logResponse.ok) continue;
      let log;
      try {
        log = await logResponse.json();
      } catch {
        continue;
      }
      for (const item of log.items || []) {
        items.push({ ...item, collection: item.collection || collection.name });
      }
    }
  }
  return dedupeItems(items);
}

export function dedupeItems(items) {
  const byKey = new Map();
  for (const item of items) {
    const key = `${item.collection || ""}\u001f${item.url || ""}`;
    byKey.set(key, { ...item, key });
  }
  return [...byKey.values()];
}

export async function readLogFiles(fileList) {
  const items = [];
  for (const file of fileList) {
    const text = await file.text();
    const data = JSON.parse(text);
    if (!Array.isArray(data.items)) {
      throw new Error(`${file.name} has no items list`);
    }
    const collection = file.name.replace(/\.json$/i, "");
    for (const item of data.items) {
      items.push({ ...item, collection: item.collection || collection });
    }
  }
  return items;
}

export async function loadSettings() {
  const response = await fetch("/api/settings", { cache: "no-store" });
  if (!response.ok) throw new Error("Could not load saved preferences.");
  return response.json();
}

export async function saveSettings(settings) {
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Could not save preferences.");
  return data;
}

export async function browseFolder(path) {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  const response = await fetch(`/api/browse${query}`, { cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Could not open that folder.");
  return data;
}

export async function startDownload(body) {
  const response = await fetch("/api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Could not start the download.");
  return data;
}

export async function stopDownload() {
  const response = await fetch("/api/download/stop", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Could not stop the download.");
  return data;
}

export async function loadSavedCollections() {
  const response = await fetch("/api/saved/collections", { cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Could not read saved collections.");
  return data;
}

export async function startSavedDownload(body) {
  const response = await fetch("/api/saved/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Could not start that collection.");
  return data;
}

export async function downloadStatus(since) {
  const response = await fetch(`/api/download/status?since=${since}`, { cache: "no-store" });
  if (!response.ok) throw new Error("Could not read download progress.");
  return response.json();
}
