import { useCallback, useEffect, useState } from "react";
import { dedupeItems, loadLibrary, readLogFiles } from "./api.js";
import DownloadView from "./components/DownloadView.jsx";
import InstagramView from "./components/InstagramView.jsx";
import LogView from "./components/LogView.jsx";
import PostModal from "./components/PostModal.jsx";
import TopBar from "./components/TopBar.jsx";

export default function App() {
  const [items, setItems] = useState([]);
  const [view, setView] = useState("instagram");
  const [collection, setCollection] = useState("");
  const [openItem, setOpenItem] = useState(null);
  const [message, setMessage] = useState("Loading collections…");

  const refreshLibrary = useCallback(() => {
    return loadLibrary()
      .then((loaded) => {
        setItems(loaded);
        setMessage(loaded.length ? "" : "No collection logs found yet.");
      })
      .catch(() => setMessage("Could not reach the dashboard server."));
  }, []);

  useEffect(() => {
    refreshLibrary();
  }, []);

  async function onPickFiles(fileList) {
    if (!fileList?.length) return;
    try {
      const extra = await readLogFiles(fileList);
      setItems((current) => dedupeItems([...current, ...extra]));
      setMessage("");
    } catch (error) {
      setMessage(error.message);
    }
  }

  const collectionCount = new Set(items.map((item) => item.collection).filter(Boolean)).size;

  return (
    <>
      <TopBar view={view} onView={setView} onPickFiles={onPickFiles} />
      <main>
        <p className="loaded-hint">
          {message || `${items.length} posts across ${collectionCount} collection${collectionCount === 1 ? "" : "s"}`}
        </p>
        {view === "download" ? (
          <DownloadView onLibraryChanged={refreshLibrary} />
        ) : view === "instagram" ? (
          <InstagramView
            items={items}
            collection={collection}
            onCollection={setCollection}
            onOpen={setOpenItem}
          />
        ) : (
          <LogView
            items={collection ? items.filter((item) => item.collection === collection) : items}
            onOpen={setOpenItem}
          />
        )}
      </main>
      {openItem && <PostModal item={openItem} onClose={() => setOpenItem(null)} />}
    </>
  );
}
