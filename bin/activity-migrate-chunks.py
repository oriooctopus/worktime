#!/usr/bin/env python3
"""One-time migration: convert the old single-file-per-day activity export
(<dashboard>/activity/YYYY-MM-DD.md) into the new chunked layout
(<dashboard>/activity/YYYY-MM-DD/HH.md + periods.md) that activity-export.py
now writes -- see that file's module docstring for why (Obsidian Sync's 1GB
version-history quota).

    activity-migrate-chunks.py <activity-dir> [--apply]

Dry-run by default (prints what it WOULD do); pass --apply to actually write
and delete. Safe to run more than once:

  - A day whose new-format directory does NOT yet exist: rows are parsed out
    of the old file's table + '## Periods' section, written as hour chunks
    (via activity-export.py's own write_if_changed, so this is byte-for-byte
    the same writer the real exporter uses) and periods.md, then RE-READ from
    disk and checked row-for-row against what was parsed -- only on a clean
    match is the old file deleted. Interrupted mid-run (killed after writing
    chunks but before deleting): the old file is simply still there next
    time, and re-running reproduces byte-identical chunks (write_if_changed
    is a no-op on a re-run) before re-attempting the delete -- so a partial
    run is always safe to resume by re-running.

  - A day whose new-format directory ALREADY exists (this box's real
    activity-export.service kept running throughout development and had
    already written today/yesterday in the new layout before this script's
    first real run): the old file's rows are checked as a SUBSET of what the
    live directory already holds, not equality -- the live export has kept
    moving and may hold rows newer than the stale old file's last write. Only
    when every old row is already present is the old file deleted; otherwise
    it's left in place and reported, never overwritten with stale content.

The old day files have no epoch, only HH:MM local time -- rows within an hour
keep their original within-file order (already epoch-sorted by the old
exporter), per the migration's row-ordering requirement.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import importlib.util as _ilu  # noqa: E402

_ae_spec = _ilu.spec_from_file_location(
    "activity_export", os.path.join(os.path.dirname(os.path.realpath(__file__)), "activity-export.py"))
ae = _ilu.module_from_spec(_ae_spec)
_ae_spec.loader.exec_module(ae)

DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def split_table_row(line):
    """Split a markdown table row on '|' not preceded by a backslash --
    matches activity-export.py's own '\\|' pipe-escaping (sanitize_detail),
    so a detail cell that legitimately contains an escaped pipe is not
    mistaken for an extra column boundary."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [p.strip() for p in re.split(r"(?<!\\)\|", s)]


def parse_old_day_file(path):
    """(events_rows, periods_rows) where events_rows is [(time_str, source,
    direction, who, detail), ...] in original file order (already
    epoch-sorted -- see module docstring), and periods_rows is [(start, end,
    summary), ...]. Deliberately ignores the old frontmatter entirely (its
    aggregate counts/last_seen/generated_at/errors/llm_calls fields are all
    either removed by design or trivially recomputable from the rows
    themselves -- see activity-export.py's build_hour_markdown)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    events_part, _, periods_part = content.partition("\n## Periods")

    events_rows = []
    in_table = False
    for line in events_part.splitlines():
        if line.startswith("| time |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cells = split_table_row(line)
            if len(cells) == 5:
                events_rows.append(tuple(cells))
        elif in_table and not line.startswith("|"):
            in_table = False  # table ended (blank line before '## Periods' etc)

    periods_rows = []
    in_table = False
    for line in periods_part.splitlines():
        if line.startswith("| start |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cells = split_table_row(line)
            if len(cells) == 3:
                periods_rows.append(tuple(cells))
        elif in_table and not line.startswith("|"):
            in_table = False
    return events_rows, periods_rows


def events_rows_from_hour_chunks(day_dir):
    """Re-read every hour chunk under day_dir and return the same
    (time_str, source, direction, who, detail) tuples parse_old_day_file
    produces, in hour-then-file-order -- what the migration verifies its own
    write against."""
    rows = []
    if not os.path.isdir(day_dir):
        return rows
    for hour in ae.HOUR_RANGE:
        path = os.path.join(day_dir, f"{hour}.md")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            in_table = False
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("| time |"):
                    in_table = True
                    continue
                if in_table and line.startswith("|---"):
                    continue
                if in_table and line.startswith("|"):
                    cells = split_table_row(line)
                    if len(cells) == 5:
                        rows.append(tuple(cells))
    return rows


def periods_rows_from_file(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        in_table = False
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("| start |"):
                in_table = True
                continue
            if in_table and line.startswith("|---"):
                continue
            if in_table and line.startswith("|"):
                cells = split_table_row(line)
                if len(cells) == 3:
                    rows.append(tuple(cells))
    return rows


def rows_to_events(rows, day):
    """(time_str, source, direction, who, detail) tuples -> ae.Event list.
    epoch is set to a synthetic, monotonically increasing placeholder (the
    old file has no real epoch) -- fine here because build_hour_markdown
    only uses epoch to ORDER rows that share a bucket, and these rows are
    already in the correct final order from the old file; a stable index
    preserves that order through the sort exactly."""
    events = []
    for i, (time_str, source, direction, who, detail) in enumerate(rows):
        events.append(ae.Event(epoch=i, day=day, time_str=time_str, source=source,
                                direction=direction, who=who, detail=detail))
    return events


def migrate_day(activity_dir, day_str, apply, log):
    from datetime import date as _date
    y, m, d = (int(x) for x in day_str.split("-"))
    day = _date(y, m, d)
    old_path = os.path.join(activity_dir, f"{day_str}.md")
    day_dir = os.path.join(activity_dir, day_str)
    old_rows, old_periods = parse_old_day_file(old_path)

    live_already = os.path.isdir(day_dir)
    if live_already:
        # The real exporter has already written this day in the new layout
        # (see module docstring) -- never regress it to the old file's
        # possibly-stale content. Just verify every old row is already
        # covered, then the old file is pure redundancy.
        current_rows = set(events_rows_from_hour_chunks(day_dir))
        missing = [r for r in old_rows if r not in current_rows]
        if missing:
            log(f"{day_str}: SKIP (live dir exists, {len(missing)} old row(s) "
                f"not found in it -- leaving old file in place for manual review)")
            return False
        log(f"{day_str}: live dir already covers all {len(old_rows)} old row(s)"
            + (" -- deleting old file" if apply else " -- would delete old file"))
        if apply:
            os.remove(old_path)
        return True

    events = rows_to_events(old_rows, day)
    if apply:
        ae.write_day_chunks(activity_dir, day, events)
        raw_periods = [
            (start, end, summary) for start, end, summary in old_periods
        ]
        # periods.md has no epoch to reconstruct either -- write_periods_file
        # only needs period[0].time_str/period[-1].time_str (for the start/
        # end columns) and the pre-computed summary string, so a minimal
        # one-event-per-period stand-in carries exactly what's needed.
        periods_as_events = [
            [ae.Event(epoch=0, day=day, time_str=start, source="", direction="", who="", detail=""),
             ae.Event(epoch=1, day=day, time_str=end, source="", direction="", who="", detail="")]
            for start, end, _ in raw_periods
        ]
        summaries = [summary for _, _, summary in raw_periods]
        ae.write_periods_file(activity_dir, day, periods_as_events, summaries)

        # Re-read back what was just written and verify row-for-row equality
        # (rule: never delete the old file on anything less than a proven
        # match) before removing the old source file.
        written_rows = events_rows_from_hour_chunks(day_dir)
        written_periods = periods_rows_from_file(os.path.join(day_dir, "periods.md"))
        if written_rows != list(old_rows):
            log(f"{day_str}: MISMATCH after write -- old file NOT deleted "
                f"({len(old_rows)} old rows vs {len(written_rows)} written)")
            return False
        if written_periods != list(old_periods):
            log(f"{day_str}: PERIODS MISMATCH after write -- old file NOT deleted")
            return False
        os.remove(old_path)
        log(f"{day_str}: migrated {len(old_rows)} row(s), {len(old_periods)} period(s) -- verified, old file deleted")
    else:
        log(f"{day_str}: would migrate {len(old_rows)} row(s), {len(old_periods)} period(s)")
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("activity_dir")
    parser.add_argument("--apply", action="store_true",
                         help="actually write/delete; default is dry-run")
    args = parser.parse_args(argv)

    names = sorted(n for n in os.listdir(args.activity_dir) if DAY_FILE_RE.match(n))
    if not names:
        print("no old-format day files found -- nothing to migrate")
        return 0
    ok = True
    for name in names:
        day_str = DAY_FILE_RE.match(name).group(1)
        if not migrate_day(args.activity_dir, day_str, args.apply, print):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
