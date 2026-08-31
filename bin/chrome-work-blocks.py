#!/usr/bin/env python3
"""Derive work blocks from Chrome history and cache them for calendar-export.

The probe treats a quiet stretch as "not working" unless something covers it.
Browsing is the missing evidence: 19 minutes reviewing a PR generates no Claude
prompts and no calendar event, so it scores as idle. This turns classified
browsing into time blocks the probe already knows how to read.

Visits are NOT counted by row. A redirect chain of eight hops in two seconds is
not eight units of work, and one PR page held for 19 minutes is not one unit --
so each visit becomes an interval and overlapping intervals are unioned.

Writes ~/.cache/activity-export/chrome-work-blocks.json. Reads nothing from the
network. Run it as often as you like; the Chrome History copy is ~0.4s.
"""
import argparse, json, os, shutil, sqlite3, sys, tempfile
from datetime import datetime, timedelta, date as date_cls
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (this box: 3.8.10)
    from backports.zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
CHROME_EPOCH = datetime(1601, 1, 1)
PROFILE_PATH = os.path.expanduser("~/.config/worktime/profile.json")
# Where Chrome keeps History differs per platform. macOS matters because
# total_foreground_duration -- the only signal that separates reading from a
# parked tab -- exists on the machine doing the browsing and is stripped by sync.
CHROME_HISTORY_DEFAULTS = {
    "wsl": "/mnt/c/chrome-cdp-profile/Default/History",
    "macos": os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/History"),
    "linux": os.path.expanduser("~/.config/google-chrome/Default/History"),
}


def chrome_history_path(profile_path=None):
    path = profile_path if profile_path is not None else PROFILE_PATH
    if os.path.exists(path):
        try:
            with open(path) as f:
                configured = json.load(f).get("chrome_history_path")
        except (OSError, ValueError) as e:
            raise WorkBlocksError(f"profile {path} is unreadable: {e}")
        if configured:
            return os.path.expanduser(configured)
    if sys.platform == "darwin":
        return CHROME_HISTORY_DEFAULTS["macos"]
    if "microsoft" in os.uname().release.lower():
        return CHROME_HISTORY_DEFAULTS["wsl"]
    return CHROME_HISTORY_DEFAULTS["linux"]


CHROME_HISTORY = chrome_history_path()
CONFIG_PATH = os.path.expanduser("~/.config/activity-export/work-domains.json")
CACHE_PATH = os.path.expanduser("~/.cache/activity-export/chrome-work-blocks.json")

# A visit with no recorded dwell (the page you are still on, or a redirect hop)
# still means you were there; give it a floor rather than zero width.
MIN_DWELL_SEC = 60
# A parked tab accrues dwell for as long as it sits there -- one tab today held
# 214 minutes. visit_duration cannot distinguish reading from parking, so a
# single visit is capped: past this, it is evidence of an open tab, not work.
MAX_DWELL_SEC = 10 * 60
# Same 12-minute threshold the probe uses for Claude prompt bouts.
GAP_SEC = 12 * 60
# A single stray visit should not manufacture a work block.
MIN_BLOCK_SEC = 3 * 60


class WorkBlocksError(Exception):
    pass


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise WorkBlocksError(f"no work-domain config at {path}")
    with open(path) as f:
        cfg = json.load(f)
    for key in ("work", "ignore"):
        if not isinstance(cfg.get(key), list):
            raise WorkBlocksError(f"config {path} needs a '{key}' list")
    for key in ("max_dwell_sec", "min_dwell_sec", "gap_sec", "min_block_sec"):
        if key in cfg and not isinstance(cfg[key], int):
            raise WorkBlocksError(f"config {path}: '{key}' must be an integer")
    return cfg


def classify(url, cfg):
    """'ignore' wins over 'work': a longer, more specific ignore rule is how you
    carve an exception out of a broad work domain."""
    u = (url or "").lower()
    if any(s.lower() in u for s in cfg["ignore"]):
        return "ignore"
    if any(s.lower() in u for s in cfg["work"]):
        return "work"
    return "neutral"


def chrome_ts(dt):
    return int((dt.replace(tzinfo=None) - CHROME_EPOCH).total_seconds() * 1e6)


def read_visits(history_path, local_date, tz=TZ):
    """Return [(url, start_dt, dwell_sec)] for local_date, as local times."""
    start_local = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
    lo = chrome_ts(start_local.astimezone(ZoneInfo("UTC")))
    hi = chrome_ts((start_local + timedelta(days=1)).astimezone(ZoneInfo("UTC")))
    tmp_dir = tempfile.mkdtemp(prefix="chrome-work-blocks-")
    tmp = os.path.join(tmp_dir, "History")
    try:
        shutil.copy2(history_path, tmp)
        conn = sqlite3.connect(tmp)
        rows = list(conn.execute(
            """SELECT urls.url, visits.visit_time, visits.visit_duration
               FROM visits JOIN urls ON urls.id = visits.url
               WHERE visits.visit_time BETWEEN ? AND ?
               ORDER BY visits.visit_time""", (lo, hi)))
        conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    out = []
    for url, t, dur in rows:
        start_utc = (CHROME_EPOCH + timedelta(microseconds=t)).replace(tzinfo=ZoneInfo("UTC"))
        out.append((url, start_utc.astimezone(tz), (dur or 0) / 1e6))
    return out


def work_intervals(visits, cfg, min_dwell=MIN_DWELL_SEC, max_dwell=MAX_DWELL_SEC):
    return [(s, s + timedelta(seconds=min(max(d, min_dwell), max_dwell)))
            for url, s, d in visits if classify(url, cfg) == "work"]


def merge_intervals(intervals, gap_sec=GAP_SEC):
    """Union overlapping intervals, bridging gaps under gap_sec."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if (start - merged[-1][1]).total_seconds() <= gap_sec:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(m) for m in merged]


def build_blocks(visits, cfg, **kw):
    min_block = cfg.get("min_block_sec", MIN_BLOCK_SEC)
    kw.setdefault("max_dwell", cfg.get("max_dwell_sec", MAX_DWELL_SEC))
    kw.setdefault("min_dwell", cfg.get("min_dwell_sec", MIN_DWELL_SEC))
    blocks = merge_intervals(work_intervals(visits, cfg, **kw),
                             cfg.get("gap_sec", GAP_SEC))
    return [b for b in blocks if (b[1] - b[0]).total_seconds() >= min_block]


def render(blocks, local_date, now=None, tz=TZ):
    now = now or datetime.now(tz)
    return {
        "date": local_date.isoformat(),
        "generated": now.astimezone(tz).isoformat(timespec="seconds"),
        "blocks": [{"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"),
                    "minutes": round((e - s).total_seconds() / 60)}
                   for s, e in blocks],
    }


def run(local_date=None, history_path=CHROME_HISTORY, config_path=CONFIG_PATH,
        cache_path=CACHE_PATH, now=None):
    local_date = local_date or datetime.now(TZ).date()
    cfg = load_config(config_path)
    blocks = build_blocks(read_visits(history_path, local_date), cfg)
    payload = render(blocks, local_date, now=now)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, cache_path)
    return payload


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.add_argument("--dry-run", action="store_true", help="print, do not write cache")
    a = p.parse_args()
    d = date_cls.fromisoformat(a.date) if a.date else None
    try:
        if a.dry_run:
            cfg = load_config()
            blocks = build_blocks(read_visits(CHROME_HISTORY, d or datetime.now(TZ).date()), cfg)
            print(json.dumps(render(blocks, d or datetime.now(TZ).date()), indent=2))
        else:
            print(json.dumps(run(local_date=d), indent=2))
    except WorkBlocksError as e:
        print(f"chrome-work-blocks: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
