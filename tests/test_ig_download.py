"""Tests for ig_download.py.

Run either way:
    python -m pytest tests -q
    python tests/test_ig_download.py

No network access is required.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import ig_download as ig  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────

def _expect_raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    raise AssertionError(f"expected {exc.__name__}")


def _fake_once(result, calls):
    def fake(url, output_dir, cookies_file, dry_run,
             filename_template="{shortcode}", file_index=0):
        calls.append(cookies_file)
        return dict(result)
    return fake


RESTRICTED = {"status": "failed", "error_category": "private_or_restricted",
              "error_detail": "This content isn't available to everyone: certain audiences"}
PARSE = {"status": "failed", "error_category": "parse_error",
         "error_detail": "Failed to parse JSON (Expecting value in '')"}
OK = {"status": "ok", "error_category": None, "error_detail": None}


# ─────────────────────────────────────────────────────────────────────────
# classify_error / is_restricted_error
# ─────────────────────────────────────────────────────────────────────────

def test_classify_error():
    assert ig.classify_error("HTTP Error 429: Too Many Requests") == "rate_limited"
    assert ig.classify_error("rate-limit reached") == "rate_limited"
    assert ig.classify_error("Failed to parse JSON (JSONDecodeError)") == "parse_error"
    assert ig.classify_error("Unable to extract data - Failed to parse JSON") == "parse_error"
    assert ig.classify_error("This content isn't available to everyone") == "private_or_restricted"
    assert ig.classify_error("Sign in to confirm your age") == "private_or_restricted"
    assert ig.classify_error("There is no video in this post") == "image_only"
    assert ig.classify_error("Unable to download webpage: timed out") == "network_error"


def test_is_restricted_error():
    assert ig.is_restricted_error(RESTRICTED)
    assert not ig.is_restricted_error(PARSE)      # throttle -> never uses cookies
    assert not ig.is_restricted_error(OK)
    assert not ig.is_restricted_error({"status": "failed", "error_category": "network_error",
                                       "error_detail": "timed out"})


def test_instaloader_login_errors_use_cookies():
    for detail in ("instaloader: Login required.",
                   "instaloader: profile x requires login",
                   "instaloader: Redirected to login page. Use --login or --load-cookies."):
        r = {"status": "failed", "error_category": "instaloader_error", "error_detail": detail}
        assert ig.is_restricted_error(r), detail

    calls = []
    err = {"status": "failed", "error_category": "instaloader_error",
           "error_detail": "instaloader: Login required."}
    with mock.patch.object(ig, "_download_url_once", _fake_once(err, calls)):
        res = ig.download_url("u", "out", "cookies.txt", False, cookies_mode="fallback")
    assert calls == [None, "cookies.txt"] and res["cookie_fallback"]


# ─────────────────────────────────────────────────────────────────────────
# cookie policy
# ─────────────────────────────────────────────────────────────────────────

def test_cookie_policy_fallback_restricted():
    calls = []
    with mock.patch.object(ig, "_download_url_once", _fake_once(RESTRICTED, calls)):
        res = ig.download_url("u", "out", "cookies.txt", False, cookies_mode="fallback")
    assert calls == [None, "cookies.txt"]
    assert res["cookie_fallback"] and res["used_cookies"]


def test_cookie_policy_never_for_parse_error():
    calls = []
    with mock.patch.object(ig, "_download_url_once", _fake_once(PARSE, calls)):
        res = ig.download_url("u", "out", "cookies.txt", False, cookies_mode="fallback")
    assert calls == [None]
    assert not res["used_cookies"]


def test_cookie_policy_always_and_never():
    calls = []
    with mock.patch.object(ig, "_download_url_once", _fake_once(OK, calls)):
        ig.download_url("u", "out", "cookies.txt", False, cookies_mode="always")
    assert calls == ["cookies.txt"]

    calls = []
    with mock.patch.object(ig, "_download_url_once", _fake_once(RESTRICTED, calls)):
        ig.download_url("u", "out", "cookies.txt", False, cookies_mode="never")
    assert calls == [None]


# ─────────────────────────────────────────────────────────────────────────
# shortcode parsing
# ─────────────────────────────────────────────────────────────────────────

def test_shortcode_from_url():
    cases = {
        "https://www.instagram.com/p/ABC123/": "ABC123",
        "https://www.instagram.com/p/ABC123/?img_index=1": "ABC123",
        "https://www.instagram.com/reel/DEF456/": "DEF456",
        "https://www.instagram.com/reels/GHI789/": "GHI789",
        "https://www.instagram.com/tv/JKL012/": "JKL012",
        "https://instagram.com/someuser/p/MNO345/": "MNO345",
        "https://www.instagram.com/share/reel/PQR678/": "PQR678",
        "https://www.instagram.com/stories/highlights/12345/": "unknown",
        "not a url": "unknown",
    }
    for url, want in cases.items():
        assert ig.shortcode_from_url(url) == want, url


# ─────────────────────────────────────────────────────────────────────────
# path validation
# ─────────────────────────────────────────────────────────────────────────

def test_validate_collection():
    assert ig.validate_collection(" my_saves-2026 ") == "my_saves-2026"
    for bad in ("", "..", "a/b", "a\\b", "/abs", "C:\\x", "bad:name", "CON"):
        _expect_raises(ValueError, ig.validate_collection, bad)


def test_validate_filename_template():
    assert ig.validate_filename_template("{author}_{shortcode}") == "{author}_{shortcode}"
    assert ig.validate_filename_template("{index:04d}_{shortcode}") == "{index:04d}_{shortcode}"
    assert ig.validate_filename_template("") == "{shortcode}"
    for bad in ("{author}/{shortcode}", "..\\evil", "/etc/x", "a..b"):
        _expect_raises(ValueError, ig.validate_filename_template, bad)


def test_resolve_output_dir_containment():
    base = os.path.join(tempfile.gettempdir(), "igdl_base")
    out = ig.resolve_output_dir(base, "coll")
    assert out == os.path.abspath(os.path.join(base, "coll"))
    _expect_raises(ValueError, ig.resolve_output_dir, base, "..")


# ─────────────────────────────────────────────────────────────────────────
# log loading / saving
# ─────────────────────────────────────────────────────────────────────────

def test_load_log_valid_and_dedup():
    d = tempfile.mkdtemp(prefix="igdl_log_")
    try:
        p = os.path.join(d, "x.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"items": [
                {"url": "a", "status": "failed"},
                {"url": "a", "status": "ok"},
                {"url": "b", "status": "ok"},
            ]}, f)
        log = ig.load_log(p)
        assert len(log["items"]) == 2
        a = next(i for i in log["items"] if i["url"] == "a")
        assert a["status"] == "ok"          # latest wins
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_load_log_missing_is_empty():
    p = os.path.join(tempfile.gettempdir(), "igdl_missing_%d.json" % os.getpid())
    assert not os.path.exists(p)
    assert ig.load_log(p) == {"items": [], "summary": {}}


def test_load_log_corrupt_raises():
    d = tempfile.mkdtemp(prefix="igdl_corrupt_")
    try:
        p = os.path.join(d, "x.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        _expect_raises(ig.LogCorruptedError, ig.load_log, p)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_load_log_or_backup_preserves_corrupt_file():
    d = tempfile.mkdtemp(prefix="igdl_backup_")
    try:
        p = os.path.join(d, "x.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("<<<broken>>>")
        log = ig.load_log_or_backup(p)
        assert log == {"items": [], "summary": {}}
        backups = [f for f in os.listdir(d) if f.startswith("x.json.corrupt-")]
        assert backups, "corrupt log should have been moved aside, not overwritten"
        assert not os.path.exists(p)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_save_log_is_atomic_and_readable():
    d = tempfile.mkdtemp(prefix="igdl_save_")
    try:
        p = os.path.join(d, "sub", "x.json")
        ig.save_log({"items": [{"url": "a"}], "summary": {"ok": 1}}, p)
        assert ig.load_log(p)["summary"] == {"ok": 1}
        leftovers = [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]
        assert not leftovers, "temp file should have been replaced"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# end-to-end run_download (download monkeypatched, no network)
# ─────────────────────────────────────────────────────────────────────────

def test_run_download_uses_output_base_and_atomic_log():
    d = tempfile.mkdtemp(prefix="igdl_run_")
    try:
        urlf = os.path.join(d, "urls.txt")
        with open(urlf, "w", encoding="utf-8") as f:
            f.write("https://www.instagram.com/p/AAA/\n")
        seen = {}

        def fake_dl(url, output_dir, cookies_file, dry_run, filename_template="{shortcode}",
                    file_index=0, cookies_mode="fallback"):
            seen["dir"] = output_dir
            return {"url": url, "shortcode": "AAA", "status": "ok",
                    "used_cookies": True, "cookie_fallback": True, "author": "x"}

        with mock.patch.object(ig, "download_url", fake_dl):
            summary = ig.run_download(
                urlf, "coll", log_file=os.path.join(d, "coll.json"),
                cookies="c.txt", output_base=os.path.join(d, "myout"))
        assert seen["dir"] == os.path.join(os.path.abspath(os.path.join(d, "myout")), "coll")
        assert os.path.isdir(seen["dir"])
        assert summary.get("cookie_fallbacks") == 1
        assert ig.load_log(os.path.join(d, "coll.json"))["items"][0]["status"] == "ok"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_default_log_is_written_inside_collection_folder():
    d = tempfile.mkdtemp(prefix="igdl_logdir_")
    try:
        urlf = os.path.join(d, "urls.txt")
        with open(urlf, "w", encoding="utf-8") as f:
            f.write("https://www.instagram.com/p/AAA/\n")

        def fake_dl(url, output_dir, cookies_file, dry_run, filename_template="{shortcode}",
                    file_index=0, cookies_mode="fallback"):
            return {"url": url, "shortcode": "AAA", "status": "ok", "author": "x"}

        out = os.path.join(d, "TestDL")
        with mock.patch.object(ig, "download_url", fake_dl):
            ig.run_download(urlf, "exercise", output_base=out)
        log_path = os.path.join(out, "exercise", "exercise.json")
        assert os.path.isfile(log_path)
        assert ig.load_log(log_path)["items"][0]["status"] == "ok"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_discover_collections_groups_logs_by_folder():
    d = tempfile.mkdtemp(prefix="igdl_disc_")
    try:
        hair = os.path.join(d, "TestDL", "Haircut")
        empty = os.path.join(d, "TestDL", "empty")
        os.makedirs(hair)
        os.makedirs(empty)
        ig.save_log({"items": [{"url": "https://example.test/a"}], "summary": {}},
                    os.path.join(hair, "Haircut.json"))
        with open(os.path.join(empty, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("no log")
        found = ig.discover_collections([d])
        assert [c["name"] for c in found] == ["Haircut"]
        assert found[0]["abs_logs"][0].endswith(os.path.join("Haircut", "Haircut.json"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_path_is_under_rejects_escape():
    d = tempfile.mkdtemp(prefix="igdl_under_")
    try:
        inside = os.path.join(d, "clip.mp4")
        with open(inside, "w", encoding="utf-8") as f:
            f.write("x")
        outside = os.path.abspath(os.path.join(d, "..", "not-here.mp4"))
        assert ig.path_is_under(inside, [d])
        assert not ig.path_is_under(outside, [d])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_loose_carousel_files_move_into_post_folder():
    d = tempfile.mkdtemp(prefix="igdl_carousel_")
    try:
        col = os.path.join(d, "Haircut")
        os.makedirs(col)
        for name in ("DeVtrD4jYsw_1.jpg", "DeVtrD4jYsw_2.jpg", "DeVtrD4jYsw_10.mp4"):
            with open(os.path.join(col, name), "w", encoding="utf-8") as f:
                f.write("x")
        with open(os.path.join(col, "Video by someone (Db8Zd-2ERAa).mp4"), "w", encoding="utf-8") as f:
            f.write("reel")
        ig.save_log({
            "items": [
                {"url": "https://www.instagram.com/p/DeVtrD4jYsw/", "shortcode": "DeVtrD4jYsw",
                 "media_type": "image", "status": "ok",
                 "saved_path": os.path.join(col, "DeVtrD4jYsw_1.jpg")},
                {"url": "https://www.instagram.com/reel/Db8Zd-2ERAa/", "shortcode": "Db8Zd-2ERAa",
                 "media_type": "video", "status": "ok"},
            ],
            "summary": {},
        }, os.path.join(col, "Haircut.json"))
        moved = ig.regroup_collection_carousels(col)
        assert list(moved) == ["DeVtrD4jYsw"]
        folder = os.path.join(col, "DeVtrD4jYsw")
        assert os.path.isdir(folder)
        names = sorted(os.listdir(folder))
        assert names == ["DeVtrD4jYsw_1.jpg", "DeVtrD4jYsw_10.mp4", "DeVtrD4jYsw_2.jpg"]
        assert os.path.isfile(os.path.join(col, "Video by someone (Db8Zd-2ERAa).mp4"))
        item = ig.load_log(os.path.join(col, "Haircut.json"))["items"][0]
        assert item["media_type"] == "carousel"
        assert item["carousel_count"] == 3
        assert os.path.basename(item["saved_files"][0]).endswith("_1.jpg")
        assert os.path.basename(item["saved_files"][1]).endswith("_2.jpg")
        assert item["saved_path"] == folder
        video = ig.load_log(os.path.join(col, "Haircut.json"))["items"][1]
        assert video["media_type"] == "video"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_video_poster_path_stays_out_of_the_media_folder():
    video = os.path.join("TestDL", "exercise", "clip.mp4")
    poster = ig.video_poster_path(video)
    assert os.path.basename(os.path.dirname(poster)) == ".posters"
    assert poster.endswith(os.path.join(".posters", "clip.mp4.jpg"))


def test_dashboard_ui_root_prefers_the_react_build():
    d = tempfile.mkdtemp(prefix="igdl_ui_")
    try:
        assert ig.dashboard_ui_root(d) == d
        dist = os.path.join(d, "dashboard", "dist")
        os.makedirs(dist)
        with open(os.path.join(dist, "index.html"), "w", encoding="utf-8") as f:
            f.write("<!doctype html>")
        assert ig.dashboard_ui_root(d) == dist
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_settings_roundtrip_keeps_known_fields():
    d = tempfile.mkdtemp(prefix="igdl_set_")
    path = os.path.join(d, "settings.json")
    try:
        with mock.patch.object(ig, "dashboard_settings_path", return_value=path), \
             mock.patch.object(ig, "remember_library_roots", return_value=[]):
            saved = ig.save_dashboard_settings({
                "output_dir": os.path.join(d, "media"),
                "cookies_mode": "always",
                "filename_template": "{author}_{shortcode}",
                "nope": "secret",
            })
            assert saved["output_dir"] == os.path.abspath(os.path.join(d, "media"))
            assert saved["cookies_mode"] == "always"
            assert "nope" not in saved
            assert ig.load_dashboard_settings()["filename_template"] == "{author}_{shortcode}"
            again = ig.save_dashboard_settings({
                "cookies_mode": "sometimes",
                "filename_template": "../x",
            })
            assert again["cookies_mode"] == "always"
            assert again["filename_template"] == "{author}_{shortcode}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dashboard_host_stays_local_until_asked():
    with mock.patch.dict(os.environ, {"IGRIP_HOST": ""}, clear=False):
        assert ig.dashboard_listen_host(None) == "127.0.0.1"
    with mock.patch.dict(os.environ, {"IGRIP_HOST": "0.0.0.0"}):
        assert ig.dashboard_listen_host(None) == "0.0.0.0"
    assert ig.dashboard_listen_host("192.168.1.20") == "192.168.1.20"
    url = ig.dashboard_page_url("0.0.0.0", 8765, "/")
    assert url.startswith("http://")
    assert url.endswith(":8765/")
    assert "0.0.0.0" not in url


def test_data_dir_reads_a_dataset_outside_the_user_home():
    d = tempfile.mkdtemp(prefix="igdl_data_")
    home = os.path.join(d, "home")
    dataset = os.path.join(d, "dataset")
    folder = os.path.join(dataset, "Haircut")
    os.makedirs(home)
    os.makedirs(folder)
    with open(os.path.join(folder, "Haircut.json"), "w", encoding="utf-8") as fh:
        json.dump({"items": [{"url": "https://www.instagram.com/p/aaa/", "status": "ok"}]}, fh)
    url_file = os.path.join(dataset, "posts.txt")
    with open(url_file, "w", encoding="utf-8") as fh:
        fh.write("https://www.instagram.com/p/aaa/\n")
    elsewhere = os.path.join(d, "elsewhere")
    os.makedirs(elsewhere)
    try:
        ig.set_data_dir(dataset)
        with mock.patch.object(ig, "browse_home", return_value=os.path.realpath(home)):
            found = ig.discover_collections(ig.library_roots())
            assert any(
                os.path.realpath(item["dir"]).startswith(os.path.realpath(dataset))
                for item in found
            )
            listing = ig.browse_directory(dataset)
            assert [item["name"] for item in listing["files"]] == ["posts.txt"]
            prepared = ig.prepare_download_request({
                "url_file": url_file,
                "collection": "Haircut",
                "output_dir": os.path.join(dataset, "library"),
            })
            assert os.path.basename(prepared["output_dir"]) == "library"
            assert ig._resolve_user_path("library").startswith(os.path.realpath(dataset))
            assert ig.dashboard_settings_path().startswith(os.path.realpath(dataset))
            _expect_raises(ValueError, ig.browse_directory, elsewhere)
            _expect_raises(ValueError, ig.prepare_download_request, {
                "url_file": url_file,
                "collection": "Haircut",
                "output_dir": elsewhere,
            })
    finally:
        ig.set_data_dir(None)
        shutil.rmtree(d, ignore_errors=True)


def test_browse_lists_txt_and_rejects_outside_home():
    d = tempfile.mkdtemp(prefix="igdl_browse_")
    home = os.path.join(d, "home")
    outside = os.path.join(d, "outside")
    os.makedirs(os.path.join(home, "sub"))
    os.makedirs(outside)
    with open(os.path.join(home, "list.txt"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    with open(os.path.join(home, "notes.md"), "w", encoding="utf-8") as fh:
        fh.write("no\n")
    try:
        with mock.patch.object(ig, "browse_home", return_value=os.path.realpath(home)):
            listing = ig.browse_directory(home)
            assert [item["name"] for item in listing["files"]] == ["list.txt"]
            assert any(item["name"] == "sub" for item in listing["dirs"])
            _expect_raises(ValueError, ig.browse_directory, outside)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_prepare_download_rejects_a_missing_url_file():
    d = tempfile.mkdtemp(prefix="igdl_prep_")
    try:
        with mock.patch.object(ig, "browse_home", return_value=os.path.realpath(d)):
            _expect_raises(ValueError, ig.prepare_download_request, {
                "url_file": os.path.join(d, "missing.txt"),
                "collection": "Haircut",
                "output_dir": d,
            })
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_download_job_runs_once():
    import time
    d = tempfile.mkdtemp(prefix="igdl_job_")
    urlf = os.path.join(d, "urls.txt")
    with open(urlf, "w", encoding="utf-8") as fh:
        fh.write("https://www.instagram.com/p/AAA/\n")
    started = threading.Event()
    release = threading.Event()
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs["collection"])
        kwargs["progress_cb"]({"type": "start", "total": 1, "to_process": 1})
        started.set()
        assert release.wait(2)
        kwargs["progress_cb"]({"type": "done", "summary": {"ok": 0, "failed": 0}})
        return {}

    try:
        with mock.patch.object(ig, "browse_home", return_value=os.path.realpath(d)), \
             mock.patch.object(ig, "save_dashboard_settings", side_effect=lambda data: data), \
             mock.patch.object(ig, "run_download", side_effect=fake_run):
            job = ig.DownloadJob()
            params = {
                "url_file": urlf,
                "collection": "Haircut",
                "output_dir": d,
                "cookies": "",
                "cookies_mode": "fallback",
            }
            job.start(params)
            assert started.wait(2)
            _expect_raises(ig.DownloadBusy, job.start, dict(params))
            release.set()
            for _ in range(50):
                if not job.status()["running"]:
                    break
                time.sleep(0.02)
            status = job.status()
            assert status["running"] is False
            assert seen == ["Haircut"]
            assert [event["type"] for event in status["events"]] == ["start", "done"]
    finally:
        release.set()
        shutil.rmtree(d, ignore_errors=True)


_SAVED_PAGE = """
<a href="/jane.doe/" aria-label="Profile">Profile</a>
<a href="/someone.else/">Other</a>
<a href="/jane.doe/saved/all-posts/">All posts</a>
<a href="/jane.doe/saved/haircut/998877/">Haircut 9</a>
<a href="/other/saved/nope/1/">Nope</a>
<a href="/p/AbCdEfGhIj/">post</a>
<a href="https://www.instagram.com/reel/ZzYyXxWwVv/">reel</a>
<script type="application/json">{"x":"/p/AbCdEfGhIj/"}</script>
"""


def test_saved_page_parser_reads_profile_collections_and_posts():
    assert ig.parse_profile_username(_SAVED_PAGE) == "jane.doe"
    collections = ig.parse_saved_collections(_SAVED_PAGE, "jane.doe")
    assert [item["name"] for item in collections] == ["All posts", "Haircut"]
    assert collections[1]["count"] == 9
    assert collections[1]["url"].endswith("/jane.doe/saved/haircut/998877/")
    urls = ig.parse_post_urls(_SAVED_PAGE)
    assert urls == [
        "https://www.instagram.com/p/AbCdEfGhIj/",
        "https://www.instagram.com/reel/ZzYyXxWwVv/",
    ]


def test_gather_post_urls_keeps_posts_that_scroll_away():
    class Page:
        def __init__(self):
            self.url = "https://www.instagram.com/jane.doe/saved/haircut/1/"
            self.calls = 0
            self.batches = [
                [["reel", "aaaaa"], ["p", "bbbbb"]],
                [["p", "ccccc"]],
                [["p", "ccccc"]],
                [["p", "ccccc"]],
                [["p", "ccccc"]],
            ]

        def evaluate(self, script):
            batch = self.batches[min(self.calls, len(self.batches) - 1)]
            self.calls += 1
            return batch

        def wait_for_timeout(self, _ms):
            return None

    seen = []
    urls = ig._gather_post_urls(Page(), on_count=seen.append)
    assert urls == [
        "https://www.instagram.com/reel/aaaaa/",
        "https://www.instagram.com/p/bbbbb/",
        "https://www.instagram.com/p/ccccc/",
    ]
    assert seen == [2, 3]


def test_saved_collection_counts_compare_with_the_library():
    d = tempfile.mkdtemp(prefix="igdl_counts_")
    folder = os.path.join(d, "Haircut")
    os.makedirs(folder)
    with open(os.path.join(folder, "Haircut.json"), "w", encoding="utf-8") as fh:
        json.dump({"items": [
            {"url": "https://www.instagram.com/p/aaa/", "status": "ok"},
            {"url": "https://www.instagram.com/p/bbb/", "status": "ok"},
            {"url": "https://www.instagram.com/p/ccc/", "status": "failed"},
        ]}, fh)
    try:
        collections = [
            {"name": "haircut", "count": 9},
            {"name": "Empty shelf", "count": 2},
        ]
        ig.annotate_collection_counts(collections, [d])
        assert collections[0]["saved"] == 2
        assert collections[0]["failed"] == 1
        assert collections[1]["saved"] == 0
        assert collections[1]["failed"] == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_saved_collection_url_and_folder_name():
    assert ig.is_saved_collection_url("https://www.instagram.com/jane.doe/saved/haircut/1/")
    assert not ig.is_saved_collection_url("https://example.com/jane.doe/saved/haircut/")
    assert ig.collection_folder_name("bad:name") == "bad name"
    assert ig.looks_like_login("https://www.instagram.com/accounts/login/", "")


def test_write_collection_url_file_and_reject_foreign_download():
    d = tempfile.mkdtemp(prefix="igdl_saved_")
    cookies = os.path.join(d, "cookies.txt")
    with open(cookies, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
    try:
        with mock.patch.object(ig, "browse_home", return_value=os.path.realpath(d)), \
             mock.patch.object(ig, "load_dashboard_settings", return_value={
                 "cookies": cookies, "output_dir": d, "filename_template": "{shortcode}",
             }):
            _expect_raises(ValueError, ig.prepare_saved_download, {
                "name": "Haircut",
                "url": "https://example.com/a/saved/x/",
            })
        with mock.patch.object(ig, "__file__", os.path.join(d, "ig_download.py")):
            path = ig.write_collection_url_file("Haircut", [
                "https://www.instagram.com/p/AbCdEfGhIj/",
            ])
        assert os.path.dirname(os.path.dirname(path)) == os.path.realpath(d)
        assert os.path.basename(path) == "Haircut.txt"
        with open(path, encoding="utf-8") as fh:
            assert "AbCdEfGhIj" in fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_saved_job_writes_urls_then_downloads():
    import time
    d = tempfile.mkdtemp(prefix="igdl_savedjob_")
    cookies = os.path.join(d, "cookies.txt")
    with open(cookies, "w", encoding="utf-8") as fh:
        fh.write("cookies\n")
    url_file = os.path.join(d, "out.txt")
    seen = {}

    def fake_collect(cookies_file, collection_url, stop_event=None, progress_cb=None):
        seen["url"] = collection_url
        if progress_cb:
            progress_cb({"type": "collecting", "found": 1, "message": "Reading the collection… 1 posts"})
        return ["https://www.instagram.com/p/AbCdEfGhIj/"]

    def fake_write(name, urls, directory=None):
        seen["name"] = name
        seen["urls"] = urls
        seen["directory"] = directory
        return url_file

    def fake_run(**kwargs):
        seen["download"] = kwargs["url_file"]
        seen["retry_failed"] = kwargs["retry_failed"]
        kwargs["progress_cb"]({"type": "done", "summary": {"ok": 1, "failed": 0}})
        return {}

    try:
        with mock.patch.object(ig, "prepare_saved_download", return_value={
                 "cookies": cookies,
                 "collection": "Haircut",
                 "url": "https://www.instagram.com/jane.doe/saved/haircut/1/",
                 "output_dir": d,
                 "filename_template": "{shortcode}",
             }), \
             mock.patch.object(ig, "collect_collection_posts", side_effect=fake_collect), \
             mock.patch.object(ig, "write_collection_url_file", side_effect=fake_write), \
             mock.patch.object(ig, "save_dashboard_settings", side_effect=lambda data: data), \
             mock.patch.object(ig, "run_download", side_effect=fake_run):
            job = ig.DownloadJob()
            job.start_saved({"name": "Haircut", "url": "https://www.instagram.com/jane.doe/saved/haircut/1/"})
            for _ in range(50):
                if not job.status()["running"]:
                    break
                time.sleep(0.02)
        status = job.status()
        assert status["running"] is False
        assert seen["name"] == "Haircut"
        assert seen["download"] == url_file
        assert seen["retry_failed"] is True
        assert status["events"][-1]["type"] == "done"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_carousel_extract_keeps_photo_slides():
    playlist = {"_type": "playlist", "id": "abc", "entries": [
        {"formats": [], "thumbnails": [{"url": "https://cdn.example/a.jpg"}]},
    ]}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def extract_info(self, url, download=False, process=True):
            assert process is False
            return playlist
        def process_ie_result(self, info, download=False):
            raise AssertionError("photo slides must stay in the carousel")

    with mock.patch.object(ig.yt_dlp, "YoutubeDL", FakeYDL):
        info = ig.yt_extract_info("https://www.instagram.com/p/abc/", None)
    assert info["entries"][0]["thumbnails"][0]["url"].endswith("/a.jpg")


def test_video_extract_is_still_resolved():
    raw = {"id": "abc", "formats": [{"url": "https://cdn.example/v.mp4"}]}
    resolved = {**raw, "ext": "mp4"}

    class FakeYDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def extract_info(self, url, download=False, process=True):
            return raw
        def process_ie_result(self, info, download=False):
            assert info is raw
            return resolved

    with mock.patch.object(ig.yt_dlp, "YoutubeDL", FakeYDL):
        info = ig.yt_extract_info("https://www.instagram.com/reel/abc/", None)
    assert info["ext"] == "mp4"


def test_instaloader_cookies_stay_logged_out():
    """A sessionid must not flip instaloader onto graphql/query."""
    d = tempfile.mkdtemp(prefix="igdl_ck_")
    path = os.path.join(d, "cookies.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        fh.write(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n")
        fh.write(".instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t42\n")
    import requests
    loader = mock.Mock()
    loader.context.username = None
    loader.context._session.cookies = requests.cookies.RequestsCookieJar()
    try:
        assert ig._apply_instaloader_cookies(loader, path) is True
        assert not loader.context.username
        assert "sessionid" in {cookie.name for cookie in loader.context._session.cookies}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_save_carousel_images_writes_photo_slides_only():
    d = tempfile.mkdtemp(prefix="igdl_slides_")
    info = {"entries": [
        {"formats": [{"url": "https://cdn.example/v.mp4"}], "thumbnails": [{"url": "https://cdn.example/skip.jpg", "width": 10, "height": 10}]},
        {"formats": [], "thumbnails": [
            {"url": "https://cdn.example/small.jpg", "width": 100, "height": 100},
            {"url": "https://cdn.example/large.jpg", "width": 800, "height": 800},
        ]},
    ]}

    class _Response:
        def read(self):
            return b"jpeg"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    try:
        with mock.patch.object(ig.urllib.request, "urlopen", return_value=_Response()) as opened:
            saved = ig.save_carousel_images(info, d)
        assert [os.path.basename(path) for path in saved] == ["02.jpg"]
        assert opened.call_args.args[0].full_url == "https://cdn.example/large.jpg"
        assert os.path.getsize(os.path.join(d, "02.jpg")) > 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _netscape_cookie(path, user_id):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        fh.write(f".instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t{user_id}\n")


def test_saved_download_uses_the_account_folder():
    d = tempfile.mkdtemp(prefix="igdl_acct_")
    cookies = os.path.join(d, "cookies.txt")
    _netscape_cookie(cookies, "111")
    try:
        with mock.patch.object(ig, "browse_home", return_value=os.path.realpath(d)), \
             mock.patch.object(ig, "load_dashboard_settings", return_value={
                 "cookies": cookies, "output_dir": d, "filename_template": "{shortcode}",
             }):
            prepared = ig.prepare_saved_download({
                "name": "Haircut",
                "url": "https://www.instagram.com/jane.doe/saved/haircut/1/",
            })
        assert os.path.basename(prepared["output_dir"]) == "jane.doe"
        assert os.path.dirname(os.path.realpath(prepared["output_dir"])) == os.path.realpath(d)
        path = ig.write_collection_url_file(
            "Haircut", ["https://www.instagram.com/p/AbCdEfGhIj/"], directory=prepared["output_dir"]
        )
        assert os.path.basename(os.path.dirname(path)) == "jane.doe"
        assert os.path.basename(path) == "Haircut.txt"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_saved_cache_switches_with_the_cookie_account():
    d = tempfile.mkdtemp(prefix="igdl_cache_")
    one = os.path.join(d, "one.txt")
    two = os.path.join(d, "two.txt")
    _netscape_cookie(one, "111")
    _netscape_cookie(two, "222")
    folder = os.path.join(d, "jane.doe", "Haircut")
    os.makedirs(folder)
    with open(os.path.join(folder, "Haircut.json"), "w", encoding="utf-8") as fh:
        json.dump({"items": [{"url": "https://www.instagram.com/p/aaa/", "status": "ok"}]}, fh)
    loose = os.path.join(d, "Haircut")
    os.makedirs(loose)
    with open(os.path.join(loose, "Haircut.json"), "w", encoding="utf-8") as fh:
        json.dump({"items": [
            {"url": "https://www.instagram.com/p/bbb/", "status": "ok"},
            {"url": "https://www.instagram.com/p/ccc/", "status": "ok"},
        ]}, fh)
    ig.set_data_dir(d)
    try:
        ig.store_saved_collections("111", "jane.doe", [{
            "name": "Haircut",
            "url": "https://www.instagram.com/jane.doe/saved/haircut/1/",
            "count": 10,
        }])
        ig.store_saved_collections("222", "other.user", [{
            "name": "Nails",
            "url": "https://www.instagram.com/other.user/saved/nails/2/",
            "count": 4,
        }])
        settings = {"output_dir": d, "filename_template": "{shortcode}"}
        with mock.patch.object(ig, "load_dashboard_settings", return_value={**settings, "cookies": one}):
            first = ig.saved_collections_view()
        with mock.patch.object(ig, "load_dashboard_settings", return_value={**settings, "cookies": two}):
            second = ig.saved_collections_view()
        assert first["username"] == "jane.doe"
        assert first["collections"][0]["count"] == 10
        assert first["collections"][0]["saved"] == 1
        assert second["username"] == "other.user"
        assert second["collections"][0]["name"] == "Nails"
        assert second["collections"][0]["saved"] == 0
        urlf = os.path.join(d, "urls.txt")
        with open(urlf, "w", encoding="utf-8") as fh:
            fh.write("https://www.instagram.com/p/AAA/\n")
        with mock.patch.object(ig, "save_dashboard_settings", side_effect=lambda data: data):
            nested = ig.prepare_download_request({
                "url_file": urlf, "collection": "Haircut", "output_dir": d,
                "cookies": one, "cookies_mode": "fallback",
            })
            plain = ig.prepare_download_request({
                "url_file": urlf, "collection": "Haircut", "output_dir": d,
                "cookies": "", "cookies_mode": "fallback",
            })
        assert os.path.basename(nested["output_dir"]) == "jane.doe"
        assert os.path.realpath(plain["output_dir"]) == os.path.realpath(d)
    finally:
        ig.set_data_dir(None)
        shutil.rmtree(d, ignore_errors=True)


def test_tracking_runs_every_five_days_and_keeps_its_flag():
    now = ig.datetime(2026, 11, 6, tzinfo=ig.timezone.utc)
    assert ig.collection_is_due({"tracked": True}, now) is True
    assert ig.collection_is_due({"tracked": False, "checked_at": ""}, now) is False
    recent = (now - ig.timedelta(days=4)).isoformat()
    due = (now - ig.timedelta(days=5)).isoformat()
    assert ig.collection_is_due({"tracked": True, "checked_at": recent}, now) is False
    assert ig.collection_is_due({"tracked": True, "checked_at": due}, now) is True

    d = tempfile.mkdtemp(prefix="igdl_track_")
    cookies = os.path.join(d, "cookies.txt")
    _netscape_cookie(cookies, "111")
    haircut = "https://www.instagram.com/jane.doe/saved/haircut/1/"
    nails = "https://www.instagram.com/jane.doe/saved/nails/2/"
    ig.set_data_dir(d)
    try:
        ig.save_dashboard_settings({
            "cookies": cookies, "output_dir": d, "filename_template": "{shortcode}",
        })
        ig.store_saved_collections("111", "jane.doe", [
            {"name": "Haircut", "url": haircut, "count": 10},
            {"name": "Nails", "url": nails, "count": 4},
        ])
        one = ig.update_saved_tracking({"url": haircut, "tracked": True})
        assert one["collections"][0]["tracked"] is True
        assert one["collections"][1]["tracked"] is False
        ig.store_saved_collections("111", "jane.doe", [
            {"name": "Haircut", "url": haircut, "count": 11},
            {"name": "Nails", "url": nails, "count": 4},
        ])
        kept = ig.saved_collections_view()
        assert kept["collections"][0]["tracked"] is True
        assert kept["collections"][0]["count"] == 11
        both = ig.update_saved_tracking({"all": True})
        assert [item["tracked"] for item in both["collections"]] == [True, True]

        class FakeJob:
            def __init__(self):
                self.calls = []
                self.running = False

            def status(self, since=0):
                return {"running": self.running}

            def start_saved(self, params):
                assert params["remember_url_file"] is False
                self.calls.append(params["url"])

        job = FakeJob()
        started = ig._run_due_tracks(job, now=now)
        assert started == [haircut, nails]
        assert ig._run_due_tracks(job, now=now) == []
        job.running = True
        cache = ig.load_saved_cache()
        cache["accounts"]["111"]["collections"][0]["checked_at"] = due
        ig.save_saved_cache(cache)
        assert ig._run_due_tracks(job, now=now) == []
        assert job.calls == [haircut, nails]
    finally:
        ig.set_data_dir(None)
        shutil.rmtree(d, ignore_errors=True)


def test_discovered_log_reports_its_parent_as_the_account():
    d = tempfile.mkdtemp(prefix="igdl_disc_")
    nested = os.path.join(d, "jane.doe", "Haircut")
    loose = os.path.join(d, "Loose")
    os.makedirs(nested)
    os.makedirs(loose)
    for folder, name in ((nested, "Haircut"), (loose, "Loose")):
        with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8") as fh:
            json.dump({"items": []}, fh)
    try:
        found = {item["name"]: item.get("account") for item in ig.discover_collections([d])}
        assert found["Haircut"] == "jane.doe"
        assert found["Loose"] == ""
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_run_download_rejects_traversal_collection():
    d = tempfile.mkdtemp(prefix="igdl_trav_")
    try:
        urlf = os.path.join(d, "urls.txt")
        with open(urlf, "w", encoding="utf-8") as f:
            f.write("https://www.instagram.com/p/AAA/\n")
        _expect_raises(ValueError, ig.run_download, urlf, "../escape")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# standalone runner (works without pytest)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", name, "->", repr(e))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
