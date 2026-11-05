# igdl

![igdl screenshot](https://i.imgur.com/zeTc3GH.png)

A desktop tool for batch-downloading Instagram posts - videos, reels, image posts, and carousels - from a plain list of URLs. The browser dashboard is the app: it starts downloads, remembers your folders, and shows the library. A CLI remains for scripts.

---

## Files

| File | Purpose |
|---|---|
| `ig_download.py` | Core download engine. Works standalone as a CLI. |
| `dashboard/` | Browser app. React source, built into `dashboard/dist`. |
| `ig_dashboard.html` | Older single-file viewer, used only if the React build is missing. |
| `example.txt` | Example URL file format. |
| `requirements.txt` | Python dependencies (`yt-dlp`, `instaloader`). |
| `requirements-dev.txt` | Test dependencies (`pytest`). |
| `tests/` | Pytest suite for the core logic (no network needed). |
| `.gitignore` | Keeps cookies, sessions, logs and downloads out of git. |
| `LICENSE` | MIT license. |

---

## Requirements

- **Python 3.10+** (required by current `yt-dlp`; `instaloader` needs 3.9+)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [instaloader](https://github.com/instaloader/instaloader)
- [ffmpeg](https://ffmpeg.org/download.html) on your `PATH` — yt-dlp needs it to merge best video + audio. Without it, video/reel downloads may be single-stream or fail; the tool warns at startup.

```
pip install -r requirements.txt
```

For development and tests: `pip install -r requirements-dev.txt`

### Authentication

The tool downloads **without cookies by default**. A `cookies.txt` is used only as a **fallback**: if a post cannot be downloaded because it is **age-restricted** or **private / login-gated**, that URL is retried once with your cookies. Public content never sends cookies.

`--cookies-mode` (or **Cookies mode** on the Download tab) controls this:

| Mode | Behaviour |
|---|---|
| `fallback` (default) | Try without cookies; retry with cookies only for age/private restricted posts |
| `always` | Send cookies on every request (e.g. to reduce rate-limiting on huge batches) |
| `never` | Ignore the cookies file entirely |

Cookies work for both downloaders: **yt-dlp** (videos, reels, carousels) and **instaloader** (image posts).

To supply cookies, export a `cookies.txt` (Netscape format) using a browser extension such as [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc), then pass it with `--cookies`.

> Treat `cookies.txt` as a password — anyone who has it can access your account. Never commit or share it. Every download result is tagged `used_cookies` / `cookie_fallback` in the JSON log so you can see exactly when they were used.

---

## Usage

### Dashboard

```
python ig_download.py --dashboard
```

This opens the one app. The **Download** tab takes the URL list, collection name, output folder, cookies file, cookies mode, and filename template. Those paths are remembered in `ig_dashboard_settings.json` (gitignored) and filled in next time. **Instagram** shows the saved posts. **Log** is the filterable table. Start and Stop run on the Python process that is already serving the page.

### CLI

```
python ig_download.py <url-file> <collection-name> [options]
```

```
positional arguments:
  url_file              Path to a .txt file with one Instagram URL per line
  collection            Name for this collection (used as the log file name)

options:
  --cookies FILE        Path to a cookies.txt file
  --cookies-mode MODE   fallback (default) | always | never
  --output-dir DIR      Base folder for downloads (default: downloads)
  --log FILE            Override the log file path (default: <collection>.json)
  --filename-template T Filename template for saved files (default: {shortcode})
  --retry-failed        Re-attempt URLs previously logged as failed
  --dry-run             Extract metadata only, do not download
```

**Example:**

```
python ig_download.py my-saves.txt design --cookies cookies.txt
```

---

## Features

- **Videos and reels** - downloaded via yt-dlp (best quality, merged to mp4)
- **Image posts** - downloaded via instaloader
- **Carousels** - yt-dlp handles mixed video/image carousels; falls back to instaloader for image-only carousels
- **Skip duplicates** - already-downloaded posts are skipped automatically (by log and by disk check)
- **Throttle protection** - explicit rate limits *and* the "Failed to parse JSON" response (Instagram serving HTML where JSON is expected) are backed off exponentially and retried; the run only aborts after several consecutive failures, saving progress first
- **Retry failed** - re-run with `--retry-failed` to retry anything that previously failed
- **Custom filename templates** - control how files are named using variables like `{shortcode}`, `{author}`, `{date}`, `{upload_date}`, `{index}`
- **Custom output folder** - download into any base folder with `--output-dir` or the Download tab; each collection is created as a subfolder inside it
- **Cookie fallback** - cookies are kept out of the way until a post is age-restricted or private, then used automatically on retry
- **Crash-safe logs** - writes are atomic (temp file + rename), and a corrupt log is moved aside to `*.corrupt-<timestamp>` instead of being overwritten
- **Input validation** - collection names and filename templates are checked so they can never write outside the output folder
- **Live progress** - real-time output in the terminal and on the Download tab; 🔑 marks downloads that used cookies
- **JSON log** - every download is logged with status, author, media type, file path, error detail, and whether cookies were used

---

## Filename templates

The `--filename-template` option (or the template field on the Download tab) controls the output filename. Available variables:

| Variable | Description |
|---|---|
| `{shortcode}` | Instagram post shortcode (default) |
| `{author}` | Uploader username |
| `{title}` | Post title as reported by yt-dlp |
| `{upload_date}` | Original upload date (`YYYYMMDD`) |
| `{date}` | Today's date (`YYYY-MM-DD`) |
| `{index}` | Sequential position in the current run |
| `{index:04d}` | Zero-padded index (width 4) |

Examples:

```
{shortcode}                          →  DYFgyOEuIRN.mp4
{author}_{shortcode}                 →  natgeo_DYFgyOEuIRN.mp4
{date}_{index:04d}_{shortcode}       →  2026-05-18_0001_DYFgyOEuIRN.mp4
```

---

## Tests

The core logic has a small pytest suite (no network required):

```
python -m pytest tests -q
```

It also runs with plain Python:

```
python tests/test_ig_download.py
```

Covered: error classification, the cookie-fallback policy, Instagram URL/shortcode parsing, path-traversal validation, and atomic/corruption-safe log handling.

---

## Library

`python ig_download.py --dashboard` serves the page on `127.0.0.1` and loads every collection log it can find. Each log is `<output>/<collection>/<collection>.json`, written beside the downloaded files. The Instagram grid and the Log tab both read those files. **Load JSON** can still merge an extra log by hand.

---

## Extensions

### Saved collections

On the Download tab, **Refresh** opens your saved page with the cookies file and lists the collections shown there. **Download** on a row reads every post and reel link from that collection's page, writes `Collections/<name>.txt`, and starts the download into the saved output folder. Cookies are sent with every post in that run.

The first time you use it, install the page reader:

```
pip install -r requirements.txt
python -m playwright install chromium
```

### IG Link Collector (Tampermonkey userscript)

The userscript is still there for Explore or someone else's profile. Saved collections no longer need it.

**Install:** [IG Link Collector](https://github.com/doncezart/IGbulkCollector)

Requires [Tampermonkey](https://www.tampermonkey.net/). Once installed, open a feed on Instagram. A small control panel appears in the corner. Hit **Start** and let it scroll; when done, click **Export** to download the URL list.

---

## Notes

- Downloads go to a `downloads/` folder next to the script by default; use `--output-dir` or the Download tab to change the base folder. The dashboard remembers that folder.
- Instagram restricts access for non-authenticated requests. Cookies are only needed for age-restricted or private posts; for very large batches you can opt into `--cookies-mode always`.
- This tool is for personal archival use. Respect Instagram's terms of service and the rights of content creators.

---

## Troubleshooting

### "Failed to parse JSON (caused by JSONDecodeError(...))"

**Root cause.** Instagram sometimes answers one of its JSON endpoints with an HTML login / consent / challenge page while still returning HTTP 200. yt-dlp's `InfoExtractor._parse_json()` (in `yt_dlp/extractor/common.py`) then runs `json.loads()` over that HTML and raises:

```
Failed to parse JSON (caused by JSONDecodeError("Expecting value in '': line 1 column 1 (char 0)"))
```

It is normally a temporary throttle after a large batch, **not** a broken post.

**What this tool does.** It classifies that message as a transient throttle (`parse_error`) and reuses the same exponential backoff as an explicit rate limit (30s → 60s → 120s → 300s), retrying the URL. After several consecutive failures it pauses, saves progress and aborts, so you can resume later with `--retry-failed`.

**What you should do.**

1. **Update yt-dlp** — Instagram changes its private API constantly and the extractor is fixed often: `pip install -U yt-dlp`.
2. Wait a while, then re-run. If the content is genuinely login-gated, retry with `--cookies-mode always`.

> Do **not** blindly copy the "fork fix" from issue #2. It does not fix the JSON parse — it simply relabels the error and re-downloads through instaloader. That fork also committed the reporter's `cookies.txt`, saved-post list and `__pycache__`. This repo now ships a `.gitignore` for exactly that reason.

---

## Forks and AI-assisted development

**Read this before you (or an AI assistant) commit anything.**

- `.gitignore` is not optional. It keeps `cookies.txt`, `*.session`, `Saved Posts/`, `downloads/`, generated `*.json` logs and `__pycache__/` out of git.
- A `cookies.txt` is equivalent to your Instagram password. Never commit it, paste it into a chat, attach it to an issue, or share it. If you ever do, revoke the session inside Instagram immediately and rotate the cookie.
- If you use an AI coding assistant on a fork, tell it explicitly: **do not stage, commit, print or upload any file matching the `.gitignore` patterns, and never echo the contents of `cookies.txt`.**
- Before opening a pull request, run `git status` and `git diff --staged` and confirm no cookies, URL lists or logs are included.

---

## License

[MIT](LICENSE) © 2026 Don Cezar.
