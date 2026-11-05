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
