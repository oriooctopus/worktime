#!/usr/bin/env python3
"""Print the domains this person actually visits most, so the setup skill can
propose real options instead of asking someone to invent a list from memory.

Read-only. Prints domains and visit counts, never URLs or page titles.
"""
import collections, datetime, os, shutil, sqlite3, sys, tempfile
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "bin"))
import importlib.util
spec = importlib.util.spec_from_file_location("cwb", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin", "chrome-work-blocks.py"))
cwb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cwb)

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14


def main():
    src = cwb.chrome_history_path()
    if not os.path.exists(src):
        print(f"no Chrome history found at {src}", file=sys.stderr)
        sys.exit(1)
    d = tempfile.mkdtemp(prefix="worktime-suggest-")
    tmp = os.path.join(d, "History")
    try:
        shutil.copy2(src, tmp)
        conn = sqlite3.connect(tmp)
        since = cwb.chrome_ts(datetime.datetime.utcnow() - datetime.timedelta(days=DAYS))
        counts = collections.Counter()
        for (url,) in conn.execute(
                "SELECT urls.url FROM visits JOIN urls ON urls.id=visits.url "
                "WHERE visits.visit_time > ?", (since,)):
            host = (urlparse(url).hostname or "").lower().lstrip("www.")
            if host:
                counts[host] += 1
        conn.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print(f"Most-visited sites over the last {DAYS} days:")
    for host, n in counts.most_common(25):
        print(f"  {n:6}  {host}")


if __name__ == "__main__":
    main()
