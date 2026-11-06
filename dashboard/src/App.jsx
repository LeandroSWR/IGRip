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
  const [account, setAccount] = useState("");
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

  const collectionCount = new Set(
    items.filter((item) => item.collection).map((item) => `${item.account || ""}\u0000${item.collection}`),
  ).size;

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
            account={account}
            onAccount={setAccount}
            collection={collection}
            onCollection={setCollection}
            onOpen={setOpenItem}
          />
        ) : (
          <LogView
            items={items.filter((item) => {
              if (account && item.account !== account) return false;
              if (!collection) return true;
              const [selectedAccount, selectedName] = collection.split("\u0000");
              return (item.account || "") === selectedAccount && (item.collection || "Collection") === selectedName;
            })}
            onOpen={setOpenItem}
          />
        )}
      </main>
      {openItem && <PostModal item={openItem} onClose={() => setOpenItem(null)} />}
    </>
  );
}
