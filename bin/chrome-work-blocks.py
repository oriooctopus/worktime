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
import argparse, json, os, sys
from datetime import datetime, timedelta, date as date_cls

# realpath, not abspath: this file may be reached through a symlink on PATH,
# and abspath would look for the shared module beside the symlink.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

# The timezone, the 1601 epoch, and the History location used to be spelled out
# again here, and the copy differed: it hardcoded the "Default" profile, which
# does not exist on the Mac, so this script read an empty history there and the
# result was indistinguishable from a day of no browsing.
TZ = wc.local_tz()
CHROME_HISTORY = wc.chrome_history_path()
CONFIG_PATH = os.path.expanduser("~/.config/activity-export/work-domains.json")
CACHE_PATH = os.path.expanduser("~/.cache/activity-export/chrome-work-blocks.json")

# A visit with no recorded dwell (the page you are still on, or a redirect hop)
# still means you were there; give it a floor rather than zero width.
MIN_DWELL_SEC = 60
# A parked tab accrues dwell for as long as it sits there -- one tab today held
# 214 minutes. visit_duration cannot distinguish reading from parking, so a
# single visit is capped: past this, it is evidence of an open tab, not work.
MAX_DWELL_SEC = 10 * 60
# Two browsing stretches closer together than this are one block. Deliberately
# looser than the probe's own prompt-bout gap, which is 5 minutes and ramps when
# the screen is unfocused: a prompt is a deliberate act, so silence after one is
# real, whereas reading a long PR page emits nothing for minutes at a time.
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
    # The work keyword and the Google work account live in the profile beside
    # the timezone -- they are facts about the person, not about this
    # exporter, and the probe classifies the same visits from the same two
    # answers. Resolved once here so classify() never reads a file.
    profile = wc.load_profile()
    cfg.setdefault("work_url_keywords", wc.work_url_keywords(profile))
    cfg.setdefault("google_work_account", wc.google_work_account(profile))
    # Normalised to a set of ints here so a list in the JSON config and the
    # profile default reach classify() as the same thing.
    cfg["work_localhost_ports"] = {int(p) for p in cfg.get(
        "work_localhost_ports", wc.work_localhost_ports(profile))}
    return cfg


def classify(url, cfg):
    """'ignore' wins over 'work': a longer, more specific ignore rule is how you
    carve an exception out of a broad work domain.

    Beyond the listed sites, three rules come from the profile: a URL naming a
    work keyword, a dev server on one of the work app's localhost ports, and a
    Google page signed in as the work account. They are
    here rather than expanded into cfg["work"] because neither is a substring
    -- the keyword rule ignores the query string so that searching the
    company's name is not work, and the account rule reads an index out of the
    path, which is the only thing separating the work Drive from the personal
    one on the same hostname.
    """
    u = (url or "").lower()
    if any(s.lower() in u for s in cfg["ignore"]):
        return "ignore"
    if any(s.lower() in u for s in cfg["work"]):
        return "work"
    if wc.is_work_url(u, cfg["work_url_keywords"], cfg["google_work_account"],
                      cfg["work_localhost_ports"]):
        return "work"
    return "neutral"


def read_visits(history_path, local_date, tz=TZ):
    """Return [(url, start_dt, dwell_sec)] for local_date, as local times."""
    if not history_path:
        raise WorkBlocksError("no Chrome profile found to read history from")
    start_local = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
    rows = wc.read_history(
        history_path,
        """SELECT urls.url, visits.visit_time, visits.visit_duration
           FROM visits JOIN urls ON urls.id = visits.url
           WHERE visits.visit_time BETWEEN ? AND ?
           ORDER BY visits.visit_time""",
        (wc.chrome_micros(start_local),
         wc.chrome_micros(start_local + timedelta(days=1))))
    return [(url, wc.chrome_time(t, tz), (dur or 0) / 1e6) for url, t, dur in rows]


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
    except (WorkBlocksError, wc.ProfileError) as e:
        print(f"chrome-work-blocks: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
