#!/usr/bin/env python3
"""Validate and write the worktime configs from a single JSON answer blob.

Kept as a script rather than steps in the SKILL so the model does not re-derive
file layout, key names or validation every run -- and so a bad answer fails
loudly here instead of silently producing a config that tracks nothing.

Reads JSON on stdin:
  {"timezone": "...", "personal_account": "...", "work_calendar_id": "..."|null,
   "chrome_history_path": "..."|null,
   "google_work_account": 1|null, "work_url_keywords": [...]|null,
   "work": [...], "ignore": [...],
   "break_minutes": 3, "walked_away_minutes": 10, "shortest_stretch_minutes": 3}
"""
import json, os, sys

PROFILE = os.path.expanduser("~/.config/worktime/profile.json")
DOMAINS = os.path.expanduser("~/.config/activity-export/work-domains.json")


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    try:
        a = json.load(sys.stdin)
    except ValueError as e:
        fail(f"answers were not valid JSON: {e}")

    for k in ("timezone", "personal_account", "work", "ignore"):
        if k not in a:
            fail(f"missing required answer: {k}")
    if not a["work"]:
        fail("the 'work' list is empty -- nothing would ever count as working")
    overlap = {w.lower() for w in a["work"]} & {i.lower() for i in a["ignore"]}
    if overlap:
        fail(f"these appear in both work and ignore: {sorted(overlap)}")

    def minutes(key, default):
        v = a.get(key, default)
        if not isinstance(v, (int, float)) or v <= 0:
            fail(f"{key} must be a positive number of minutes, got {v!r}")
        return int(v * 60)

    profile = {
        "_comment": "Written by the worktime-setup skill. Safe to edit by hand.",
        "timezone": a["timezone"],
        "personal_account": a["personal_account"],
    }
    if a.get("work_calendar_id"):
        profile["work_calendar_id"] = a["work_calendar_id"]
    if a.get("chrome_history_path"):
        profile["chrome_history_path"] = a["chrome_history_path"]
    if a.get("google_work_account") is not None:
        index = a["google_work_account"]
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            fail(f"google_work_account must be an account number like 1, got {index!r}")
        profile["google_work_account"] = index
    if a.get("work_url_keywords") is not None:
        keywords = a["work_url_keywords"]
        if not isinstance(keywords, list) or any(not str(k).strip() for k in keywords):
            fail(f"work_url_keywords must be a list of non-empty words, got {keywords!r}")
        profile["work_url_keywords"] = [str(k).strip().lower() for k in keywords]

    domains = {
        "_comment": "Written by the worktime-setup skill. 'ignore' wins over 'work'.",
        "work": a["work"],
        "ignore": a["ignore"],
        "gap_sec": minutes("break_minutes", 3),
        "max_dwell_sec": minutes("walked_away_minutes", 10),
        "min_block_sec": minutes("shortest_stretch_minutes", 3),
        "min_dwell_sec": 30,
    }

    for path, data in ((PROFILE, profile), (DOMAINS, domains)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        print(f"wrote {path}")

    print(json.dumps({"profile": profile, "domains": domains}, indent=2))


if __name__ == "__main__":
    main()
