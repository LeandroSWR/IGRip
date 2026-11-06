# IG RIP

IG RIP is a personal Instagram library. It downloads posts, reels, images, and carousels, then shows them in a browser dashboard that runs from the same Python process.

This project started from [IGbulkDL](https://github.com/doncezart/IGbulkDL) by [Don Cezar](https://github.com/doncezart). Reading a saved collection follows the same idea as his Tampermonkey script, [IG Bulk Collector](https://github.com/doncezart/IGbulkCollector): open the page while logged in, scroll, and collect the post and reel links. IG RIP does that inside the dashboard. The userscript is still useful for Explore or someone else's profile.

---

## What you get

The dashboard has three views:

- **Instagram** shows downloaded posts as a grid. Videos use a still frame, so the page does not load every file at once. A carousel lives in one folder named with the post's shortcode.
- **Log** is a filterable table of the same posts.
- **Download** starts a run, remembers your folders and cookies file, and can pull one saved collection at a time.

A command-line run is still there for a URL list you already have.

---

## Credit

- [Don Cezar](https://github.com/doncezart) created [IGbulkDL](https://github.com/doncezart/IGbulkDL), the downloader this project started from.
- He also created [IG Bulk Collector](https://github.com/doncezart/IGbulkCollector), the original Tampermonkey bulk post collector.

---

## Requirements

- Python 3.10+
- ffmpeg on `PATH`, so yt-dlp can merge video and audio and the grid can make stills
- The Python packages in `requirements.txt`: yt-dlp, instaloader, and Playwright

```
pip install -r requirements.txt
python -m playwright install chromium
```

Playwright's Chromium is only needed for **Refresh** and **Download** on a saved collection. A URL-file download does not open a browser.

For tests: `pip install -r requirements-dev.txt`

Node is only needed if you change the React page in `dashboard/src` and rebuild `dashboard/dist`. The running app does not use Node. The server opens `dashboard/dist` when that build is present, and falls back to `ig_dashboard.html` when it is not.

---

## Dashboard

```
python ig_download.py --dashboard
```

On this PC that listens on `127.0.0.1`, port 8765 (or the next free port up to 8774), and opens the page. Press Ctrl+C in that console to stop it.

The Download tab remembers the output folder, cookies file, cookies mode, filename template, and last URL file in `ig_dashboard_settings.json`. That file is gitignored. **Retry previously failed** starts checked. Dry run starts unchecked.

**Refresh** on Saved collections opens your Instagram saved page with the cookies file and lists each collection. Next to the name it shows how many posts from that collection are already in the library. **Download** on a row scrolls that collection, writes `Collections/<name>.txt`, and downloads into `<output>/<name>/`. Cookies are sent for every post in a saved-collection run. The log for a collection is `<output>/<name>/<name>.json`, beside the files.

---

## Cookies

Export a Netscape `cookies.txt` while logged into instagram.com. The extension [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) can do that. The file must include the `sessionid` cookie.

| Mode | Behaviour |
|---|---|
| `fallback` (default) | Try without cookies. Send them only when a post is age-restricted or private. |
| `always` | Send cookies on every request. |
| `never` | Ignore the cookies file. |

A saved-collection download always sends cookies, because that page is only visible while logged in.

Treat `cookies.txt` as the account password. Anyone with it can use the account. Never commit it, paste it into a chat, or attach it to an issue. If it leaks, log out of Instagram everywhere and export a new file.

---

## CLI

```
python ig_download.py <url-file> <collection-name> [options]
```

```
--cookies FILE         Path to cookies.txt
--cookies-mode MODE    fallback (default) | always | never
--output-dir DIR       Base folder (default: downloads). Each collection is a subfolder.
--log FILE             Override the log path
--filename-template T  Saved filename (default: {shortcode})
--retry-failed         Try URLs that previously failed
--dry-run              Read the post only, do not download
--host ADDR            Dashboard listen address (default: 127.0.0.1)
--data-dir DIR         Folder for the library, cookies, and settings
```

```
python ig_download.py my-saves.txt design --cookies cookies.txt
```

One URL per line. Blank lines and lines starting with `#` are ignored. See `example.txt`.

---

## How posts are saved

- Videos and reels go through yt-dlp and are merged to mp4 when ffmpeg is available.
- A single image goes through instaloader.
- A carousel is a folder named with the shortcode. Video slides are downloaded by yt-dlp. Photo slides are saved from the images yt-dlp already listed for that post. Posts that were already saved are skipped. `--retry-failed` tries the ones that failed.
- The log records status, author, media type, path, and whether cookies were used. Writes are atomic. A log that cannot be parsed is moved aside instead of being overwritten.

Filename variables:

| Variable | Meaning |
|---|---|
| `{shortcode}` | Post shortcode |
| `{author}` | Uploader username |
| `{title}` | Title reported for the post |
| `{upload_date}` | Upload date, `YYYYMMDD` |
| `{date}` | Today's date, `YYYY-MM-DD` |
| `{index}` | Position in this run |
| `{index:04d}` | That position, zero-padded |

---

## Home server

The dashboard can listen on the LAN and store the library on another disk. On this PC, leave these unset.

```
IGRIP_HOST=0.0.0.0
IGRIP_DATA=/data
python ig_download.py --dashboard
```

`--host` and `--data-dir` do the same thing. `IGRIP_DATA` is the folder the app may read and write in addition to your user home: downloads, cookies, URL lists, and settings. The library scan includes it, so collections on that disk show up in the grid. Relative paths are placed inside it. The printed address is a LAN address other devices can open, and the process does not try to launch a browser.

There is no login on the page. Do not publish that port past your home network. Anyone who can open it can see the library and start a download with the cookies file.

### TrueNAS Scale custom app

TrueNAS installs a container image. It does not build this repository. Pushing `main` or `nas-automation` publishes `ghcr.io/leandroswr/igrip:latest`. The first package GitHub creates is private: open the package settings and make it public, or the NAS pull fails.

In **Install Custom App**, leave Version as TrueNAS shows it. Use these values:

| Field | Value |
|---|---|
| Application name | `igrip` |
| Image repository | `ghcr.io/leandroswr/igrip` |
| Tag | `latest` |
| Pull policy | Always, so an update pulls the new image |
| Timezone | `Europe/Lisbon` (or yours) |
| Restart policy | Unless stopped |
| Host network | off |
| Command | empty. The image already starts the dashboard |

Environment variables:

| Name | Value |
|---|---|
| `IGRIP_HOST` | `0.0.0.0` |
| `IGRIP_DATA` | `/data` |

Port: container `8765`, host `8765`, TCP.

Portal: name `IG RIP`, protocol HTTP, port `8765`. That adds the web button in the TrueNAS apps list.

Storage: one host path. Mount your dataset at `/data`. Put `cookies.txt` in that dataset. In the Download tab, set the cookies file to `/data/cookies.txt` and the output folder to `/data` (or a folder inside it). The library, settings, and downloads stay on the dataset when you update the image.

Leave Privileged and Custom User off. Chromium is started with the flags a container needs. Do not forward host port 8765 from your router.

---

## Tests

```
python -m pytest tests -q
```

The tests do not call Instagram.

---

## Notes

- This is for personal archival use. Respect Instagram's terms and the rights of the people who made the posts.
- Instagram changes often. If a public video stops downloading, update yt-dlp first: `pip install -U yt-dlp`.
- `.gitignore` keeps `cookies.txt`, URL lists, downloads, and `*.json` logs out of git. Check `git status` before you commit.

---

## License

[MIT](LICENSE). IGbulkDL is © 2026 Don Cezar.
