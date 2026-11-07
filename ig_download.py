#!/usr/bin/env python3
"""
ig_download.py — Download Instagram posts from a URL list using yt-dlp + instaloader.

Usage:
    python ig_download.py urls.txt art
    python ig_download.py urls.txt art --cookies cookies.txt
    python ig_download.py urls.txt art --dry-run
    python ig_download.py urls.txt art --retry-failed

Rate-limit protection: consecutive rate-limit responses trigger exponential backoff
(30s -> 60s -> 120s -> 300s) and the same URL is retried automatically.

Log deduplication: each URL has at most one entry — retries overwrite the previous
result rather than creating a second entry.

Caption: actual post caption is now stored alongside the synthetic title.

Cookie policy: by default cookies are NOT used for normal downloads. They are
sent only as a fallback when a post fails because it is age-restricted or
private. Override with --cookies-mode always|never.
"""

import argparse
import html
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed.\nRun: pip install yt-dlp")

try:
    import instaloader
    _INSTALOADER_AVAILABLE = True
except ImportError:
    _INSTALOADER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RATE_LIMIT_DELAYS = [30, 60, 120, 300]
_MAX_CONSECUTIVE_RATE_LIMITS = len(_RATE_LIMIT_DELAYS) + 2

# Instagram sometimes answers a JSON endpoint with an HTML login/consent/challenge
# page (HTTP 200) when it is throttling. yt-dlp's _parse_json() then raises
# "Failed to parse JSON (caused by JSONDecodeError(...))". That is a transient
# throttle/login-wall rather than a broken post, so it is backed off and retried
# exactly like an explicit rate limit instead of being logged as a hard failure.
_TRANSIENT_ERROR_CATEGORIES = frozenset({"rate_limited", "parse_error"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHABCDFJsu]')

def strip_ansi(s):
    return _ANSI_RE.sub('', s or '').strip()


def load_urls(path):
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    seen = set()
    unique = []
    for url in lines:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


class LogCorruptedError(Exception):
    """Raised when an existing log file cannot be parsed.

    We never silently treat a corrupt log as empty, because the next save would
    overwrite it and destroy the download history.
    """

    def __init__(self, path, reason):
        super().__init__(f"Log file is not valid JSON: {path} ({reason})")
        self.path = path
        self.reason = reason


def load_log(path):
    """Load and dedupe the log (latest entry wins per URL).

    Raises LogCorruptedError if the file exists but is not a valid log - the
    caller must decide what to do rather than losing the data.
    """
    if not os.path.exists(path):
        return {"items": [], "summary": {}}
    if os.path.getsize(path) == 0:
        return {"items": [], "summary": {}}
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LogCorruptedError(path, e) from e

    if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
        raise LogCorruptedError(path, "unexpected structure (missing 'items' list)")

    seen = {}
    for item in data["items"]:
        key = item.get("url") or item.get("shortcode") or f"__{len(seen)}"
        seen[key] = item
    data["items"] = list(seen.values())
    return data


def load_log_or_backup(path):
    """Load the log, but if it is corrupt move it aside to <path>.corrupt-<ts>
    and start a fresh one instead of overwriting history."""
    try:
        return load_log(path)
    except LogCorruptedError as e:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.corrupt-{stamp}"
        try:
            os.replace(path, backup)
            print(f"WARNING: {path} is corrupt ({e}); moved it to {backup} "
                  f"and starting a new log.", file=sys.stderr)
        except OSError:
            print(f"WARNING: {path} is corrupt and could not be backed up; "
                  f"starting a new log (the old file was left untouched).", file=sys.stderr)
        return {"items": [], "summary": {}}


def save_log(log, path):
    """Write the log atomically: a crash can never leave a half-written file.

    The JSON is written to a temp file in the same directory, flushed to disk,
    then os.replace()d over the target. os.replace is atomic on one filesystem,
    so readers always see either the old file or the complete new one.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".iglog-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert_log_entry(log, result):
    """Insert or update a log entry keyed by URL. Prevents duplicates on retry runs."""
    url = result.get("url")
    for i, item in enumerate(log["items"]):
        if item.get("url") == url:
            log["items"][i] = result
            return
    log["items"].append(result)


_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv|share)/"
    r"(?P<code>[A-Za-z0-9_-]+)(?=[/?#]|$)",
    re.IGNORECASE,
)


def shortcode_from_url(url):
    """Extract the post shortcode from common Instagram URL shapes.

    Handles /p/, /reel/, /reels/, /tv/, /share/ and the /<user>/p/... form,
    and ignores query strings and fragments. Returns "unknown" if nothing
    matches.
    """
    m = _SHORTCODE_RE.search(url or "")
    return m.group("code") if m else "unknown"


def already_done(log, url):
    return any(item["url"] == url and item.get("status") == "ok" for item in log["items"])


def already_attempted(log, url):
    return any(item["url"] == url for item in log["items"])


# ---------------------------------------------------------------------------
# Input validation (paths)
# ---------------------------------------------------------------------------

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_collection(name):
    """A collection becomes a single directory name, so reject anything that
    could escape the output folder or is invalid as a folder name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Collection name must not be empty.")
    if name in (".", "..") or os.path.isabs(name) or os.path.basename(name) != name:
        raise ValueError(f"Collection name must not contain path separators or be absolute: {name!r}")
    if _INVALID_NAME_CHARS.search(name):
        raise ValueError(f"Collection name contains characters invalid in a folder name: {name!r}")
    if name.upper() in _WINDOWS_RESERVED:
        raise ValueError(f"Collection name is a reserved system name: {name!r}")
    return name


def validate_filename_template(template):
    """The template is turned into a file path, so it must not escape the
    output folder."""
    template = (template or "").strip() or "{shortcode}"
    if os.path.isabs(template) or "/" in template or "\\" in template:
        raise ValueError(f"Filename template must not contain path separators: {template!r}")
    if ".." in template:
        raise ValueError(f"Filename template must not contain '..': {template!r}")
    if any(ord(c) < 32 for c in template):
        raise ValueError("Filename template must not contain control characters.")
    return template


def resolve_output_dir(output_base, collection):
    """Join base + collection and refuse to leave the base folder."""
    output_base = (output_base or "downloads").strip() or "downloads"
    base_abs = os.path.abspath(output_base)
    output_dir = os.path.join(base_abs, collection)
    if os.path.commonpath([base_abs, os.path.abspath(output_dir)]) != base_abs:
        raise ValueError(f"Resolved output folder escapes the base folder: {output_dir!r}")
    return output_dir


def ffmpeg_available():
    """yt-dlp needs ffmpeg on PATH to merge best video + audio into one mp4."""
    return shutil.which("ffmpeg") is not None


_RE_RATE = re.compile(r"rate[- ]limit reached|too many requests|\b429\b")
_RE_RESTRICTED = re.compile(
    r"private|login|log in|not available|isn't available|\bage\b|"
    r"certain audiences|available to everyone|restricted|sign in|authentication|"
    r"registered users|follow this account")
_RE_DELETED = re.compile(r"does not exist|sorry|removed|deleted")
_RE_UNSUPPORTED = re.compile(r"unsupported url")
_RE_IMAGE = re.compile(r"no video|\bimage\b|\bphoto\b|no formats")
_RE_NETWORK = re.compile(r"network|connect|timed out|http error")
_RE_PARSE = re.compile(r"failed to parse json|jsondecodeerror|unable to extract.*json")


def classify_error(msg):
    m = (msg or "").lower()
    if _RE_RATE.search(m):
        return "rate_limited"
    if _RE_RESTRICTED.search(m):
        return "private_or_restricted"
    if _RE_DELETED.search(m):
        return "deleted_or_not_found"
    if _RE_UNSUPPORTED.search(m):
        return "unsupported_url"
    if _RE_IMAGE.search(m):
        return "image_only"
    if _RE_NETWORK.search(m):
        return "network_error"
    if _RE_PARSE.search(m):
        # Instagram served HTML where JSON was expected (throttle / consent /
        # login wall). Treated as transient by _TRANSIENT_ERROR_CATEGORIES.
        return "parse_error"
    return "other_error"


# ---------------------------------------------------------------------------
# Cookie policy
# ---------------------------------------------------------------------------

# How the cookies file is used:
#   "fallback" (default) - never sent on the first attempt; retried with cookies
#                          ONLY when the post is age-restricted or private.
#   "always"             - sent on the first attempt.
#   "never"              - the cookies file is ignored entirely.
COOKIES_MODE_FALLBACK = "fallback"
COOKIES_MODE_ALWAYS = "always"
COOKIES_MODE_NEVER = "never"
COOKIES_MODES = (COOKIES_MODE_FALLBACK, COOKIES_MODE_ALWAYS, COOKIES_MODE_NEVER)

# Categories/details that mean "this failed because of an age gate or a
# private/login requirement" - the only time cookies are allowed to be used.
_RESTRICTED_ERROR_CATEGORIES = {"private_or_restricted"}
_RESTRICTED_ERROR_MARKERS = (
    "private",
    "login",
    "log in",
    "require_login",
    "registered users",
    "follow this account",
    "authentication",
    "sign in",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "isn't available to everyone",
    "not available to everyone",
    "available to everyone",
    "certain audiences",
    "only available to logged",
    "logged-in users",
    "18+",
    "nsfw",
    "restricted",
)


def is_restricted_error(result):
    """True when a failed download was blocked by an age gate or a
    private/login requirement.

    Network, rate-limit, deleted-post and JSON/parse errors deliberately do NOT
    qualify - cookies are not sent for those.
    """
    if not result or result.get("status") != "failed":
        return False
    if result.get("error_category") in _RESTRICTED_ERROR_CATEGORIES:
        return True
    detail = (result.get("error_detail") or "").lower()
    return any(marker in detail for marker in _RESTRICTED_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Filename template system
# ---------------------------------------------------------------------------

_TEMPLATE_TO_YTDLP = {
    "{shortcode}":   "%(id)s",
    "{author}":      "%(uploader_id)s",
    "{title}":       "%(title)s",
    "{upload_date}": "%(upload_date)s",
}

FILENAME_PRESETS = {
    "{shortcode}":                              "Shortcode only (default)",
    "{author}_{shortcode}":                     "Author + shortcode",
    "{date}_{shortcode}":                       "Download date + shortcode",
    "{index:04d}_{shortcode}":                  "4-digit index + shortcode",
    "{upload_date}_{shortcode}":                "Upload date + shortcode",
    "{author}_{upload_date}_{shortcode}":       "Author + upload date + shortcode",
    "{date}_{index:04d}_{author}_{shortcode}": "Date + index + author + shortcode",
}

FILENAME_VARIABLE_DOCS = """
Available template variables
────────────────────────────
{shortcode}      Instagram post shortcode, e.g. DYFgyOEuIRN
{author}         Uploader username (e.g. natgeo)
{title}          Post title as reported by yt-dlp
{upload_date}    Original upload date in YYYYMMDD format (e.g. 20240318)
{date}           Today's date in YYYY-MM-DD format (e.g. 2026-05-18)
{index}          Sequential number within the current run (0-based)
{index:04d}      Zero-padded index, width 4  →  0001, 0002, …
{index:03d}      Zero-padded index, width 3  →  001, 002, …

The file extension (.mp4, .jpg, …) is always appended automatically.
Carousel slides are saved together in a folder named with the post shortcode.
"""


def build_outtmpl(template: str, output_dir: str, file_index: int = 0) -> str:
    """Convert our {var} template to a yt-dlp output path string."""
    today = datetime.now().strftime("%Y-%m-%d")
    t = template
    # Pre-substitute non-yt-dlp variables
    t = re.sub(r"\{index:(\d+)d\}", lambda m: f"{file_index:0{int(m.group(1))}d}", t)
    t = t.replace("{index}", str(file_index))
    t = t.replace("{date}", today)
    # Map our var names to yt-dlp %(...)s placeholders
    for our_var, ytdlp_var in _TEMPLATE_TO_YTDLP.items():
        t = t.replace(our_var, ytdlp_var)
    return os.path.join(output_dir, t + ".%(ext)s")


def extract_author_from_title(title, fallback):
    if title and " by " in title:
        return title.split(" by ")[-1].strip()
    return fallback or "unknown"


def _wait_rate_limit(consecutive, stop_event=None, progress_cb=None):
    """Sleep with countdown. Returns True if completed, False if stop_event fired."""
    delay = _RATE_LIMIT_DELAYS[min(consecutive - 1, len(_RATE_LIMIT_DELAYS) - 1)]
    print(f"\n  WARNING Throttled ({consecutive}x in a row) - waiting {delay}s before retrying...", flush=True)
    if progress_cb:
        progress_cb({"type": "rate_limited", "wait_seconds": delay, "consecutive": consecutive})
    deadline = time.monotonic() + delay
    while True:
        if stop_event and stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        print(f"\r  Waiting: {int(remaining):3d}s remaining...  ", end="", flush=True)
        if progress_cb:
            progress_cb({"type": "rate_limit_tick", "remaining": int(remaining)})
        time.sleep(min(1.0, remaining))
    print(f"\r  Resuming...                   ", flush=True)
    return True


def collection_log_path(output_base, collection):
    """JSON log lives inside the collection folder, named after the collection."""
    return os.path.join(resolve_output_dir(output_base, collection), f"{collection}.json")


_SKIP_SCAN_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".vscode", ".idea",
}
_SKIP_LOG_NAMES = {"ig_logs_manifest.json", "ig_library.json", "ig_dashboard_roots.json"}
_MAX_COLLECTION_DEPTH = 3

_dashboard_server = None
_dashboard_lock = threading.Lock()


def _is_collection_log_name(name):
    if not name.endswith(".json"):
        return False
    if name in _SKIP_LOG_NAMES or name.endswith(".tmp"):
        return False
    if ".corrupt-" in name:
        return False
    return True


def _looks_like_download_log(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            head = fh.read(800)
    except OSError:
        return False
    return '"items"' in head


def path_is_under(path, roots):
    """True when path resolves inside one of roots. Rejects path traversal."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        if not root:
            continue
        try:
            base = os.path.realpath(root)
        except OSError:
            continue
        try:
            if os.path.commonpath([base, real]) == base:
                return True
        except ValueError:
            continue
    return False


def discover_collections(roots):
    """Find collection folders: a directory that holds a download-log JSON.

    Scans each root a few levels down. The collection name is the folder name.
    The same folder is returned once even if two roots both contain it.
    """
    found = {}
    web_root = os.path.dirname(os.path.abspath(__file__))
    for root in roots:
        root = os.path.abspath(root) if root else ""
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _SKIP_SCAN_DIRS and not d.startswith(".")
            )
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > _MAX_COLLECTION_DEPTH:
                dirnames.clear()
                continue
            if os.path.abspath(dirpath) == root:
                continue
            logs = []
            for name in sorted(filenames):
                if not _is_collection_log_name(name):
                    continue
                full = os.path.join(dirpath, name)
                if _looks_like_download_log(full):
                    logs.append(full)
            if not logs:
                continue
            key = os.path.abspath(dirpath)
            hrefs = [_log_href(path, web_root) for path in logs]
            found[key] = {
                "name": os.path.basename(dirpath),
                "account": _library_account(dirpath, roots),
                "dir": os.path.relpath(dirpath, web_root).replace("\\", "/")
                if path_is_under(dirpath, [web_root])
                else os.path.abspath(dirpath),
                "logs": hrefs,
                "abs_logs": logs,
            }
            dirnames.clear()
    return sorted(found.values(), key=lambda c: c["name"].lower())


def _library_account(dirpath, roots):
    """The account folder is the parent of a collection, one level under a library root."""
    parent = os.path.dirname(os.path.abspath(dirpath))
    known = {os.path.realpath(root) for root in roots if root}
    if os.path.realpath(parent) in known:
        return ""
    if os.path.realpath(os.path.dirname(parent)) in known:
        return os.path.basename(parent)
    return ""


def _log_href(path, web_root):
    if path_is_under(path, [web_root]):
        return os.path.relpath(path, web_root).replace("\\", "/")
    return "/api/log?path=" + urllib.parse.quote(os.path.abspath(path))


def _dashboard_roots_file():
    return os.path.join(_state_dir(), "ig_dashboard_roots.json")


def remember_library_roots(extra_roots):
    """Remember output folders so the dashboard can see collections outside the project."""
    path = _dashboard_roots_file()
    current = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            current = [os.path.abspath(p) for p in data if isinstance(p, str)]
    except (OSError, json.JSONDecodeError):
        current = []
    changed = False
    for root in extra_roots or []:
        if not root:
            continue
        ab = os.path.abspath(root)
        if ab not in current:
            current.append(ab)
            changed = True
    if changed or not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2)
        except OSError:
            pass
    return current


def library_roots(extra_roots=None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    roots = [script_dir]
    data = configured_data_dir()
    if data and data not in roots:
        roots.append(data)
    roots.extend(remember_library_roots(extra_roots))
    for root in extra_roots or []:
        if root:
            ab = os.path.abspath(root)
            if ab not in roots:
                roots.append(ab)
    return roots


_POSTER_LOCK = threading.Lock()
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}


def video_poster_path(video_path):
    """Cached still frame, kept out of the media folder so it is not a slide."""
    folder = os.path.join(os.path.dirname(os.path.abspath(video_path)), ".posters")
    return os.path.join(folder, os.path.basename(video_path) + ".jpg")


def ensure_video_poster(video_path):
    """Extract one JPEG frame with ffmpeg. Returns the jpeg path, or None."""
    video_path = os.path.abspath(video_path)
    if os.path.splitext(video_path)[1].lower() not in _VIDEO_EXTS or not os.path.isfile(video_path):
        return None
    dest = video_poster_path(video_path)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0 and os.path.getmtime(dest) >= os.path.getmtime(video_path):
        return dest
    if shutil.which("ffmpeg") is None:
        return None
    with _POSTER_LOCK:
        if os.path.isfile(dest) and os.path.getsize(dest) > 0 and os.path.getmtime(dest) >= os.path.getmtime(video_path):
            return dest
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for seek in ("0.5", "0"):
            cmd = [
                "ffmpeg", "-y", "-ss", seek, "-i", video_path,
                "-frames:v", "1", "-vf", "scale=480:-1", "-q:v", "5", dest,
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30, creationflags=flags)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                return dest
    return None


def dashboard_ui_root(script_dir=None):
    """Built React app when it exists, otherwise the original HTML file."""
    script_dir = script_dir or os.path.dirname(os.path.abspath(__file__))
    dist = os.path.join(script_dir, "dashboard", "dist")
    if os.path.isfile(os.path.join(dist, "index.html")):
        return dist
    return script_dir


_DATA_DIR = None


def set_data_dir(path):
    """Remember a dataset folder for this process. None clears the override."""
    global _DATA_DIR
    if not path:
        _DATA_DIR = None
        return
    _DATA_DIR = os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def configured_data_dir():
    """Dataset folder from --data-dir or IGRIP_DATA. Empty when unset."""
    if _DATA_DIR:
        return _DATA_DIR
    raw = os.environ.get("IGRIP_DATA", "").strip()
    if not raw:
        return ""
    return os.path.realpath(os.path.abspath(os.path.expanduser(raw)))


def _state_dir():
    """Where settings live. The dataset survives an image update; the script dir does not."""
    data = configured_data_dir()
    if data:
        os.makedirs(data, exist_ok=True)
        return data
    return os.path.dirname(os.path.abspath(__file__))


def dashboard_settings_path():
    return os.path.join(_state_dir(), "ig_dashboard_settings.json")


def default_dashboard_settings():
    data = configured_data_dir()
    return {
        "output_dir": data or "downloads",
        "cookies": "",
        "cookies_mode": COOKIES_MODE_FALLBACK,
        "filename_template": "{shortcode}",
        "url_file": "",
    }


def sanitize_dashboard_settings(data):
    """Keep only the fields the dashboard is allowed to remember."""
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for key in ("output_dir", "cookies", "url_file"):
        value = data.get(key)
        if isinstance(value, str):
            cleaned[key] = _resolve_user_path(value) if value.strip() else ""
    template = data.get("filename_template")
    if isinstance(template, str):
        cleaned["filename_template"] = template.strip()
    mode = data.get("cookies_mode")
    if mode in COOKIES_MODES:
        cleaned["cookies_mode"] = mode
    if "filename_template" in cleaned:
        try:
            cleaned["filename_template"] = validate_filename_template(cleaned["filename_template"])
        except ValueError:
            cleaned.pop("filename_template", None)
    return cleaned


def load_dashboard_settings():
    settings = default_dashboard_settings()
    try:
        with open(dashboard_settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return settings
    settings.update(sanitize_dashboard_settings(data))
    return settings


def save_dashboard_settings(data):
    """Remember download preferences. The output folder stays visible to the library."""
    settings = load_dashboard_settings()
    settings.update(sanitize_dashboard_settings(data))
    path = dashboard_settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, path)
    if settings.get("output_dir"):
        remember_library_roots([settings["output_dir"]])
    return settings


def _resolve_user_path(path):
    """Relative preferences sit in the data folder when one is set, otherwise beside this script."""
    path = os.path.expanduser((path or "").strip())
    if not path:
        return ""
    if not os.path.isabs(path):
        base = configured_data_dir() or os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)
    return os.path.abspath(path)


def browse_home():
    """The furthest up the dashboard file picker is allowed to go."""
    return os.path.realpath(os.path.expanduser("~"))


def storage_roots():
    """Folders the app may read and write: the user home, plus the NAS dataset."""
    roots = [browse_home()]
    data = configured_data_dir()
    if data and data not in roots:
        roots.append(data)
    return roots


def _require_storage_dir(path, label):
    """Allow a folder under the user home or the data folder, even if it is not created yet."""
    if os.path.isdir(path):
        allowed = path_is_under(path, storage_roots())
    else:
        parent = os.path.dirname(path) or path
        allowed = path_is_under(parent, storage_roots())
    if not allowed:
        raise ValueError(f"{label} must stay inside your user folder or the data folder.")
    return path


def browse_directory(path=None):
    """List folders and .txt files inside the user home or the data folder.

    The browser cannot see the disk. The Python process does, and this is the
    only listing the Download tab is given. Cookie files are named, never read.
    """
    roots = storage_roots()
    start = path or configured_data_dir() or os.path.dirname(os.path.abspath(__file__))
    full = os.path.realpath(start)
    if os.path.isfile(full):
        full = os.path.dirname(full)
    if not path_is_under(full, roots):
        raise ValueError("That folder is outside your user folder or the data folder.")
    if not os.path.isdir(full):
        raise ValueError("That path is not a folder.")
    parent = os.path.dirname(full)
    parent_ok = parent != full and path_is_under(parent, roots)
    skip = {".git", "__pycache__", "node_modules", ".vscode", "venv", ".posters"}
    dirs, files = [], []
    try:
        entries = list(os.scandir(full))
    except OSError as exc:
        raise ValueError("That folder could not be read.") from exc
    for entry in entries:
        if entry.name.startswith(".") or entry.name in skip:
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            dirs.append({"name": entry.name, "path": entry.path})
        elif is_file and entry.name.lower().endswith(".txt"):
            files.append({"name": entry.name, "path": entry.path})
    dirs.sort(key=lambda item: item["name"].lower())
    files.sort(key=lambda item: item["name"].lower())
    return {
        "path": full,
        "parent": parent if parent_ok else None,
        "dirs": dirs,
        "files": files,
    }


_RESERVED_PROFILES = {
    "explore", "reels", "direct", "accounts", "stories", "p", "reel", "saved",
    "legal", "about", "privacy", "terms", "emails", "directory", "developer",
    "challenge", "login", "tv", "ar", "nametag",
}
_POST_URL_RE = re.compile(
    r"(?:https://www\.instagram\.com)?/(?:[^/\"'\\s]+/)?(p|reel)/([A-Za-z0-9_-]{5,})/?"
)
_BROWSER_LOCK = threading.Lock()
_LOGIN_AGAIN = "Instagram asked you to log in. Export cookies.txt again."


class _AnchorParser(HTMLParser):
    """Collect anchor hrefs and the text shown inside them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._href = None
        self._label = ""
        self._buf = []

    def handle_starttag(self, tag, attrs):
        ad = {key.lower(): value or "" for key, value in attrs}
        if tag == "img" and self._href is not None and ad.get("alt"):
            self._buf.append(ad["alt"])
        if tag != "a":
            return
        href = html.unescape(ad.get("href") or "")
        if not href:
            return
        self._href = href
        self._label = ad.get("aria-label") or ""
        self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._buf).split())
            self.anchors.append((self._href, text, self._label))
            self._href = None


def _anchors(page_html):
    parser = _AnchorParser()
    try:
        parser.feed(page_html or "")
        parser.close()
    except Exception:
        return []
    return parser.anchors


def _path_parts(href):
    href = html.unescape(href or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urllib.parse.urlparse(href)
        host = (parsed.netloc or "").lower()
        if host and host not in ("www.instagram.com", "instagram.com"):
            return []
        path = parsed.path
    else:
        path = href
    path = path.split("?", 1)[0].split("#", 1)[0]
    return [part for part in path.split("/") if part]


def parse_profile_username(page_html):
    """The logged-in home page links to /{username}/. Prefer the Profile control."""
    labeled = []
    counts = {}
    for href, text, label in _anchors(page_html):
        parts = _path_parts(href)
        if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9._]+", parts[0]):
            continue
        if parts[0].lower() in _RESERVED_PROFILES:
            continue
        blob = f"{label} {text}".lower()
        if "profile" in blob:
            labeled.append(parts[0])
        counts[parts[0]] = counts.get(parts[0], 0) + 1
    if labeled:
        return labeled[0]
    if not counts:
        return ""
    return max(counts, key=counts.get)


def _collection_label(text, slug):
    text = " ".join((text or "").split())
    match = re.match(r"^(.*?\D)\s+(\d+)\s*(?:posts?)?$", text, re.I)
    if match and match.group(1).strip():
        return match.group(1).strip(), int(match.group(2))
    if text and not text.isdigit():
        return text, None
    pretty = (slug or "").replace("-", " ").strip()
    return pretty or slug, None


def parse_saved_collections(page_html, username):
    """Collection tiles on /{username}/saved/: name, link, and count when shown."""
    found = []
    seen = set()
    owner = (username or "").lower()
    for href, text, _label in _anchors(page_html):
        parts = _path_parts(href)
        if len(parts) < 3 or parts[0].lower() != owner or parts[1].lower() != "saved":
            continue
        slug = parts[2]
        if slug.lower() in ("", "saved"):
            continue
        url = "https://www.instagram.com/" + "/".join(parts) + "/"
        if url in seen:
            continue
        seen.add(url)
        name, count = _collection_label(text, slug)
        item = {"name": name, "url": url}
        if count is not None:
            item["count"] = count
        found.append(item)
    return found


def parse_post_urls(page_html):
    """Post and reel links, in the order the page first mentions each code."""
    found = []
    seen = set()
    for kind, code in _POST_URL_RE.findall(page_html or ""):
        kind = kind.lower()
        if code in seen:
            continue
        seen.add(code)
        found.append(f"https://www.instagram.com/{kind}/{code}/")
    return found


def looks_like_login(url, page_html):
    path = urllib.parse.urlparse(url or "").path.lower()
    if path.startswith("/accounts/login") or "/accounts/login" in path:
        return True
    body = page_html or ""
    return "accounts/login" in body and 'name="username"' in body


def collection_folder_name(name):
    """Turn a collection title into one folder name."""
    cleaned = _INVALID_NAME_CHARS.sub(" ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return validate_collection(cleaned)


def _downloaded_counts(roots, account=None):
    """How many posts each collection folder already has, by folder name."""
    counts = {}
    wanted = None if account is None else (account or "").casefold()
    for collection in discover_collections(roots):
        found_account = (collection.get("account") or "").casefold()
        if wanted is not None and found_account != wanted:
            continue
        seen = set()
        saved = failed = 0
        for path in collection.get("abs_logs") or []:
            log = load_log(path)
            for item in log.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = item.get("url") or item.get("shortcode")
                if not key or key in seen:
                    continue
                seen.add(key)
                status = item.get("status")
                if status == "ok":
                    saved += 1
                elif status == "failed":
                    failed += 1
        name = (collection.get("name") or "").casefold()
        if not name:
            continue
        bucket = counts.setdefault(name, {"saved": 0, "failed": 0})
        bucket["saved"] += saved
        bucket["failed"] += failed
    return counts


def annotate_collection_counts(collections, roots, account=None):
    """Add how many posts we already have next to Instagram's count."""
    counts = _downloaded_counts(roots, account=account)
    for item in collections or []:
        try:
            folder = collection_folder_name(item.get("name") or "")
        except ValueError:
            folder = ""
        found = counts.get(folder.casefold(), {})
        item["saved"] = found.get("saved", 0)
        item["failed"] = found.get("failed", 0)
    return collections


def is_saved_collection_url(url):
    return bool(_saved_url_parts(url))


def _saved_url_parts(url):
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("www.instagram.com", "instagram.com"):
        return []
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[1].lower() != "saved":
        return []
    return parts


def account_from_saved_url(url):
    """The username is the first part of /{username}/saved/{collection}/."""
    parts = _saved_url_parts(url)
    return parts[0] if parts else ""


def account_output_dir(output_base, username):
    """output/<username>, still inside the chosen output folder."""
    return resolve_output_dir(output_base, collection_folder_name(username))


def cookie_user_id(cookies_file):
    """Instagram's numeric account id from a Netscape cookies file."""
    if not cookies_file or not os.path.isfile(cookies_file):
        return ""
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        return ""
    for cookie in jar:
        if cookie.name == "ds_user_id" and cookie.value:
            return str(cookie.value)
    return ""


def _saved_cache_path():
    return os.path.join(_state_dir(), "saved_collections.json")


def load_saved_cache():
    try:
        with open(_saved_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"accounts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        return {"accounts": {}}
    return data


def save_saved_cache(data):
    path = _saved_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def cached_account_username(user_id):
    if not user_id:
        return ""
    entry = load_saved_cache()["accounts"].get(str(user_id)) or {}
    name = entry.get("username") or ""
    return name if isinstance(name, str) else ""


TRACK_INTERVAL = timedelta(days=5)
TRACK_POLL_SECONDS = 60 * 60
TRACK_FIRST_WAIT_SECONDS = 30


def store_saved_collections(user_id, username, collections):
    cache = load_saved_cache()
    previous = {}
    old_entry = cache["accounts"].get(str(user_id)) or {}
    for item in old_entry.get("collections") or []:
        if isinstance(item, dict) and item.get("url"):
            previous[item["url"]] = item
    stored = []
    for item in collections or []:
        if not isinstance(item, dict):
            continue
        row = {"name": item.get("name") or "", "url": item.get("url") or ""}
        if item.get("count") is not None:
            row["count"] = item.get("count")
        old = previous.get(row["url"]) or {}
        row["tracked"] = bool(old.get("tracked"))
        if old.get("checked_at"):
            row["checked_at"] = old["checked_at"]
        stored.append(row)
    cache["accounts"][str(user_id)] = {
        "username": username,
        "updated": datetime.now(timezone.utc).isoformat(),
        "collections": stored,
    }
    save_saved_cache(cache)
    return cache["accounts"][str(user_id)]


def collection_is_due(item, now=None):
    """A tracked collection is due when it has never been checked, or the last check was 5 days ago."""
    if not isinstance(item, dict) or not item.get("tracked"):
        return False
    now = now or datetime.now(timezone.utc)
    raw = item.get("checked_at") or ""
    if not raw:
        return True
    try:
        then = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return now - then >= TRACK_INTERVAL


def update_saved_tracking(params):
    """Turn tracking on or off for one saved collection, or for every collection."""
    if not isinstance(params, dict):
        raise ValueError("Expected a JSON object.")
    cookies, _settings = require_cookies_file()
    user_id = cookie_user_id(cookies)
    if not user_id:
        raise ValueError("The cookies file has no account id. Export cookies.txt again.")
    cache = load_saved_cache()
    entry = cache["accounts"].get(user_id)
    if not entry or not entry.get("collections"):
        raise ValueError("Refresh saved collections before tracking.")
    if params.get("all"):
        for item in entry["collections"]:
            if isinstance(item, dict):
                item["tracked"] = True
    else:
        url = (params.get("url") or "").strip()
        found = False
        for item in entry["collections"]:
            if isinstance(item, dict) and item.get("url") == url:
                item["tracked"] = bool(params.get("tracked"))
                found = True
                break
        if not found:
            raise ValueError("That collection is not in the saved list.")
    save_saved_cache(cache)
    return saved_collections_view()


def mark_collection_checked(user_id, url, when=None):
    cache = load_saved_cache()
    entry = cache["accounts"].get(str(user_id)) or {}
    for item in entry.get("collections") or []:
        if isinstance(item, dict) and item.get("url") == url:
            item["checked_at"] = (when or datetime.now(timezone.utc)).isoformat()
            save_saved_cache(cache)
            return True
    return False


_track_wake = threading.Event()
_tracker_started = False
_tracker_lock = threading.Lock()


def wake_collection_tracker():
    _track_wake.set()


def _run_due_tracks(job, now=None):
    """Download each tracked collection that is due. One collection at a time."""
    try:
        cookies, _settings = require_cookies_file()
    except ValueError:
        return []
    user_id = cookie_user_id(cookies)
    if not user_id:
        return []
    now = now or datetime.now(timezone.utc)
    entry = load_saved_cache()["accounts"].get(user_id) or {}
    started = []
    for item in entry.get("collections") or []:
        if not collection_is_due(item, now):
            continue
        if job.status().get("running"):
            break
        url = item.get("url") or ""
        try:
            job.start_saved({
                "name": item.get("name") or "",
                "url": url,
                "retry_failed": True,
                "remember_url_file": False,
            })
        except (DownloadBusy, ValueError):
            break
        while job.status().get("running"):
            time.sleep(1)
        if any(event.get("type") == "error" for event in job.status().get("events") or []):
            break
        mark_collection_checked(user_id, url)
        started.append(url)
    return started


def _track_loop(job):
    if _track_wake.wait(TRACK_FIRST_WAIT_SECONDS):
        _track_wake.clear()
    while True:
        try:
            _run_due_tracks(job)
        except Exception:
            pass
        _track_wake.wait(TRACK_POLL_SECONDS)
        _track_wake.clear()


def start_collection_tracker(job):
    global _tracker_started
    with _tracker_lock:
        if _tracker_started:
            return
        _tracker_started = True
    threading.Thread(target=_track_loop, args=(job,), name="ig-track", daemon=True).start()


def require_cookies_file():
    settings = load_dashboard_settings()
    cookies = settings.get("cookies") or ""
    if not cookies or not os.path.isfile(cookies):
        raise ValueError("Choose a cookies file first.")
    if not path_is_under(cookies, storage_roots()):
        raise ValueError("Cookies file must stay inside your user folder or the data folder.")
    return cookies, settings


def playwright_cookies(cookies_file):
    """Netscape cookies for Playwright. Values stay on this machine."""
    import http.cookiejar
    jar = http.cookiejar.MozillaCookieJar(cookies_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError as exc:
        raise ValueError("The cookies file could not be read.") from exc
    cookies = []
    for cookie in jar:
        domain = cookie.domain or ""
        if "instagram.com" not in domain:
            continue
        item = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain if domain.startswith(".") else domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": cookie.name == "sessionid" or "HttpOnly" in (getattr(cookie, "_rest", {}) or {}),
        }
        if cookie.expires:
            item["expires"] = int(cookie.expires)
        cookies.append(item)
    if not any(item["name"] == "sessionid" for item in cookies):
        raise ValueError(_LOGIN_AGAIN)
    return cookies


class SavedPageError(ValueError):
    """The saved page stopped early. ``urls`` are the posts read so far."""

    def __init__(self, message, urls=None):
        super().__init__(message)
        self.urls = list(urls or [])


def _open_instagram(cookies_file):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError(
            "Reading the saved page needs Playwright. "
            "Run: pip install playwright && python -m playwright install chromium"
        ) from exc
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
    except Exception as exc:
        pw.stop()
        raise ValueError(
            "Chromium for Playwright is not installed. Run: python -m playwright install chromium"
        ) from exc
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    context.add_cookies(playwright_cookies(cookies_file))
    return pw, browser, context.new_page()


def _click_retry(page):
    try:
        page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('button, [role="button"]')) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text === 'retry' || text === 'try again') { el.click(); return true; }
                }
                return false;
            }"""
        )
    except Exception:
        return False


def _scroll_collect(page, parse_fn, stop_event=None, on_step=None, idle_rounds=3, max_steps=500):
    """Scroll like the userscript until the page stops growing. Returns the last parse."""
    seen_height = 0
    idle = 0
    last = parse_fn("")
    for _step in range(max_steps):
        if stop_event is not None and stop_event.is_set():
            break
        page_html = page.content()
        if looks_like_login(page.url, page_html):
            raise SavedPageError(_LOGIN_AGAIN, parse_fn(page_html))
        last = parse_fn(page_html)
        if on_step:
            on_step(last)
        _click_retry(page)
        height = page.evaluate("() => document.body ? document.body.scrollHeight : 0") or 0
        near_bottom = page.evaluate(
            "() => window.scrollY + window.innerHeight >= (document.body ? document.body.scrollHeight : 0) - 600"
        )
        page.evaluate("() => window.scrollBy(0, Math.max(window.innerHeight * 0.85, 400))")
        page.wait_for_timeout(700)
        if near_bottom and height <= seen_height + 40:
            idle += 1
        else:
            idle = 0
        if height > seen_height:
            seen_height = height
        if idle >= idle_rounds:
            break
    page_html = page.content()
    if looks_like_login(page.url, page_html):
        raise SavedPageError(_LOGIN_AGAIN, parse_fn(page_html))
    return parse_fn(page_html)


def _close_instagram(pw, browser):
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


_VISIBLE_POSTS_JS = r"""
() => {
  const found = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll('a[href]')) {
    const match = (anchor.getAttribute('href') || '').match(/\/(p|reel)\/([A-Za-z0-9_-]+)/);
    if (!match || seen.has(match[2])) continue;
    seen.add(match[2]);
    found.push([match[1], match[2]]);
  }
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    const text = (el.textContent || '').trim().toLowerCase();
    if (text === 'retry' || text === 'try again') {
      return { posts: found, moved: false, retry: true };
    }
  }
  const link = document.querySelector('a[href*="/p/"], a[href*="/reel/"]');
  const step = Math.max((window.innerHeight || 800) * 0.72, 480);
  const chain = [];
  let node = link ? link.parentElement : null;
  while (node) {
    chain.push(node);
    node = node.parentElement;
  }
  chain.push(document.scrollingElement, document.documentElement, document.body);
  const tried = new Set();
  let moved = false;
  for (const target of chain) {
    if (!target || tried.has(target)) continue;
    tried.add(target);
    const before = target.scrollTop || 0;
    const extra = (target.scrollHeight || 0) - (target.clientHeight || 0);
    const isRoot = target === document.scrollingElement || target === document.documentElement;
    if (extra <= 8 && !isRoot) continue;
    target.scrollTop = before + step;
    if ((target.scrollTop || 0) > before + 2) {
      moved = true;
      break;
    }
  }
  if (!moved) window.scrollBy(0, step);
  return { posts: found, moved: moved || window.scrollY > 0 };
}
"""


def _wheel_feed(page):
    """A real wheel event, for when Instagram ignores a scripted scrollTop."""
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        return
    try:
        mouse.move(640, 560)
        mouse.wheel(0, 1100)
    except Exception:
        return


def _pairs_from_collect(raw):
    if isinstance(raw, dict):
        return raw.get("posts") or [], bool(raw.get("moved")), bool(raw.get("retry"))
    return raw or [], True, False


def _gather_post_urls(page, max_steps=80, on_count=None, stop_event=None, progress_cb=None):
    """Collect post links while scrolling.

    Instagram removes links that scroll off screen, so each pass is merged
    into the posts already seen. The count therefore only stays or grows.
    The first screen is 12 posts. Scrolling has to move the list that holds
    those links, or the page never loads the rest.
    """
    if looks_like_login(page.url, ""):
        raise SavedPageError(_LOGIN_AGAIN)
    seen = set()
    urls = []
    last = None
    idle = 0
    rate_waits = 0
    for step in range(max_steps):
        if stop_event is not None and stop_event.is_set():
            break
        if looks_like_login(page.url, ""):
            raise SavedPageError(_LOGIN_AGAIN, urls)
        pairs, moved, retry = _pairs_from_collect(page.evaluate(_VISIBLE_POSTS_JS))
        if retry:
            idle = 0
            rate_waits += 1
            if rate_waits > 2:
                raise ValueError(
                    f"Instagram is rate limiting this collection. It loaded {len(urls)} posts "
                    "and then stopped. Wait a bit and try the download again."
                )
            if not _wait_rate_limit(rate_waits, stop_event=stop_event, progress_cb=progress_cb):
                break
            _click_retry(page)
            page.wait_for_timeout(1500)
            continue
        if not moved:
            _wheel_feed(page)
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            kind, code = pair
            if kind not in ("p", "reel") or not code or code in seen:
                continue
            seen.add(code)
            urls.append(f"https://www.instagram.com/{kind}/{code}/")
        count = len(urls)
        if on_count and count != last:
            on_count(count)
        if count == last:
            idle += 1
            if idle >= 4 and (count > 0 or step >= 6):
                break
        else:
            idle = 0
            last = count
        page.wait_for_timeout(700)
    if looks_like_login(page.url, ""):
        raise SavedPageError(_LOGIN_AGAIN, urls)
    return urls


def _count_collection_posts(page, max_steps=80):
    """How many posts are in the collection that is already open."""
    count = len(_gather_post_urls(page, max_steps=max_steps))
    return count or None


def list_saved_collections(count_posts=False):
    """Open the saved page and return the collection names and links on it."""
    cookies, _settings = require_cookies_file()
    with _BROWSER_LOCK:
        pw, browser, page = _open_instagram(cookies)
        try:
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            if looks_like_login(page.url, page.content()):
                raise ValueError(_LOGIN_AGAIN)
            username = parse_profile_username(page.content())
            if not username:
                raise ValueError(
                    "Could not find your profile link. Export cookies.txt again and retry."
                )
            page.goto(
                f"https://www.instagram.com/{username}/saved/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(1500)
            try:
                page.wait_for_selector('a[href*="/saved/"]', timeout=15000)
            except Exception:
                pass
            collections = _scroll_collect(
                page, lambda page_html: parse_saved_collections(page_html, username)
            )
            if count_posts:
                for item in collections:
                    try:
                        page.goto(item["url"], wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(1200)
                        counted = _count_collection_posts(page)
                    except SavedPageError:
                        raise ValueError(_LOGIN_AGAIN) from None
                    except ValueError as exc:
                        if "rate limit" in str(exc).lower():
                            break
                        counted = None
                    except Exception:
                        counted = None
                    if counted is not None:
                        item["count"] = counted
            return {"username": username, "collections": collections}
        finally:
            _close_instagram(pw, browser)


def _attach_local_counts(collections, username, output_dir):
    """The downloaded number comes from that account's folders, not the cached total."""
    if output_dir and os.path.isdir(output_dir):
        annotate_collection_counts(collections, [output_dir], account=username or "")
    else:
        for item in collections or []:
            item["saved"] = 0
            item["failed"] = 0
    return collections


def saved_collections_view():
    """The cached list for the cookies file that is selected now. No browser."""
    try:
        cookies, settings = require_cookies_file()
    except ValueError:
        return {"username": "", "user_id": "", "collections": []}
    user_id = cookie_user_id(cookies)
    entry = load_saved_cache()["accounts"].get(user_id) or {}
    username = entry.get("username") or ""
    collections = []
    for item in entry.get("collections") or []:
        if isinstance(item, dict):
            collections.append(dict(item))
    output_dir = _resolve_user_path(settings.get("output_dir") or "downloads")
    _attach_local_counts(collections, username, output_dir)
    return {
        "username": username,
        "user_id": user_id,
        "updated": entry.get("updated") or "",
        "collections": collections,
    }


def refresh_saved_collections():
    """Open Instagram, recount every collection, and replace this account's cache."""
    cookies, settings = require_cookies_file()
    user_id = cookie_user_id(cookies)
    if not user_id:
        raise ValueError("The cookies file has no account id. Export cookies.txt again.")
    payload = list_saved_collections(count_posts=True)
    username = payload.get("username") or ""
    store_saved_collections(user_id, username, payload.get("collections"))
    output_dir = _resolve_user_path(settings.get("output_dir") or "downloads")
    _attach_local_counts(payload.get("collections"), username, output_dir)
    payload["user_id"] = user_id
    payload["updated"] = load_saved_cache()["accounts"].get(user_id, {}).get("updated") or ""
    return payload


def collect_collection_posts(cookies_file, collection_url, stop_event=None, progress_cb=None):
    """Open one collection page, scroll to the end, and return post and reel URLs."""
    if not is_saved_collection_url(collection_url):
        raise ValueError("That is not a saved collection link.")
    with _BROWSER_LOCK:
        pw, browser, page = _open_instagram(cookies_file)
        try:
            page.goto(collection_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)

            def on_count(count):
                if progress_cb:
                    progress_cb({
                        "type": "collecting",
                        "found": count,
                        "message": f"Reading the collection… {count} posts",
                    })

            return _gather_post_urls(
                page, on_count=on_count, stop_event=stop_event, progress_cb=progress_cb
            )
        finally:
            _close_instagram(pw, browser)


def write_collection_url_file(name, urls, directory=None):
    """Store the collected links. Saved runs keep the file in that account's folder."""
    folder = collection_folder_name(name)
    if not directory:
        directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Collections")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{folder}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        for url in urls:
            fh.write(url + "\n")
    return path


def prepare_saved_download(params):
    """Check a saved-collection request before the browser opens."""
    if not isinstance(params, dict):
        raise ValueError("Expected a JSON object.")
    cookies, settings = require_cookies_file()
    name = collection_folder_name(params.get("name"))
    url = (params.get("url") or "").strip()
    if not is_saved_collection_url(url):
        raise ValueError("That is not a saved collection link.")
    output_full = _resolve_user_path(settings.get("output_dir") or "downloads")
    _require_storage_dir(output_full, "Output folder")
    account = account_from_saved_url(url)
    if account:
        output_full = account_output_dir(output_full, account)
        _require_storage_dir(output_full, "Output folder")
    template = validate_filename_template(settings.get("filename_template") or "{shortcode}")
    retry = params.get("retry_failed", True)
    return {
        "cookies": cookies,
        "collection": name,
        "url": url,
        "output_dir": output_full,
        "filename_template": template,
        "retry_failed": True if retry is None else bool(retry),
    }


class DownloadBusy(RuntimeError):
    """A download is already running in this process."""


def prepare_download_request(params):
    """Validate a dashboard start request and remember the reusable fields."""
    if not isinstance(params, dict):
        raise ValueError("Expected a JSON object.")
    url_file = (params.get("url_file") or "").strip()
    if not url_file:
        raise ValueError("Choose a URL file.")
    collection = validate_collection(params.get("collection"))
    output_dir = (params.get("output_dir") or "downloads").strip() or "downloads"
    cookies = (params.get("cookies") or "").strip()
    mode = params.get("cookies_mode") or COOKIES_MODE_FALLBACK
    if mode not in COOKIES_MODES:
        raise ValueError("Unknown cookies mode.")
    template = validate_filename_template(params.get("filename_template") or "{shortcode}")

    def inside_storage(path, label, must_be_file):
        full = _resolve_user_path(path)
        if not path_is_under(full, storage_roots()):
            raise ValueError(f"{label} must stay inside your user folder or the data folder.")
        if must_be_file and not os.path.isfile(full):
            raise ValueError(f"{label} was not found.")
        return full

    url_full = inside_storage(url_file, "URL file", True)
    if cookies:
        cookies = inside_storage(cookies, "Cookies file", True)
    output_full = _resolve_user_path(output_dir)
    _require_storage_dir(output_full, "Output folder")
    account = cached_account_username(cookie_user_id(cookies)) if cookies else ""

    save_dashboard_settings({
        "url_file": url_full,
        "output_dir": output_full,
        "cookies": cookies,
        "cookies_mode": mode,
        "filename_template": template,
    })
    if account:
        output_full = account_output_dir(output_full, account)
        _require_storage_dir(output_full, "Output folder")
    return {
        "url_file": url_full,
        "collection": collection,
        "output_dir": output_full,
        "cookies": cookies,
        "cookies_mode": mode,
        "filename_template": template,
        "dry_run": bool(params.get("dry_run")),
        "retry_failed": bool(params.get("retry_failed")),
    }


class DownloadJob:
    """One background download. The dashboard polls status() for new events."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = None
        self._events = []
        self._running = False

    def status(self, since=0):
        with self._lock:
            try:
                since = max(0, int(since))
            except (TypeError, ValueError):
                since = 0
            return {
                "running": self._running,
                "events": list(self._events[since:]),
                "next": len(self._events),
            }

    def _append(self, event):
        with self._lock:
            self._events.append(event)

    def stop(self):
        with self._lock:
            stop = self._stop
        if stop is not None:
            stop.set()
        return self.status()

    def _begin(self):
        with self._lock:
            if self._running:
                raise DownloadBusy("A download is already running.")
            self._events = []
            self._running = True
            self._stop = threading.Event()
            return self._stop

    def _finish(self):
        with self._lock:
            self._running = False

    def start(self, params):
        prepared = prepare_download_request(params)
        stop = self._begin()

        def _run():
            try:
                run_download(
                    url_file=prepared["url_file"],
                    collection=prepared["collection"],
                    cookies=prepared["cookies"] or None,
                    cookies_mode=prepared["cookies_mode"],
                    output_base=prepared["output_dir"],
                    retry_failed=prepared["retry_failed"],
                    dry_run=prepared["dry_run"],
                    filename_template=prepared["filename_template"],
                    progress_cb=self._append,
                    stop_event=stop,
                )
            except Exception as exc:
                self._append({"type": "error", "message": str(exc)})
            finally:
                self._finish()

        threading.Thread(target=_run, name="ig-download-job", daemon=True).start()
        return self.status()

    def start_saved(self, params):
        """Read one saved collection from its page, then download those posts."""
        prepared = prepare_saved_download(params)
        stop = self._begin()

        def _run():
            try:
                self._append({
                    "type": "collecting",
                    "found": 0,
                    "message": "Opening the collection…",
                })
                try:
                    urls = collect_collection_posts(
                        prepared["cookies"], prepared["url"], stop, self._append
                    )
                except SavedPageError as exc:
                    if exc.urls:
                        write_collection_url_file(
                            prepared["collection"], exc.urls, directory=prepared["output_dir"]
                        )
                    raise
                if stop.is_set():
                    if urls:
                        write_collection_url_file(
                            prepared["collection"], urls, directory=prepared["output_dir"]
                        )
                    self._append({"type": "stopped"})
                    return
                if not urls:
                    raise ValueError("No posts found in that collection.")
                url_file = write_collection_url_file(
                    prepared["collection"], urls, directory=prepared["output_dir"]
                )
                if params.get("remember_url_file", True):
                    save_dashboard_settings({"url_file": url_file})
                self._append({
                    "type": "collecting",
                    "found": len(urls),
                    "message": f"Found {len(urls)} posts. Starting download.",
                })
                run_download(
                    url_file=url_file,
                    collection=prepared["collection"],
                    cookies=prepared["cookies"],
                    cookies_mode=COOKIES_MODE_ALWAYS,
                    output_base=prepared["output_dir"],
                    retry_failed=prepared.get("retry_failed", True),
                    filename_template=prepared["filename_template"],
                    progress_cb=self._append,
                    stop_event=stop,
                )
            except Exception as exc:
                self._append({"type": "error", "message": str(exc)})
            finally:
                self._finish()

        threading.Thread(target=_run, name="ig-saved-job", daemon=True).start()
        return self.status()


class _DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        server = args[2] if len(args) > 2 else kwargs.get("server")
        super().__init__(*args, directory=server.ui_root, **kwargs)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/collections":
            self.server.library_roots = library_roots()
            self._send_json({"collections": discover_collections(self.server.library_roots)})
            return
        if parsed.path == "/api/settings":
            self._send_json(load_dashboard_settings())
            return
        if parsed.path == "/api/browse":
            raw = urllib.parse.parse_qs(parsed.query).get("path", [""])[0]
            try:
                self._send_json(browse_directory(raw or None))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            return
        if parsed.path == "/api/download/status":
            since = urllib.parse.parse_qs(parsed.query).get("since", ["0"])[0]
            self._send_json(self._job().status(since))
            return
        if parsed.path == "/api/saved/collections":
            try:
                self._send_json(saved_collections_view())
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            return
        if parsed.path == "/api/log":
            self._serve_allowed(parsed.query, kind="log")
            return
        if parsed.path == "/api/poster":
            self._serve_poster(parsed.query)
            return
        if parsed.path == "/api/media":
            self._serve_media(parsed.query)
            return
        if parsed.path.startswith("/api/"):
            self.send_error(404)
            return
        library_file = self._library_json(parsed.path)
        if library_file:
            self._stream_file(library_file, "application/json")
            return
        if not self._ui_file_exists(parsed.path):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        data = self._read_json()
        if data is None:
            return
        if parsed.path == "/api/settings":
            self._send_json(save_dashboard_settings(data))
            return
        if parsed.path == "/api/download":
            try:
                self._send_json(self._job().start(data))
            except DownloadBusy as exc:
                self._send_error_json(409, str(exc))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            return
        if parsed.path == "/api/saved/refresh":
            if self._job().status()["running"]:
                self._send_error_json(409, "A download is already running.")
                return
            try:
                self._send_json(refresh_saved_collections())
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            return
        if parsed.path == "/api/saved/track":
            try:
                payload = update_saved_tracking(data)
            except ValueError as exc:
                self._send_error_json(400, str(exc))
                return
            wake_collection_tracker()
            self._send_json(payload)
            return
        if parsed.path == "/api/saved/download":
            try:
                self._send_json(self._job().start_saved(data))
            except DownloadBusy as exc:
                self._send_error_json(409, str(exc))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            return
        if parsed.path == "/api/download/stop":
            self._send_json(self._job().stop())
            return
        self.send_error(404)

    def _job(self):
        if not hasattr(self.server, "job"):
            self.server.job = DownloadJob()
        return self.server.job

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error_json(400, "Invalid request.")
            return None
        if length > 65536:
            self._send_error_json(413, "Request is too large.")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "Expected JSON.")
            return None
        if not isinstance(data, dict):
            self._send_error_json(400, "Expected a JSON object.")
            return None
        return data

    def _send_error_json(self, code, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_poster(self, query):
        raw = urllib.parse.parse_qs(query).get("path", [""])[0]
        if not raw or not path_is_under(raw, self.server.library_roots):
            self.send_error(403)
            return
        poster = ensure_video_poster(os.path.realpath(raw))
        if not poster:
            self.send_error(404)
            return
        self._stream_file(poster, "image/jpeg", cache_control="public, max-age=86400")

    def _library_json(self, url_path):
        """A collection log lives beside its downloads, outside the React build."""
        rel = urllib.parse.unquote(url_path).lstrip("/")
        if not rel.lower().endswith(".json"):
            return None
        root = os.path.realpath(self.server.web_root)
        candidate = os.path.realpath(os.path.join(root, rel))
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            return None
        if not inside or not os.path.isfile(candidate):
            return None
        if not _looks_like_download_log(candidate):
            return None
        return candidate

    def _ui_file_exists(self, url_path):
        rel = urllib.parse.unquote(url_path).lstrip("/")
        if not rel:
            rel = "index.html"
        candidate = os.path.realpath(os.path.join(self.server.ui_root, rel))
        root = os.path.realpath(self.server.ui_root)
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            return False
        return inside and os.path.isfile(candidate)

    def _serve_media(self, query):
        raw = urllib.parse.parse_qs(query).get("path", [""])[0]
        if not raw or not path_is_under(raw, self.server.library_roots):
            self.send_error(403)
            return
        full = os.path.realpath(raw)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._stream_file(full, content_type)

    def _stream_file(self, full, content_type, cache_control="no-store"):
        file_size = os.path.getsize(full)
        start, end = 0, file_size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].split(",")[0].strip()
            left, _, right = spec.partition("-")
            if left == "" and right:
                start = max(file_size - int(right), 0)
            else:
                start = int(left or 0)
                end = int(right) if right else file_size - 1
            status = 206
        start = max(0, start)
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            self.send_error(416)
            return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        with open(full, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionAbortedError):
                    return
                remaining -= len(chunk)

    def _serve_allowed(self, query, kind):
        raw = urllib.parse.parse_qs(query).get("path", [""])[0]
        if not raw or not path_is_under(raw, self.server.library_roots):
            self.send_error(403)
            return
        full = os.path.realpath(raw)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        if kind == "log" and not full.lower().endswith(".json"):
            self.send_error(403)
            return
        ctype = "application/json" if kind == "log" else (mimetypes.guess_type(full)[0] or "application/octet-stream")
        try:
            fh = open(full, "rb")
        except OSError:
            self.send_error(404)
            return
        try:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            shutil.copyfileobj(fh, self.wfile)
        finally:
            fh.close()


def dashboard_listen_host(host=None):
    """Listen address for the dashboard. Localhost unless --host or IGRIP_HOST says otherwise."""
    raw = host if host is not None else os.environ.get("IGRIP_HOST", "")
    raw = (raw or "127.0.0.1").strip()
    return raw or "127.0.0.1"


def _local_lan_ip():
    """An address other devices on the LAN can open. Empty when none is known."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 1))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return ""
    if not ip or ip.startswith("127."):
        return ""
    return ip


def dashboard_page_url(host, port, page):
    shown = host
    if host in ("0.0.0.0", "::"):
        shown = _local_lan_ip() or "127.0.0.1"
    return f"http://{shown}:{port}{page}"


def ensure_dashboard_server(extra_roots=None, port=8765, host=None):
    """Serve the dashboard and collection logs. Returns the page URL."""
    global _dashboard_server
    script_dir = os.path.dirname(os.path.abspath(__file__))
    roots = library_roots(extra_roots)
    bind_host = dashboard_listen_host(host)
    with _dashboard_lock:
        if _dashboard_server is None:
            httpd = None
            bind_error = None
            for candidate in range(port, port + 10):
                try:
                    httpd = ThreadingHTTPServer((bind_host, candidate), _DashboardHandler)
                    break
                except OSError as exc:
                    bind_error = exc
            if httpd is None:
                raise bind_error
            httpd.web_root = script_dir
            httpd.ui_root = dashboard_ui_root(script_dir)
            httpd.library_roots = roots
            httpd.job = DownloadJob()
            start_collection_tracker(httpd.job)
            thread = threading.Thread(target=httpd.serve_forever, name="ig-dashboard", daemon=True)
            thread.start()
            _dashboard_server = httpd
        else:
            for root in roots:
                if root not in _dashboard_server.library_roots:
                    _dashboard_server.library_roots.append(root)
        bound = _dashboard_server.server_address[1]
        bound_host = _dashboard_server.server_address[0]
    page = "/" if os.path.basename(_dashboard_server.ui_root) == "dist" else "/ig_dashboard.html"
    return dashboard_page_url(bound_host, bound, page)


# ---------------------------------------------------------------------------
# Quiet logger
# ---------------------------------------------------------------------------

class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------

def _ydl_opts(outtmpl=None, cookies_file=None, skip_download=False, noplaylist=True):
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": noplaylist,
    }
    if skip_download:
        opts["skip_download"] = True
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["embedmetadata"] = True
    if outtmpl:
        opts["outtmpl"] = outtmpl
    if cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


def yt_extract_info(url, cookies_file):
    # noplaylist=False so carousels return their full entries list.
    # process=False keeps photo slides. Resolving the playlist makes yt-dlp
    # treat the first photo as a failed video and abort the whole post.
    with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True, cookies_file=cookies_file, noplaylist=False)) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
        if info is None:
            raise RuntimeError("yt-dlp returned no info")
        if info.get("_type") != "playlist":
            info = ydl.process_ie_result(info, download=False)
    return info


def yt_download(url, outtmpl, cookies_file):
    with yt_dlp.YoutubeDL(_ydl_opts(outtmpl=outtmpl, cookies_file=cookies_file)) as ydl:
        retcode = ydl.download([url])
    if retcode != 0:
        raise RuntimeError(f"yt-dlp download failed with return code {retcode}")


def carousel_folder(output_dir, shortcode):
    """One folder per carousel, named with the post shortcode."""
    return os.path.join(output_dir, shortcode)


_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
_LOOSE_SLIDE_RE = re.compile(r"^(?P<code>.+)_(\d+)\.[^.]+$", re.IGNORECASE)


def _slide_sort_key(path):
    name = os.path.basename(path)
    match = re.search(r"(\d+)(?=\.[^.]+$)", name)
    return (int(match.group(1)) if match else 0, name.lower())


def list_saved_media(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    files = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in _MEDIA_EXTS:
            files.append(path)
    return sorted(files, key=_slide_sort_key)


def settle_slides(output_dir, shortcode, saved):
    """Keep a multi-slide post inside output_dir/<shortcode>/.

    A single file stays where it was downloaded. Returns (saved_path, files).
    saved_path is the folder when there is more than one slide.
    """
    files = [p for p in (saved or []) if p and os.path.isfile(p)]
    files = sorted(files, key=_slide_sort_key)
    if len(files) <= 1:
        return (files[0] if files else None), files
    folder = carousel_folder(output_dir, shortcode)
    os.makedirs(folder, exist_ok=True)
    moved = []
    for path in files:
        dest = os.path.join(folder, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(dest):
            os.replace(path, dest)
        moved.append(dest)
    return folder, moved


def group_loose_carousel_files(collection_dir):
    """Move loose shortcode_1.jpg / shortcode_2.mp4 sets into shortcode folders."""
    try:
        names = os.listdir(collection_dir)
    except OSError:
        return {}
    groups = {}
    for name in names:
        path = os.path.join(collection_dir, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in _MEDIA_EXTS:
            continue
        match = _LOOSE_SLIDE_RE.match(name)
        if not match:
            continue
        groups.setdefault(match.group("code"), []).append(path)
    moved = {}
    for code, paths in groups.items():
        if len(paths) < 2:
            continue
        _, files = settle_slides(collection_dir, code, paths)
        moved[code] = files
    return moved


def regroup_collection_carousels(collection_dir):
    """Fold existing loose carousel files into post-id folders and update the log."""
    moved = group_loose_carousel_files(collection_dir)
    if not moved:
        return moved
    name = os.path.basename(os.path.abspath(collection_dir))
    log_path = os.path.join(collection_dir, f"{name}.json")
    if not os.path.isfile(log_path):
        return moved
    log = load_log(log_path)
    changed = False
    for item in log.get("items", []):
        code = item.get("shortcode")
        if code not in moved:
            continue
        item["media_type"] = "carousel"
        item["saved_files"] = moved[code]
        item["saved_path"] = carousel_folder(collection_dir, code)
        item["carousel_count"] = len(moved[code])
        item["status"] = item.get("status") or "ok"
        changed = True
    if changed:
        save_log(log, log_path)
    return moved


def _best_image_url(entry):
    """Largest thumbnail yt-dlp already found for a slide that has no video."""
    best_url = None
    best_area = -1
    for thumb in entry.get("thumbnails") or []:
        if not isinstance(thumb, dict) or not thumb.get("url"):
            continue
        area = (thumb.get("width") or 0) * (thumb.get("height") or 0)
        if area >= best_area:
            best_area = area
            best_url = thumb["url"]
    return best_url


def save_carousel_images(info, folder):
    """Save photo slides yt-dlp listed but will not download as video.

    Image slides have thumbnail URLs and no video formats. Returns the files written.
    """
    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        return []
    os.makedirs(folder, exist_ok=True)
    saved = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or entry.get("formats"):
            continue
        image_url = _best_image_url(entry)
        if not image_url:
            continue
        dest = os.path.join(folder, f"{index:02d}.jpg")
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            saved.append(dest)
            continue
        request = urllib.request.Request(image_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.instagram.com/",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
        except Exception:
            continue
        if not data:
            continue
        with open(dest, "wb") as fh:
            fh.write(data)
        saved.append(dest)
    return saved


def yt_download_carousel(url, output_dir, shortcode, cookies_file,
                         filename_template="{shortcode}", file_index=0):
    folder = carousel_folder(output_dir, shortcode)
    os.makedirs(folder, exist_ok=True)
    # Numbered slides live in the post folder so photos and videos stay together.
    outtmpl = os.path.join(folder, "%(playlist_index)02d.%(ext)s")
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "embedmetadata": True,
        "ignoreerrors": True,
        "outtmpl": outtmpl,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return list_saved_media(folder)


# ---------------------------------------------------------------------------
# instaloader wrapper
# ---------------------------------------------------------------------------

def _apply_instaloader_cookies(L, cookies_file):
    """Load a Netscape cookies.txt into an instaloader session (best effort).

    The cookies stay in the request jar. The context is deliberately not marked
    logged-in: instaloader then asks ``api/graphql`` for the post. Marking it
    logged-in switches that request to ``graphql/query``, which returns an empty
    body and breaks every photo carousel.
    """
    if not cookies_file or not os.path.exists(cookies_file):
        return False
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        L.context._session.cookies.update(jar)
        return True
    except Exception:
        return False


def instaloader_download(url, output_dir, cookies_file=None, download_videos=False):
    """Download a post using instaloader.

    Works without authentication for public posts. When ``cookies_file`` is
    supplied the session is authenticated with it - used as a fallback for
    age-restricted or private content only. Set ``download_videos=True`` when
    yt-dlp could not extract the post at all, so mixed carousels keep their
    videos instead of silently dropping them.
    """
    if not _INSTALOADER_AVAILABLE:
        raise RuntimeError("instaloader is not installed. Run: pip install instaloader")
    shortcode = shortcode_from_url(url)
    if shortcode == "unknown":
        raise RuntimeError(f"Cannot extract shortcode from URL: {url}")
    os.makedirs(output_dir, exist_ok=True)
    L = instaloader.Instaloader(
        dirname_pattern=output_dir,
        filename_pattern="{shortcode}",
        download_videos=download_videos,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )
    _apply_instaloader_cookies(L, cookies_file)
    try:
        before = set(os.listdir(output_dir))
    except OSError:
        before = set()
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target=pathlib.Path(output_dir))
    except instaloader.exceptions.InstaloaderException as e:
        raise RuntimeError(f"instaloader: {e}")
    try:
        after = set(os.listdir(output_dir))
        return sorted(
            os.path.join(output_dir, f)
            for f in (after - before)
            if os.path.splitext(f)[1].lower() in {'.jpg', '.jpeg', '.png', '.mp4', '.webp'}
        )
    except OSError:
        return []


def find_saved_files(output_dir, shortcode):
    folder = carousel_folder(output_dir, shortcode)
    if os.path.isdir(folder):
        media = list_saved_media(folder)
        if media:
            return media
    try:
        return [
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if shortcode in f and os.path.isfile(os.path.join(output_dir, f))
        ]
    except OSError:
        return []


def file_exists_on_disk(output_dir, shortcode):
    folder = carousel_folder(output_dir, shortcode)
    if os.path.isdir(folder) and list_saved_media(folder):
        return True
    try:
        return any(
            shortcode in f and os.path.isfile(os.path.join(output_dir, f))
            for f in os.listdir(output_dir)
        )
    except OSError:
        return False


def _saved_fields(output_dir, shortcode, saved, single_type):
    saved_path, files = settle_slides(output_dir, shortcode, saved)
    fields = {
        "media_type": "carousel" if len(files) > 1 else single_type,
        "saved_path": saved_path,
        "saved_files": files,
    }
    if len(files) > 1:
        fields["carousel_count"] = len(files)
    return fields


# ---------------------------------------------------------------------------
# Core per-URL handler
# ---------------------------------------------------------------------------

def _download_url_once(url, output_dir, cookies_file, dry_run,
                       filename_template="{shortcode}", file_index=0):
    shortcode = shortcode_from_url(url)
    timestamp = datetime.now(timezone.utc).isoformat()

    username = None
    title = None
    author = None
    caption = ""
    media_type = "unknown"

    try:
        info = yt_extract_info(url, cookies_file)
        username = (info.get("uploader_id") or info.get("uploader") or "unknown").lstrip("@")
        title = info.get("title") or shortcode
        author = extract_author_from_title(title, username)
        caption = (info.get("description") or "").strip()

        entries = info.get("entries")
        formats = info.get("formats") or []

        if entries is not None:
            media_type = "carousel"
        elif not formats:
            media_type = "image_only"
        else:
            media_type = "video"

    except yt_dlp.utils.DownloadError as e:
        err_str = strip_ansi(str(e))
        cat = classify_error(err_str)
        if cat == "image_only":
            if dry_run:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "dry_run", "timestamp": timestamp}
            try:
                saved = instaloader_download(url, output_dir, cookies_file,
                                             download_videos=True)
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "status": "ok", "timestamp": timestamp,
                        **_saved_fields(output_dir, shortcode, saved, "image")}
            except Exception as insta_err:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "failed",
                        "error_category": "instaloader_error",
                        "error_detail": str(insta_err), "timestamp": timestamp}
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "failed",
                "error_category": cat, "error_detail": err_str, "timestamp": timestamp}
    except Exception as e:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "failed",
                "error_category": "extract_error",
                "error_detail": strip_ansi(str(e)), "timestamp": timestamp}

    if dry_run:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "dry_run", "timestamp": timestamp}

    if media_type == "image_only":
        try:
            saved_files = instaloader_download(url, output_dir, cookies_file)
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "status": "ok", "timestamp": timestamp,
                    **_saved_fields(output_dir, shortcode, saved_files, "image")}
        except Exception as e:
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "image", "status": "failed",
                    "error_category": "instaloader_error",
                    "error_detail": str(e), "timestamp": timestamp}

    if media_type == "carousel":
        try:
            folder = carousel_folder(output_dir, shortcode)
            entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
            if any(entry.get("formats") for entry in entries):
                yt_download_carousel(url, output_dir, shortcode, cookies_file,
                                     filename_template, file_index)
            save_carousel_images(info, folder)
            saved = list_saved_media(folder)
            if saved:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "status": "ok", "timestamp": timestamp,
                        **_saved_fields(output_dir, shortcode, saved, "carousel")}
            # yt-dlp got nothing — likely an image-only carousel; try instaloader
            saved_insta = instaloader_download(
                url, carousel_folder(output_dir, shortcode), cookies_file,
                download_videos=True)
            if saved_insta:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "status": "ok", "timestamp": timestamp,
                        **_saved_fields(output_dir, shortcode, saved_insta, "carousel")}
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "carousel", "status": "failed",
                    "error_category": "no_files_saved",
                    "error_detail": "Neither yt-dlp nor instaloader saved any files for this carousel",
                    "timestamp": timestamp}
        except Exception as e:
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "carousel", "status": "failed",
                    "error_category": "other_error",
                    "error_detail": strip_ansi(str(e)), "timestamp": timestamp}

    outtmpl = build_outtmpl(filename_template, output_dir, file_index)
    try:
        yt_download(url, outtmpl, cookies_file)
        saved_files = find_saved_files(output_dir, shortcode)
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "ok",
                "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
    except yt_dlp.utils.DownloadError as e:
        err_str = strip_ansi(str(e))
        cat = classify_error(err_str)
        if cat == "image_only":
            try:
                saved_files = instaloader_download(url, output_dir, cookies_file,
                                                   download_videos=True)
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "status": "ok", "timestamp": timestamp,
                        **_saved_fields(output_dir, shortcode, saved_files, "image")}
            except Exception as insta_err:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "failed",
                        "error_category": "instaloader_error",
                        "error_detail": str(insta_err), "timestamp": timestamp}
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "failed",
                "error_category": cat, "error_detail": err_str, "timestamp": timestamp}
    except Exception as e:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "failed",
                "error_category": "other_error",
                "error_detail": strip_ansi(str(e)), "timestamp": timestamp}


def download_url(url, output_dir, cookies_file, dry_run,
                 filename_template="{shortcode}", file_index=0,
                 cookies_mode=COOKIES_MODE_FALLBACK):
    """Download a single URL, applying the cookie policy.

    Default policy (``"fallback"``): the first attempt always runs WITHOUT
    cookies. Cookies are sent on a second attempt only when the first one
    failed because the content is age-restricted or private/login-gated.
    ``"always"`` uses cookies on the first attempt; ``"never"`` ignores them.

    Every returned result carries ``used_cookies`` and ``cookie_fallback`` so
    callers (and the log) can tell exactly what happened.
    """
    if cookies_mode == COOKIES_MODE_NEVER or not cookies_file:
        result = _download_url_once(url, output_dir, None, dry_run,
                                    filename_template, file_index)
        result["used_cookies"] = False
        result["cookie_fallback"] = False
        return result

    if cookies_mode == COOKIES_MODE_ALWAYS:
        result = _download_url_once(url, output_dir, cookies_file, dry_run,
                                    filename_template, file_index)
        result["used_cookies"] = True
        result["cookie_fallback"] = False
        return result

    # Default: try unauthenticated first, fall back to cookies only if the
    # failure is due to an age gate or private/login-gated content.
    result = _download_url_once(url, output_dir, None, dry_run,
                                filename_template, file_index)
    result["used_cookies"] = False
    result["cookie_fallback"] = False

    if is_restricted_error(result):
        retry = _download_url_once(url, output_dir, cookies_file, dry_run,
                                   filename_template, file_index)
        retry["used_cookies"] = True
        retry["cookie_fallback"] = True
        retry["first_error_category"] = result.get("error_category")
        retry["first_error_detail"] = result.get("error_detail")
        return retry
    return result


# ---------------------------------------------------------------------------
# Programmatic entry point
# ---------------------------------------------------------------------------

def run_download(
    url_file,
    collection,
    log_file=None,
    cookies=None,
    cookies_mode=COOKIES_MODE_FALLBACK,
    output_base="downloads",
    retry_failed=False,
    dry_run=False,
    filename_template="{shortcode}",
    progress_cb=None,
    stop_event=None,
):
    """
    Download URLs from url_file into <output_base>/<collection>/.

    cookies_mode: when to use the cookies file - see COOKIES_MODE_*. The
                  default "fallback" sends cookies only for posts that fail
                  because they are age-restricted or private.
    output_base:  base folder for downloads (default "downloads"); each
                  collection is created as a subfolder inside it.
    filename_template: how to name output files — see FILENAME_VARIABLE_DOCS.
    progress_cb: optional callable(event_dict) for real-time progress reporting.
    stop_event:  optional threading.Event; set to cancel cleanly mid-run.
    Returns the final summary dict.
    """
    # Validate anything that becomes a filesystem path before doing real work.
    collection = validate_collection(collection)
    filename_template = validate_filename_template(filename_template)
    output_base = (output_base or "downloads").strip() or "downloads"

    if not _INSTALOADER_AVAILABLE:
        print("Warning: instaloader not installed — image-only downloads will fail.\n"
              "Install with: pip install instaloader", file=sys.stderr)

    if not dry_run and not ffmpeg_available():
        msg = ("ffmpeg was not found on PATH - yt-dlp cannot merge best video+audio, "
               "so video/reel downloads may be single-stream or fail.\n"
               "Install ffmpeg and add it to PATH: https://ffmpeg.org/download.html")
        print(f"Warning: {msg}\n", file=sys.stderr)
        if progress_cb:
            progress_cb({"type": "warning",
                         "message": "ffmpeg not found - video/audio merging may fail"})

    output_dir = resolve_output_dir(output_base, collection)
    os.makedirs(output_dir, exist_ok=True)
    # Log sits beside the media, so the dashboard can find it by collection folder.
    if not log_file:
        log_file = os.path.join(output_dir, f"{collection}.json")

    urls = load_urls(url_file)
    if not urls:
        raise ValueError(f"No URLs found in {url_file}")

    log = load_log_or_backup(log_file)
    if "items" not in log:
        log["items"] = []

    total = len(urls)

    before = len(urls)
    if retry_failed:
        urls = [u for u in urls if not already_done(log, u)]
    else:
        urls = [u for u in urls if not already_attempted(log, u)]
    skipped_auto = before - len(urls)

    if skipped_auto:
        print(f"Skipping {skipped_auto} already-processed URL(s) - {len(urls)} remaining.")
        if progress_cb:
            progress_cb({"type": "skip_info", "skipped": skipped_auto, "remaining": len(urls)})

    counts = {}
    skipped_disk = 0
    consecutive_throttle = 0
    cookie_fallbacks = 0
    i = 0

    if progress_cb:
        progress_cb({"type": "start", "total": total, "to_process": len(urls),
                     "log_file": log_file, "collection": collection})

    while i < len(urls):
        if stop_event and stop_event.is_set():
            print("\n  Download stopped by user.")
            if progress_cb:
                progress_cb({"type": "stopped"})
            break

        url = urls[i]
        shortcode = shortcode_from_url(url)

        if not dry_run and file_exists_on_disk(output_dir, shortcode):
            skipped_disk += 1
            print(f"[{i + 1 + skipped_auto}/{total}] {url}")
            print(f"  -> {shortcode} already on disk, skipping")
            if progress_cb:
                progress_cb({"type": "skipped_disk", "url": url, "shortcode": shortcode,
                             "index": i + skipped_auto, "total": total})
            i += 1
            continue

        print(f"[{i + 1 + skipped_auto}/{total}] {url}", flush=True)
        if progress_cb:
            progress_cb({"type": "downloading", "url": url, "shortcode": shortcode,
                         "index": i + skipped_auto, "total": total})

        result = download_url(url, output_dir, cookies, dry_run, filename_template,
                              i + skipped_auto, cookies_mode=cookies_mode)

        if result.get("cookie_fallback"):
            cookie_fallbacks += 1
            print("  RETRY with cookies (post is age/private restricted)")
            if progress_cb:
                progress_cb({"type": "cookie_fallback", "url": url, "shortcode": shortcode,
                             "first_error_category": result.get("first_error_category")})

        if result.get("error_category") in _TRANSIENT_ERROR_CATEGORIES:
            consecutive_throttle += 1
            if consecutive_throttle >= 5:
                # 5+ consecutive hits — treat as a real throttle, wait and retry
                if consecutive_throttle > _MAX_CONSECUTIVE_RATE_LIMITS:
                    print(f"\n  {consecutive_throttle} consecutive throttled responses - saving progress and aborting.")
                    if progress_cb:
                        progress_cb({"type": "rate_limit_abort", "consecutive": consecutive_throttle})
                    upsert_log_entry(log, result)
                    save_log(log, log_file)
                    break
                ok = _wait_rate_limit(consecutive_throttle, stop_event=stop_event, progress_cb=progress_cb)
                if not ok:
                    break
                continue  # retry same URL
            # Fewer than 5 consecutive hits — may be unavailable/deleted posts, log and continue
            print(f"  FAIL [{result.get('error_category')}] ({consecutive_throttle}x, not waiting yet) {result.get('error_detail', '')[:120]}")
        else:
            consecutive_throttle = 0
        upsert_log_entry(log, result)

        status = result["status"]
        counts[status] = counts.get(status, 0) + 1

        if status == "ok":
            display = result.get("author") or result.get("username") or "?"
            print(f"  OK {display} - {result.get('saved_path', '')}")
        elif status == "failed":
            print(f"  FAIL [{result.get('error_category', 'error')}] {result.get('error_detail', '')[:120]}")
        elif status == "dry_run":
            display = result.get("author") or result.get("username") or "?"
            print(f"  DRY {display} / {result.get('media_type', '?')}")

        if progress_cb:
            progress_cb({"type": "progress", "index": i + skipped_auto, "total": total,
                         "url": url, "shortcode": shortcode, "status": status,
                         "author": result.get("author") or result.get("username") or "?",
                         "error_category": result.get("error_category"),
                         "error_detail": (result.get("error_detail") or "")[:300],
                         "used_cookies": bool(result.get("used_cookies")),
                         "cookie_fallback": bool(result.get("cookie_fallback"))})

        summary = {
            "total_in_file": total,
            "processed": skipped_auto + i + 1,
            "ok": counts.get("ok", 0),
            "failed": counts.get("failed", 0),
            "dry_run": counts.get("dry_run", 0),
            "skipped_auto": skipped_auto,
            "cookie_fallbacks": cookie_fallbacks,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        log["summary"] = summary
        save_log(log, log_file)
        i += 1

    final_summary = log.get("summary") or {
        "total_in_file": total, "processed": skipped_auto + i,
        "ok": counts.get("ok", 0), "failed": counts.get("failed", 0),
        "dry_run": counts.get("dry_run", 0), "skipped_auto": skipped_auto,
        "cookie_fallbacks": cookie_fallbacks,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    regroup_collection_carousels(output_dir)

    print("\n--- Done ---")
    print(f"  OK:      {counts.get('ok', 0)}")
    print(f"  Failed:  {counts.get('failed', 0)}")
    print(f"  Skipped: {skipped_auto} (already in log)")
    if skipped_disk:
        print(f"  Skipped: {skipped_disk} (file already on disk)")
    if cookie_fallbacks:
        print(f"  Cookies: {cookie_fallbacks} restricted post(s) retried with cookies")
    if dry_run:
        print(f"  Dry-run: {counts.get('dry_run', 0)}")
    print(f"  Output:  {output_dir}")
    print(f"  Log:     {log_file}")

    if progress_cb:
        progress_cb({"type": "done", "summary": final_summary})

    return final_summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_CTRL_HANDLER = None


def _exit_dashboard():
    """Leave the console immediately. A browser child can otherwise keep it open."""
    server = _dashboard_server
    job = getattr(server, "job", None) if server is not None else None
    if job is not None:
        try:
            job.stop()
        except Exception:
            pass
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
    os._exit(0)


def _install_ctrl_c_exit():
    """Ctrl+C must stop the dashboard even when Windows will not interrupt a wait."""
    global _CTRL_HANDLER
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    routine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _handler(ctrl_type):
        # 0 is Ctrl+C, 1 is Ctrl+Break.
        if ctrl_type in (0, 1):
            os._exit(0)
        return False

    _CTRL_HANDLER = routine(_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True)


def main():
    parser = argparse.ArgumentParser(
        description="Download Instagram posts from a URL list using yt-dlp + instaloader."
    )
    parser.add_argument("url_file", nargs="?")
    parser.add_argument("collection", nargs="?")
    parser.add_argument("-l", "--log", default=None,
                        help="Log file path (default: <output>/<collection>/<collection>.json)")
    parser.add_argument("--dashboard", action="store_true",
                        help="Open the dashboard and keep serving collection logs")
    parser.add_argument("--cookies", default=None, metavar="FILE",
                        help=("Path to a cookies.txt file. Only used when a post is "
                              "age-restricted or private (see --cookies-mode)."))
    parser.add_argument("--cookies-mode", choices=COOKIES_MODES,
                        default=COOKIES_MODE_FALLBACK,
                        help=("When to use the cookies file. 'fallback' (default): "
                              "only for age/private restricted posts. 'always': on "
                              "every request. 'never': ignore the cookies file."))
    parser.add_argument("--output-dir", default="downloads", metavar="DIR",
                        help=("Base folder for downloads (default: downloads). "
                              "Each collection is created as a subfolder."))
    parser.add_argument("--host", default=None,
                        help="Dashboard listen address. Default 127.0.0.1. Use 0.0.0.0 on the NAS, or set IGRIP_HOST.")
    parser.add_argument("--data-dir", default=None, metavar="DIR",
                        help="Folder for the library, cookies, and saved settings. Or set IGRIP_DATA.")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--filename-template", default="{shortcode}",
                        metavar="TEMPLATE",
                        help=(
                            "Output filename template. Default: {shortcode}. "
                            "Variables: {shortcode} {author} {title} {upload_date} "
                            "{date} {index} {index:04d}. "
                            "Example: --filename-template '{author}_{shortcode}'"
                        ))
    args = parser.parse_args()
    if args.data_dir:
        set_data_dir(args.data_dir)

    if args.dashboard:
        import webbrowser
        host = dashboard_listen_host(args.host)
        url = ensure_dashboard_server(extra_roots=[args.output_dir], host=host)
        print(f"Dashboard: {url}")
        print("Press Ctrl+C to stop.", flush=True)
        if host in ("127.0.0.1", "::1", "localhost"):
            webbrowser.open(url)
        _install_ctrl_c_exit()
        try:
            # Event.wait() never wakes for Ctrl+C on Windows. sleep() does.
            while True:
                time.sleep(0.25)
        except KeyboardInterrupt:
            _exit_dashboard()
        return

    if not args.url_file or not args.collection:
        parser.error("url_file and collection are required unless --dashboard is used")

    run_download(
        url_file=args.url_file,
        collection=args.collection,
        log_file=args.log,
        cookies=args.cookies,
        cookies_mode=args.cookies_mode,
        output_base=args.output_dir,
        retry_failed=args.retry_failed,
        dry_run=args.dry_run,
        filename_template=args.filename_template,
    )


if __name__ == "__main__":
    main()
