#!/usr/bin/env python3
"""Export today's calendar to <dashboard>/calendar-today.md, in the format
specified by Dashboard/calendar-export-brief.md, which a probe on Oliver's Mac
reads to decide whether a quiet stretch of the day was a meeting rather than
idle time. The dashboard directory differs per machine and is resolved by
worktime_common; this file used to name the Linux box's path outright, so on
the Mac it wrote where nothing reads.

Two kinds of row, tagged in the Calendar column so the probe never has to
guess from the title:

  personal  an event off one of the personal account's own calendars, with
            its real title
  work      an opaque block seen only through the work free/busy share

The personal account also subscribes to the work calendar, and every personal
appointment is mirrored there as an untitled Busy block -- sometimes more than
once. A titled event therefore wins: a work block whose start and end match a
personal event is that same appointment and is dropped. A work block with no
personal counterpart is a real meeting and is kept as "(busy)", since that is
the only trace a work meeting leaves.

Excluded outright (per the brief): all-day events, declined invitations,
events marked free/transparent, and anything not on today's local date. A
phantom row silently converts genuine idle time into recorded work, so
accuracy matters more than completeness.

Reads GOOGLE_TASKS_CLIENT_ID / GOOGLE_TASKS_CLIENT_SECRET /
GOOGLE_CALENDAR_REFRESH_TOKEN from ~/.claude/tokens.env (parsed directly,
never relies on the process environment). The refresh token carries the
calendar.readonly scope only; the tasks refresh token is a separate value.

Timezone math uses zoneinfo.ZoneInfo (aware datetimes), never the process's
OS timezone -- day-window, HH:MM and the `updated:` offset must be identical
no matter what TZ the box or a test runner happens to be in. Python 3.8 on
this box predates the stdlib zoneinfo module (3.9+), so this falls back to the
backports.zoneinfo PyPI package (same API, same system tzdata).

Exit codes: 0 on a successful write (including an empty day). Non-zero on any
auth/API failure, and the old file is left completely untouched -- a stale
file reads as "known good but old" to the Mac-side probe, which is the
correct degraded state; a half-written file is not.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

# realpath, not abspath: this file may be reached through a symlink on PATH,
# and abspath would look for the shared module beside the symlink.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (this box: 3.8.10)
    from backports.zoneinfo import ZoneInfo

PROFILE_PATH = os.path.expanduser("~/.config/worktime/profile.json")


def load_profile(path=None):
    """Per-person identity: timezone, which accounts are personal vs work.

    Hardcoding these made the pipeline single-user. A missing file keeps the
    original author's values so an existing install does not break on upgrade.
    """
    path = path if path is not None else PROFILE_PATH
    defaults = {"timezone": wc.DEFAULT_TZ_NAME,
                "personal_account": "oliverullman@gmail.com",
                "work_calendar_id": "oliver.ullman@rubrik.com"}
    if not os.path.exists(path):
        return defaults
    try:
        with open(path) as f:
            loaded = json.load(f)
    except (OSError, ValueError) as e:
        raise CalendarExportError(f"profile {path} is unreadable: {e}")
    unknown = set(loaded) - set(defaults) - {"_comment", "chrome_history_path"}
    if unknown:
        raise CalendarExportError(f"profile {path}: unknown key(s) {sorted(unknown)}")
    return {**defaults, **{k: v for k, v in loaded.items() if k in defaults}}


_PROFILE = None
TZ_NAME = wc.DEFAULT_TZ_NAME
try:
    _PROFILE = load_profile()
    TZ_NAME = _PROFILE["timezone"]
except Exception:  # a broken profile must not stop the module importing; run() re-reads it
    _PROFILE = None
NY_TZ = ZoneInfo(TZ_NAME)

TOKENS_PATH = os.path.expanduser("~/.claude/tokens.env")
OUTPUT_PATH = os.path.join(wc.dashboard_dir(), "calendar-today.md")
OVERRIDES_PATH = os.path.expanduser("~/.config/calendar-export/overrides.json")
WORK_BLOCKS_PATH = os.path.expanduser("~/.cache/activity-export/chrome-work-blocks.json")
BROWSING_LABEL = "Working (browsing)"
PERSONAL_ACCOUNT = (_PROFILE or {}).get("personal_account", "oliverullman@gmail.com")
WORK_CALENDAR_ID = (_PROFILE or {}).get("work_calendar_id", "oliver.ullman@rubrik.com")
# "My calendars" on the personal account are exactly the ones it can write.
# Read-only subscriptions (holidays, football fixtures, a school calendar) are
# not appointments Oliver attends and must not become work rows.
PERSONAL_ACCESS_ROLES = ("owner", "writer")
BUSY_LABEL = "(busy)"
PERSONAL_SOURCE = "personal"
WORK_SOURCE = "work"

WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


class CalendarExportError(Exception):
    """Any failure that must leave the existing output file untouched."""


def load_tokens_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def get_access_token(env, http_post=None, tokens_path=TOKENS_PATH):
    http_post = http_post or _http_post_form
    for key in (
        "GOOGLE_TASKS_CLIENT_ID",
        "GOOGLE_TASKS_CLIENT_SECRET",
        "GOOGLE_CALENDAR_REFRESH_TOKEN",
    ):
        if key not in env or not env[key]:
            raise CalendarExportError(f"{key} missing from {tokens_path}")

    data = {
        "client_id": env["GOOGLE_TASKS_CLIENT_ID"],
        "client_secret": env["GOOGLE_TASKS_CLIENT_SECRET"],
        "refresh_token": env["GOOGLE_CALENDAR_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }
    try:
        body = http_post("https://oauth2.googleapis.com/token", data)
    except urllib.error.HTTPError as e:
        raise CalendarExportError(f"token refresh failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise CalendarExportError(f"token refresh failed: {e.reason}")

    parsed = json.loads(body)
    if "access_token" not in parsed:
        raise CalendarExportError(f"token refresh response missing access_token: {parsed}")
    return parsed["access_token"]


def day_window(local_date, tz=NY_TZ):
    """Return (time_min, time_max) as RFC3339 strings spanning local midnight
    to next local midnight on local_date, in tz. Correct across a DST
    transition day because each boundary is constructed from a wall-clock
    date/time localized independently (not by adding a fixed 24h offset)."""
    start = datetime(local_date.year, local_date.month, local_date.day, 0, 0, 0, tzinfo=tz)
    next_day = local_date + timedelta(days=1)
    end = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=tz)
    return start.isoformat(), end.isoformat()


def fetch_calendar_list(access_token, http_get=None):
    http_get = http_get or _http_get_json
    url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
    try:
        body = http_get(url, access_token)
    except urllib.error.HTTPError as e:
        raise CalendarExportError(f"calendarList failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise CalendarExportError(f"calendarList failed: {e.reason}")

    parsed = json.loads(body)
    if "items" not in parsed:
        raise CalendarExportError(f"calendarList response missing items: {parsed}")
    return parsed["items"]


def personal_calendar_ids(calendar_list):
    """Ids of the personal account's own calendars, discovered rather than
    hard-coded so a calendar added later is picked up without a code change."""
    ids = []
    for cal in calendar_list:
        if cal.get("id") == WORK_CALENDAR_ID:
            continue
        if cal.get("accessRole") in PERSONAL_ACCESS_ROLES:
            ids.append(cal["id"])
    return ids


def fetch_events(access_token, calendar_id, time_min, time_max, http_get=None):
    http_get = http_get or _http_get_json
    params = urllib.parse.urlencode({
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
    })
    url = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events?{params}"
    try:
        body = http_get(url, access_token)
    except urllib.error.HTTPError as e:
        raise CalendarExportError(f"events.list failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise CalendarExportError(f"events.list failed: {e.reason}")

    parsed = json.loads(body)
    if "items" not in parsed:
        raise CalendarExportError(f"events.list response missing items: {parsed}")
    return parsed["items"]


def self_attendee(event):
    for attendee in event.get("attendees", []):
        if attendee.get("self"):
            return attendee
    return None


def should_include(event):
    """Whether an event says anything about whether a given minute was spent
    working. Anything that does not is dropped before it can become a row."""
    if event.get("status") == "cancelled":
        return False
    if "date" in event.get("start", {}):
        # All-day: "PTO" or a birthday says nothing about 2:15pm specifically,
        # and a full-day row would blanket the whole day as occupied.
        return False
    if event.get("transparency") == "transparent":
        # The organiser marked it as not occupying him.
        return False
    attendee = self_attendee(event)
    if attendee and attendee.get("responseStatus") == "declined":
        return False
    return True


def is_tentative(event):
    """A tentative block is the ambiguous case the probe should ask a human
    about rather than silently counting, so it has to be visible in the row."""
    if event.get("status") == "tentative":
        return True
    attendee = self_attendee(event)
    return bool(attendee and attendee.get("responseStatus") == "tentative")


def event_key(event, tz=NY_TZ):
    """Identity used to recognise the same real-world appointment on two
    calendars: its start and end instants. Compared as absolute instants (not
    as the raw strings) so a copy written with a different timeZone field
    still matches."""
    def part(slot):
        if "date" in slot:
            return ("date", slot["date"])
        return ("dt", datetime.fromisoformat(slot["dateTime"]).astimezone(tz).isoformat())
    return (part(event.get("start", {})), part(event.get("end", {})))


def merge_events(personal_events, work_events, tz=NY_TZ):
    """Return [(event, source)] with each appointment appearing once.

    A work block is dropped when a personal event covers exactly the same
    start and end -- that is the mirror the work calendar makes of every
    personal appointment. A work block at a slot no personal event occupies
    is a genuine work meeting and survives, untitled."""
    tagged = []
    personal_keys = set()
    seen_personal = set()
    for event in personal_events:
        if not should_include(event):
            continue
        key = event_key(event, tz)
        personal_keys.add(key)
        # The same invitation can sit on two personal calendars; identical
        # time and title means one appointment, not two.
        identity = (key, event.get("summary"))
        if identity in seen_personal:
            continue
        seen_personal.add(identity)
        tagged.append((event, PERSONAL_SOURCE))

    seen_work = set()
    for event in work_events:
        if not should_include(event):
            continue
        key = event_key(event, tz)
        if key in personal_keys or key in seen_work:
            continue
        seen_work.add(key)
        tagged.append((event, WORK_SOURCE))
    return tagged


def sanitize_summary(summary):
    """Collapse newlines and escape pipes so a summary can't break the table."""
    collapsed = re.sub(r"\s*\n\s*", " ", summary).strip()
    return collapsed.replace("|", "\\|")


def render_rows(tagged_events, local_date, tz=NY_TZ):
    """Return [(start_hhmm, end_hhmm, label, source)] for local_date."""
    rows = []
    for event, source in tagged_events:
        summary = event.get("summary")
        label = sanitize_summary(summary) if summary else BUSY_LABEL
        if is_tentative(event):
            label = f"{label} (tentative)"

        start_dt = datetime.fromisoformat(event["start"]["dateTime"]).astimezone(tz)
        end_dt = datetime.fromisoformat(event["end"]["dateTime"]).astimezone(tz)

        # Clamp events that started before local midnight (spanning in from
        # yesterday) to 00:00 for display.
        local_midnight = datetime(local_date.year, local_date.month, local_date.day, 0, 0, 0, tzinfo=tz)
        if start_dt < local_midnight:
            start_dt = local_midnight

        rows.append((start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M"), label, source))
    # Several calendars merged, so event order no longer implies time order.
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def events_from_snapshot(doc, tz=NY_TZ):
    """Split a hand-supplied calendar dump into (personal, work) event lists.

    The OAuth path is not the only way to reach a calendar: a Claude client
    holding a Google Calendar connection can read the same events without this
    machine needing a refresh token of its own, which is what keeps the export
    running after the credentials went missing from tokens.env. It supplies a
    flat, verbatim-hostile shape -- one dict per event, no nesting -- because
    every field it emits was retyped by a language model, and a nested Google
    event object is far easier to get subtly wrong than six scalars.

    Rebuilt into Google's shape here rather than teaching should_include and
    friends a second dialect: the filtering rules are the delicate part (a
    declined invitation and a transparent block both have to vanish), and they
    are already right for one input shape. Two shapes would mean two chances
    to get them wrong, and the second one would only be exercised by whichever
    path happened to be running that week.
    """
    personal, work = [], []
    for cal in doc["calendars"]:
        bucket = (work if cal["id"] == WORK_CALENDAR_ID
                  else personal if cal.get("accessRole") in PERSONAL_ACCESS_ROLES
                  else None)
        if bucket is None:
            # A subscribed calendar -- holidays, a football fixture list. Read
            # access to somebody else's calendar says nothing about this
            # person's day, which is the same cut personal_calendar_ids makes.
            continue
        for ev in cal["events"]:
            rebuilt = {
                "status": ev.get("status") or "confirmed",
                "summary": ev.get("summary"),
            }
            if ev.get("transparency"):
                rebuilt["transparency"] = ev["transparency"]
            if ev.get("myResponse"):
                rebuilt["attendees"] = [{"self": True,
                                         "responseStatus": ev["myResponse"]}]
            if ev.get("allDay"):
                # should_include drops these, but it recognises an all-day
                # event by the "date" key rather than by a flag, so the shape
                # has to carry that distinction rather than the flag itself.
                rebuilt["start"] = {"date": ev["start"]}
                rebuilt["end"] = {"date": ev["end"]}
            else:
                # Parsed here, and not only in render_rows, so a malformed
                # timestamp fails the whole run instead of writing a file with
                # one silently missing meeting. Nothing downstream would ever
                # report the absence.
                for slot in ("start", "end"):
                    datetime.fromisoformat(ev[slot]).astimezone(tz)
                rebuilt["start"] = {"dateTime": ev["start"]}
                rebuilt["end"] = {"dateTime": ev["end"]}
            bucket.append(rebuilt)
    return personal, work


def load_snapshot(path, local_date):
    """Read and check a supplied dump, or raise. Never returns a partial read.

    The date is checked against the day being written because the failure it
    guards is invisible downstream: yesterday's meetings rendered under
    today's heading pass every other validation here and then tell the probe
    that an hour nobody worked was a meeting.
    """
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise CalendarExportError(f"cannot read snapshot {path}: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("calendars"), list):
        raise CalendarExportError(f"snapshot {path} has no calendars list")
    if doc.get("date") != local_date.isoformat():
        raise CalendarExportError(
            f"snapshot is for {doc.get('date')!r}, expected {local_date.isoformat()}")
    try:
        return events_from_snapshot(doc)
    except (KeyError, TypeError, ValueError) as e:
        raise CalendarExportError(f"snapshot {path} is malformed: {e}")


def load_overrides(path):
    """Return {date_str: {"overrides": [...], "add": [...]}} of hand corrections.

    Two things the calendar cannot say on its own:
      * "overrides" fix a meeting that overran -- Google records the time a
        meeting was *scheduled*, so a 13:45-14:00 block that ran to 14:07
        leaves the probe treating 14:00-14:07 as idle.
      * "add" records work that generated no calendar event and no prompts at
        all, which the probe would otherwise score as not working.
      * "remove" drops a meeting that was skipped. A work block comes from the
        free/busy share and cannot be declined from here, so without this a
        meeting nobody attended is still credited as working time.

    A missing file is the normal case, not an error.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise CalendarExportError(f"overrides file {path} is unreadable: {e}")
    if not isinstance(data, dict):
        raise CalendarExportError(f"overrides file {path} must be a JSON object keyed by date")
    for day, spec in data.items():
        if not isinstance(spec, dict):
            raise CalendarExportError(
                f"overrides file {path}: {day} must map to an object with "
                f"'overrides' and/or 'add' keys, got {type(spec).__name__}")
        unknown = set(spec) - {"overrides", "add", "remove"}
        if unknown:
            raise CalendarExportError(
                f"overrides file {path}: {day} has unknown key(s) {sorted(unknown)}")
    return data


def load_work_blocks(path, local_date):
    """Rows derived from classified Chrome browsing by chrome-work-blocks.py.

    Reviewing a PR for twenty minutes produces no Claude prompt and no calendar
    event, so the probe scores it as idle. These rows are that missing evidence.
    They are tagged `work` because a `personal` row is explicitly not credited
    as working time.

    A cache for another day is ignored rather than raised on: the deriver runs
    on its own timer and a date rollover between the two is ordinary, not a
    fault.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise CalendarExportError(f"work-blocks cache {path} is unreadable: {e}")
    if data.get("date") != local_date.isoformat():
        return []
    rows = []
    for b in data.get("blocks", []):
        if not (b.get("start") and b.get("end")):
            raise CalendarExportError(f"work-blocks cache {path}: bad block {b!r}")
        rows.append((b["start"], b["end"], BROWSING_LABEL, WORK_SOURCE))
    return rows


def apply_overrides(rows, local_date, overrides, now=None, tz=NY_TZ):
    """Apply hand corrections for local_date: patch matched rows, append marks.

    An override that matches nothing is reported on stderr rather than raised:
    it means a correction is silently doing nothing, which must be visible, but
    a stale entry must not wedge the export and let the file age past the
    probe's 6-hour freshness limit. An added mark is explicit, so a bad one is
    raised rather than warned -- there is no row it could have meant.
    """
    spec = overrides.get(local_date.isoformat())
    if not spec:
        return rows

    patched = list(rows)
    for entry in spec.get("remove", []):
        start = entry.get("start")
        if not start:
            raise CalendarExportError(f"remove entry {entry!r} has no 'start'")
        kept = [row for row in patched if row[0] != start]
        if len(kept) == len(patched):
            print(
                f"calendar-export: remove for {local_date} {start} matched no event",
                file=sys.stderr,
            )
        patched = kept

    for entry in spec.get("overrides", []):
        start = entry.get("start")
        if not start:
            raise CalendarExportError(f"override entry {entry!r} has no 'start'")
        hits = [i for i, row in enumerate(patched) if row[0] == start]
        if not hits:
            print(
                f"calendar-export: override for {local_date} {start} matched no event",
                file=sys.stderr,
            )
            continue
        for i in hits:
            row_start, row_end, label, source = patched[i]
            patched[i] = (row_start, entry.get("end", row_end), entry.get("label", label), source)

    for entry in spec.get("add", []):
        start, end, label = entry.get("start"), entry.get("end"), entry.get("label")
        if not (start and end and label):
            raise CalendarExportError(
                f"added entry {entry!r} needs 'start', 'end' and 'label'")
        # An ongoing mark has no known end yet; "now" makes the row grow with
        # each 15-minute export for as long as the entry stands.
        if end == "now":
            end = (now or datetime.now(tz)).astimezone(tz).strftime("%H:%M")
        patched.append((start, end, label, entry.get("source", PERSONAL_SOURCE)))

    patched.sort(key=lambda row: (row[0], row[1]))
    return patched


def render_markdown(local_date, rows, now=None, tz=NY_TZ):
    now = now or datetime.now(tz)
    weekday = WEEKDAYS[local_date.weekday()]
    # The probe refuses this file once it is over 6 hours old, and it needs a
    # real offset to compare against -- a bare local timestamp read as UTC
    # would look 4 hours older than it is. `updated:` cannot carry that: the
    # vault's update-time-on-edit plugin rewrites that key to a bare
    # Eastern-local stamp within seconds of any write, from Obsidian on
    # Windows, where nothing here can stop it. `generated:` is a key the
    # plugin does not own, so the offset survives there.
    stamp = now.replace(microsecond=0).isoformat()

    lines = [
        "---",
        f"updated: {stamp}",
        f"generated: {stamp}",
        "---",
        f"# Calendar — {weekday} {local_date.isoformat()}",
        "",
        f"Personal calendars on {PERSONAL_ACCOUNT}, plus any work-calendar "
        f"({WORK_CALENDAR_ID}) block with no personal counterpart. "
        f"Timezone {TZ_NAME}.",
        "",
        "| Start | End | Event | Calendar |",
        "|-------|-----|-------|----------|",
    ]
    for start, end, label, source in rows:
        lines.append(f"| {start} | {end} | {label} | {source} |")
    lines.append("")
    lines.append("Notes:")
    if rows:
        lines.append(
            "- `personal` rows are appointments off Oliver's own calendars; `work` rows are "
            "opaque blocks from the Rubrik free/busy share, which exposes no titles."
        )
        lines.append(
            "- Work-calendar mirrors of personal appointments are dropped, so each event appears once."
        )
    else:
        lines.append("- No qualifying events on either calendar today.")
    lines.append("")
    return "\n".join(lines)


def atomic_write(path, content):
    directory = os.path.dirname(path)
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, path)


def _http_post_form(url, data):
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _http_get_json(url, access_token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def run(local_date=None, tokens_path=None, output_path=None,
        http_post=None, http_get=None, now=None, overrides_path=None,
        work_blocks_path=None, snapshot_path=None):
    # tokens_path/output_path default to None (not the module constants
    # directly) so a test's monkeypatch.setattr(module, "TOKENS_PATH", ...)
    # takes effect -- a default bound at def time would freeze in whatever
    # value TOKENS_PATH held when the module was first imported and silently
    # ignore any later monkeypatch, which is exactly the kind of bug that
    # would let a test fall through to real credentials and the real vault
    # file.
    local_date = local_date or datetime.now(NY_TZ).date()
    tokens_path = tokens_path if tokens_path is not None else TOKENS_PATH
    output_path = output_path if output_path is not None else OUTPUT_PATH
    overrides_path = overrides_path if overrides_path is not None else OVERRIDES_PATH
    work_blocks_path = work_blocks_path if work_blocks_path is not None else WORK_BLOCKS_PATH

    if snapshot_path:
        personal, work = load_snapshot(snapshot_path, local_date)
    else:
        env = load_tokens_env(tokens_path)
        access_token = get_access_token(env, http_post=http_post, tokens_path=tokens_path)

        time_min, time_max = day_window(local_date)
        calendar_list = fetch_calendar_list(access_token, http_get=http_get)

        personal = []
        for calendar_id in personal_calendar_ids(calendar_list):
            personal.extend(
                fetch_events(access_token, calendar_id, time_min, time_max, http_get=http_get)
            )
        work = fetch_events(access_token, WORK_CALENDAR_ID, time_min, time_max, http_get=http_get)

    rows = render_rows(merge_events(personal, work), local_date)
    rows = sorted(rows + load_work_blocks(work_blocks_path, local_date),
                  key=lambda row: (row[0], row[1]))
    rows = apply_overrides(rows, local_date, load_overrides(overrides_path), now=now)
    content = render_markdown(local_date, rows, now=now)
    atomic_write(output_path, content)
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-json", metavar="PATH", dest="snapshot",
        help="read events from a supplied JSON dump instead of Google's API. "
             "The way calendar-refresh.sh feeds in what a Claude client read "
             "over its own Calendar connection, so this machine needs no "
             "OAuth credentials of its own.")
    args = parser.parse_args()
    try:
        run(snapshot_path=args.snapshot)
    except CalendarExportError as e:
        print(f"calendar-export: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
