"""Tests for chrome-work-blocks.py. Run with:
    python3 -m pytest test_chrome_work_blocks.py -v

No Chrome History and no filesystem reads except explicit tmp_path fixtures --
read_visits() is the only DB-touching function and it is exercised against a
hand-built SQLite file, not the real 53MB profile.
"""
import importlib.util, json, os, sqlite3, sys
from datetime import datetime, timedelta, date

import pytest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "chrome-work-blocks.py")
spec = importlib.util.spec_from_file_location("chrome_work_blocks", MODULE_PATH)
cwb = importlib.util.module_from_spec(spec)
sys.modules["chrome_work_blocks"] = cwb
spec.loader.exec_module(cwb)

CFG = {"work": ["github.com/scaledata", "rubrik.com"], "ignore": ["instagram.com"]}
D = date(2026, 8, 31)


def t(h, m):
    return datetime(2026, 8, 31, h, m, tzinfo=cwb.TZ)


# --- classification ---

def test_work_url_classified_work():
    assert cwb.classify("https://github.com/scaledata/sdmain/pull/1", CFG) == "work"


def test_ignored_url_classified_ignore():
    assert cwb.classify("https://www.instagram.com/explore/", CFG) == "ignore"


def test_unknown_url_is_neutral():
    assert cwb.classify("https://news.ycombinator.com/", CFG) == "neutral"


def test_classify_is_case_insensitive():
    assert cwb.classify("HTTPS://GitHub.COM/scaledata/x", CFG) == "work"


def test_ignore_beats_work_so_exceptions_can_be_carved_out():
    cfg = {"work": ["rubrik.com"], "ignore": ["rubrik.com/cafeteria"]}
    assert cwb.classify("https://rubrik.com/cafeteria/menu", cfg) == "ignore"
    assert cwb.classify("https://rubrik.com/eng", cfg) == "work"


def test_missing_url_is_neutral_not_a_crash():
    assert cwb.classify(None, CFG) == "neutral"


# --- intervals: the parked-tab cap is the whole point ---

def test_zero_dwell_visit_gets_the_minimum_floor():
    iv = cwb.work_intervals([("https://github.com/scaledata/a", t(10, 0), 0.0)], CFG)
    assert (iv[0][1] - iv[0][0]).total_seconds() == cwb.MIN_DWELL_SEC


def test_parked_tab_dwell_is_capped():
    """A tab held 214 minutes is an open tab, not 214 minutes of work."""
    iv = cwb.work_intervals([("https://rubrik.com/x", t(10, 10), 214 * 60)], CFG)
    assert (iv[0][1] - iv[0][0]).total_seconds() == cwb.MAX_DWELL_SEC


def test_ordinary_dwell_is_kept_verbatim():
    iv = cwb.work_intervals([("https://rubrik.com/x", t(10, 0), 300.0)], CFG)
    assert (iv[0][1] - iv[0][0]).total_seconds() == 300


def test_non_work_visits_produce_no_interval():
    visits = [("https://instagram.com/", t(10, 0), 600.0),
              ("https://news.ycombinator.com/", t(10, 5), 600.0)]
    assert cwb.work_intervals(visits, CFG) == []


# --- merging ---

def test_overlapping_intervals_union_rather_than_sum():
    """Eight redirect hops in two seconds are not eight units of work."""
    merged = cwb.merge_intervals([(t(10, 0), t(10, 10)), (t(10, 2), t(10, 6))])
    assert merged == [(t(10, 0), t(10, 10))]


def test_intervals_within_the_gap_are_bridged():
    merged = cwb.merge_intervals([(t(10, 0), t(10, 5)), (t(10, 10), t(10, 20))], gap_sec=600)
    assert merged == [(t(10, 0), t(10, 20))]


def test_intervals_beyond_the_gap_stay_separate():
    merged = cwb.merge_intervals([(t(10, 0), t(10, 5)), (t(11, 0), t(11, 10))], gap_sec=600)
    assert len(merged) == 2


def test_merge_handles_unsorted_input():
    merged = cwb.merge_intervals([(t(11, 0), t(11, 10)), (t(10, 0), t(10, 5))], gap_sec=60)
    assert merged[0][0] == t(10, 0)


def test_merge_of_nothing_is_nothing():
    assert cwb.merge_intervals([]) == []


# --- blocks ---

def test_a_single_stray_visit_does_not_make_a_block():
    visits = [("https://rubrik.com/x", t(10, 0), 30.0)]
    assert cwb.build_blocks(visits, CFG) == []


def test_sustained_browsing_makes_a_block():
    visits = [("https://github.com/scaledata/p", t(10, m), 120.0) for m in range(0, 20, 2)]
    blocks = cwb.build_blocks(visits, CFG)
    assert len(blocks) == 1 and (blocks[0][1] - blocks[0][0]).total_seconds() >= 180


def test_config_can_override_the_thresholds():
    visits = [("https://rubrik.com/x", t(10, 0), 214 * 60)]
    cfg = dict(CFG, max_dwell_sec=60, min_block_sec=30)
    blocks = cwb.build_blocks(visits, cfg)
    assert (blocks[0][1] - blocks[0][0]).total_seconds() == 60


def test_render_emits_hhmm_and_minutes():
    out = cwb.render([(t(14, 3), t(15, 13))], D, now=t(15, 13))
    assert out["blocks"] == [{"start": "14:03", "end": "15:13", "minutes": 70}]
    assert out["date"] == "2026-08-31"


# --- config loading ---

def test_missing_config_raises(tmp_path):
    with pytest.raises(cwb.WorkBlocksError):
        cwb.load_config(str(tmp_path / "nope.json"))


def test_config_without_work_list_raises(tmp_path):
    p = tmp_path / "c.json"; p.write_text('{"ignore": []}')
    with pytest.raises(cwb.WorkBlocksError):
        cwb.load_config(str(p))


def test_config_with_non_integer_threshold_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"work": [], "ignore": [], "gap_sec": "twelve"}')
    with pytest.raises(cwb.WorkBlocksError):
        cwb.load_config(str(p))


# --- the SQLite read path ---

def _make_history(path, rows):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
    c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, "
              "visit_time INTEGER, visit_duration INTEGER)")
    for i, (u, dt, dur) in enumerate(rows, start=1):
        c.execute("INSERT INTO urls VALUES (?,?)", (i, u))
        micros = int((dt.astimezone(cwb.ZoneInfo("UTC")).replace(tzinfo=None)
                      - cwb.CHROME_EPOCH).total_seconds() * 1e6)
        c.execute("INSERT INTO visits VALUES (?,?,?,?)", (i, i, micros, int(dur * 1e6)))
    c.commit(); c.close()


def test_read_visits_converts_the_1601_epoch(tmp_path):
    h = tmp_path / "History"
    _make_history(h, [("https://github.com/scaledata/x", t(14, 3), 90.0)])
    visits = cwb.read_visits(str(h), D)
    assert len(visits) == 1
    url, start, dwell = visits[0]
    assert start.strftime("%H:%M") == "14:03" and dwell == 90.0


def test_read_visits_excludes_other_days(tmp_path):
    h = tmp_path / "History"
    other = datetime(2026, 8, 30, 14, 0, tzinfo=cwb.TZ)
    _make_history(h, [("https://github.com/scaledata/x", other, 60.0)])
    assert cwb.read_visits(str(h), D) == []


def test_run_writes_the_cache_file(tmp_path):
    h = tmp_path / "History"
    _make_history(h, [("https://github.com/scaledata/p", t(14, m), 120.0)
                      for m in range(0, 20, 2)])
    cfg_p = tmp_path / "cfg.json"; cfg_p.write_text(json.dumps(CFG))
    cache = tmp_path / "out" / "blocks.json"
    cwb.run(local_date=D, history_path=str(h), config_path=str(cfg_p),
            cache_path=str(cache), now=t(15, 0))
    written = json.loads(cache.read_text())
    assert written["date"] == "2026-08-31" and len(written["blocks"]) == 1


def test_config_can_override_min_dwell():
    """A 2-second glance should not buy a full minute of credit."""
    visits = [("https://rubrik.com/x", t(10, 0), 2.0)]
    cfg = dict(CFG, min_dwell_sec=30, min_block_sec=10)
    blocks = cwb.build_blocks(visits, cfg)
    assert (blocks[0][1] - blocks[0][0]).total_seconds() == 30


def test_config_gap_sec_actually_splits_blocks():
    """12 min was the Claude-prompt threshold; browsing breaks run 4-7 min."""
    visits = [("https://rubrik.com/a", t(10, 0), 60.0),
              ("https://rubrik.com/b", t(10, 6), 60.0)]
    wide = cwb.build_blocks(visits, dict(CFG, gap_sec=720, min_block_sec=10))
    tight = cwb.build_blocks(visits, dict(CFG, gap_sec=180, min_block_sec=10))
    assert len(wide) == 1 and len(tight) == 2


# --- chrome history location ---

def test_history_path_uses_the_profile_when_set(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text(json.dumps({"chrome_history_path": "/custom/History"}))
    assert cwb.chrome_history_path(str(f)) == "/custom/History"


def test_history_path_expands_a_tilde(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text(json.dumps({"chrome_history_path": "~/Chrome/History"}))
    assert cwb.chrome_history_path(str(f)).startswith(os.path.expanduser("~"))


def test_history_path_falls_back_to_a_platform_default(tmp_path):
    got = cwb.chrome_history_path(str(tmp_path / "nope.json"))
    assert got in cwb.CHROME_HISTORY_DEFAULTS.values()


def test_macos_default_points_at_the_real_chrome_location():
    assert cwb.CHROME_HISTORY_DEFAULTS["macos"].endswith(
        "Library/Application Support/Google/Chrome/Default/History")


def test_malformed_profile_raises(tmp_path):
    f = tmp_path / "profile.json"; f.write_text("{not json")
    with pytest.raises(cwb.WorkBlocksError):
        cwb.chrome_history_path(str(f))
