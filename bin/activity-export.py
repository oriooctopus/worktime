#!/usr/bin/env python3
"""Export WhatsApp/iMessage/Chrome activity to daily markdown files in the
Obsidian vault, so a dashboard on another machine can explain gaps in the
workday.

Writes <dashboard>/activity/YYYY-MM-DD.md for today and yesterday (local days)
by default; `backfill N` writes the last N days (today back through
today-(N-1)). The dashboard directory and the timezone are both resolved by
worktime_common, because the vault sits at a different path on each machine and
this used to name the Linux box's one outright -- so on the Mac the export was
written where nothing reads it.

Timezone math uses `zoneinfo.ZoneInfo` (aware datetimes) throughout, never
the process's OS timezone (`time.tzset`/`time.localtime`/`time.mktime`) --
day attribution and HH:MM must be identical no matter what TZ the box or the
test runner happens to be in. Python 3.8 on this box predates the stdlib
`zoneinfo` module (3.9+), so this falls back to the `backports.zoneinfo` PyPI
package, which provides the exact same API and reads the same system tzdata.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

# realpath, not abspath: this file may be reached through a symlink on PATH,
# and abspath would look for the shared module beside the symlink.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (this box: 3.8.10)
    from backports.zoneinfo import ZoneInfo

TZ_NAME = wc.tz_name()
NY_TZ = ZoneInfo(TZ_NAME)

VAULT_DIR = os.path.join(wc.dashboard_dir(), "activity")
CACHE_PATH = os.path.expanduser("~/.cache/activity-export/summaries.json")
ASK_HAIKU = os.path.expanduser("~/.claude/bin/ask-haiku.sh")
LLM_CALL_CAP = 20
PERIOD_GAP_SECONDS = 15 * 60
PERIOD_MAX_SECONDS = 45 * 60
PERIOD_OPEN_SECONDS = 20 * 60
WHATSAPP_DB = os.path.expanduser(
    "~/coding/whatsapp-mcp/whatsapp-bridge/store/messages.db"
)
WHATSAPP_ACCOUNT_DB = os.path.expanduser(
    "~/coding/whatsapp-mcp/whatsapp-bridge/store/whatsapp.db"
)
CALLS_JSONL = os.path.expanduser(
    "~/coding/whatsapp-mcp/whatsapp-bridge/store/calls.jsonl"
)
IMESSAGE_DIR = os.path.expanduser("~/inbox/imessage-sync")
PHONE_CALLS_GLOB = os.path.expanduser("~/inbox/imessage-sync/calls-*.json")
CHROME_HISTORY = "/mnt/c/chrome-cdp-profile/Default/History"
# Both Claude profiles, not just the interactive one: background jobs write
# their transcripts under ~/.claude-personal, and a prompt typed at one of those
# is a prompt the person typed. Reading only the first is what kept an evening
# of background-job work out of the activity export entirely.
CLAUDE_PROJECTS_GLOBS = [os.path.join(root, "*", "*.jsonl")
                         for root in wc.PROJECT_ROOTS]
REDACT_CONFIG_PATH = os.path.expanduser("~/.config/activity-export/redact.json")
CLAUDE_OFFSET_CACHE_PATH = os.path.expanduser("~/.cache/activity-export/claude-offsets.json")

# Observed on this box 2026-08-26: whatsmeow_device row has
# jid='17205846358:58@s.whatsapp.net', lid='2152181272617:58@lid' (single
# linked device, push_name "Oliver Ullman"). Also cross-checked: every
# `accept` line in calls.jsonl comes from 2152181272617@lid. Used only as a
# fallback when whatsapp.db can't be read.
DEFAULT_SELF_IDS = {"2152181272617", "17205846358"}

CALL_KINDS = ("offer", "accept", "reject", "terminate")

Event = namedtuple(
    "Event", ["epoch", "day", "time_str", "source", "direction", "who", "detail"]
)


# --------------------------------------------------------------------------
# Time helpers -- aware datetimes only, never the process/OS timezone.
# --------------------------------------------------------------------------


def local_day_time_from_epoch(epoch):
    """epoch (UTC instant) -> (date, 'HH:MM') in the configured local time."""
    dt = datetime.fromtimestamp(epoch, tz=NY_TZ)
    return dt.date(), dt.strftime("%H:%M")


def utc_offset_str(epoch):
    dt = datetime.fromtimestamp(epoch, tz=NY_TZ)
    offset = dt.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hh, mm = divmod(total_minutes, 60)
    return f"{sign}{hh:02d}:{mm:02d}"


# --------------------------------------------------------------------------
# Text sanitization
# --------------------------------------------------------------------------


def _collapse_whitespace(s):
    return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def sanitize_detail(s, maxlen=80):
    if s is None:
        s = ""
    s = _collapse_whitespace(s)
    s = s.replace("|", "\\|")
    s = s.strip()
    if len(s) > maxlen:
        s = s[:maxlen]
    return s


def truncate_at_word_boundary(s, maxlen):
    """Truncate to at most maxlen chars without splitting a word: back up to
    the last whitespace before the cut and append '…' (counted inside
    maxlen). A single token longer than maxlen still gets a hard char cut
    (nothing to back up to) rather than being emitted whole."""
    if len(s) <= maxlen:
        return s
    limit = max(maxlen - 1, 0)  # room for the ellipsis
    cut = s[:limit]
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    cut = cut.rstrip(" ,.;:·")
    return cut + "…"


def sanitize_period_summary(s, maxlen=110):
    """Like sanitize_detail (collapse whitespace, escape pipes) but truncates
    at a word boundary with an ellipsis instead of a hard mid-word cut --
    period summaries read as prose, not a fixed-width detail cell."""
    if s is None:
        s = ""
    s = _collapse_whitespace(s)
    s = s.replace("|", "\\|")
    s = s.strip()
    return truncate_at_word_boundary(s, maxlen)


def sanitize_who(s):
    if s is None:
        s = ""
    s = _collapse_whitespace(s)
    s = s.replace("|", "\\|")
    return s.strip()


def yaml_quote(s):
    escaped = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# JID helpers (WhatsApp)
# --------------------------------------------------------------------------


def strip_device(jid):
    """'182093845934191:21@lid' -> '182093845934191@lid'."""
    if not jid:
        return jid
    if "@" in jid:
        local, domain = jid.split("@", 1)
        local = local.split(":")[0]
        return f"{local}@{domain}"
    return jid.split(":")[0]


def jid_number(jid):
    if not jid:
        return jid
    local = jid.split("@")[0]
    return local.split(":")[0]


def resolve_self_ids(whatsapp_account_db):
    try:
        conn = sqlite3.connect(f"file:{whatsapp_account_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT jid, lid FROM whatsmeow_device LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row:
            ids = set()
            for val in row:
                if val:
                    ids.add(jid_number(val))
            if ids:
                return ids
    except Exception:
        pass
    return set(DEFAULT_SELF_IDS)


def load_chat_names_safe(messages_db):
    try:
        conn = sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True)
        try:
            return {
                jid: name
                for jid, name in conn.execute("SELECT jid, name FROM chats")
                if name
            }
        finally:
            conn.close()
    except Exception:
        return {}


def load_lid_map_safe(whatsapp_account_db):
    """lid (bare digits) -> pn (bare digits), from whatsmeow_lid_map."""
    try:
        conn = sqlite3.connect(f"file:{whatsapp_account_db}?mode=ro", uri=True)
        try:
            return {
                str(lid): str(pn)
                for lid, pn in conn.execute("SELECT lid, pn FROM whatsmeow_lid_map")
            }
        finally:
            conn.close()
    except Exception:
        return {}


def load_contacts_safe(whatsapp_account_db):
    """pn@s.whatsapp.net -> (first_name, full_name, push_name)."""
    try:
        conn = sqlite3.connect(f"file:{whatsapp_account_db}?mode=ro", uri=True)
        try:
            out = {}
            for their_jid, first_name, full_name, push_name in conn.execute(
                "SELECT their_jid, first_name, full_name, push_name "
                "FROM whatsmeow_contacts"
            ):
                out[their_jid] = (first_name, full_name, push_name)
            return out
        finally:
            conn.close()
    except Exception:
        return {}


def resolve_lid_digits(lid_digits, lid_map, contacts):
    """Bare-digit LID -> a display name via whatsmeow_lid_map + whatsmeow_contacts,
    or None if it can't be resolved (caller should keep its own fallback)."""
    if not lid_digits or not str(lid_digits).isdigit():
        return None
    pn = lid_map.get(str(lid_digits))
    if not pn:
        return None
    contact = contacts.get(f"{pn}@s.whatsapp.net")
    if contact:
        first_name, full_name, push_name = contact
        if full_name:
            return full_name
        if first_name:
            return first_name
        if push_name:
            return push_name
    return f"+{pn}"


def resolve_who(jid, chat_names, lid_map, contacts):
    """Best-effort display name for a jid/chat_jid: a real chats.name wins;
    a chats.name that is itself a bare LID (or no name at all) gets a shot at
    whatsmeow_lid_map -> whatsmeow_contacts before falling back to raw jid."""
    name = chat_names.get(jid)
    if name and not name.isdigit():
        return name
    # The bridge sometimes stores the account's own LID as a chat's name, so
    # the chat's jid is the trustworthy identity; a digit-only name is a hint.
    for candidate in (jid_number(jid), name):
        resolved = resolve_lid_digits(candidate, lid_map, contacts)
        if resolved:
            return resolved
    return name or jid


# --------------------------------------------------------------------------
# Source 1: WhatsApp messages
# --------------------------------------------------------------------------


def whatsapp_events(db_path, lid_map, contacts):
    events = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        chat_names = {
            jid: name
            for jid, name in conn.execute("SELECT jid, name FROM chats")
            if name
        }
        rows = conn.execute(
            "SELECT chat_jid, sender, content, timestamp, is_from_me, media_type "
            "FROM messages"
        )
        for chat_jid, sender, content, ts, is_from_me, media_type in rows:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S%z")
            epoch = dt.timestamp()
            day, time_str = local_day_time_from_epoch(epoch)
            if is_from_me:
                direction = "sent (assistant)" if sender == "me" else "sent"
            else:
                direction = "received"
            who = resolve_who(chat_jid, chat_names, lid_map, contacts)
            text = content or ""
            if not text.strip() and media_type:
                detail = f"[{media_type}]"
            else:
                detail = text
            events.append(
                Event(
                    epoch,
                    day,
                    time_str,
                    "whatsapp",
                    direction,
                    who,
                    sanitize_detail(detail),
                )
            )
    finally:
        conn.close()
    return events


# --------------------------------------------------------------------------
# Source 2: WhatsApp calls
# --------------------------------------------------------------------------


def call_events(jsonl_path, self_ids, chat_names, lid_map, contacts):
    groups = {}
    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cid = d.get("call_id")
            kind = d.get("kind")
            if not cid or kind not in CALL_KINDS:
                continue
            g = groups.setdefault(
                cid,
                {
                    "call_creator": d.get("call_creator"),
                    "froms": set(),
                    "offer": None,
                    "accept": None,
                    "reject": None,
                    "terminate": None,
                },
            )
            ts = d.get("ts")
            try:
                dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
            except (ValueError, TypeError):
                continue
            epoch = dt.timestamp()
            if g[kind] is None or epoch < g[kind]:
                g[kind] = epoch
            frm = d.get("from")
            if frm:
                g["froms"].add(frm)

    events = []
    for g in groups.values():
        offer = g["offer"]
        if offer is None:
            candidates = [
                g[k] for k in ("accept", "reject", "terminate") if g[k] is not None
            ]
            if not candidates:
                continue
            offer = min(candidates)

        if g["accept"] is not None:
            status = "answered"
        elif g["reject"] is not None:
            status = "rejected"
        elif g["terminate"] is not None:
            status = "missed"
        else:
            status = "in progress"

        # NOTE: duration is deliberately not computed. Checked all 147 real
        # calls in calls.jsonl -- every `accept` is followed by `terminate`
        # within 0-13s (the linked device drops off once the phone itself
        # answers; it never observes the real hang-up), so accept->terminate
        # is not a real call duration and would be actively misleading.

        creator_norm = jid_number(g["call_creator"])
        direction = "outgoing" if creator_norm in self_ids else "incoming"

        if direction == "incoming":
            counterpart_jid = strip_device(g["call_creator"])
        else:
            counterpart_jid = None
            for frm in g["froms"]:
                if "@call" in frm:
                    continue
                if jid_number(frm) not in self_ids:
                    counterpart_jid = strip_device(frm)
                    break

        if counterpart_jid:
            who = resolve_who(counterpart_jid, chat_names, lid_map, contacts)
        else:
            who = "(unknown)"

        day, time_str = local_day_time_from_epoch(offer)
        events.append(
            Event(offer, day, time_str, "call", direction, who, status)
        )
    return events


# --------------------------------------------------------------------------
# Source 3: iMessage
# --------------------------------------------------------------------------


def imessage_events(dir_path):
    events = []
    bad_files = 0
    records = {}  # rowid -> record; later (newer) snapshot files win

    paths = sorted(glob.glob(os.path.join(dir_path, "messages-*.json")))
    for path in paths:
        try:
            if os.path.getsize(path) == 0:
                bad_files += 1
                continue
            with open(path, "r") as f:
                data = json.load(f)
            if not isinstance(data, list):
                bad_files += 1
                continue
        except (OSError, ValueError):
            bad_files += 1
            continue
        for rec in data:
            rowid = rec.get("rowid")
            if rowid is None:
                continue
            records[rowid] = rec

    for rec in records.values():
        ts = rec.get("ts")
        try:
            dt_naive = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        # ts has no offset -- it is already local wall-clock
        # time. fold=0 (the default) picks the earlier of the two instants
        # on an ambiguous DST fall-back local time.
        dt = dt_naive.replace(tzinfo=NY_TZ)
        epoch = dt.timestamp()
        day = dt.date()
        time_str = dt.strftime("%H:%M")
        direction = "sent" if rec.get("from_me") else "received"
        who = rec.get("chat_name") or rec.get("sender") or ""
        events.append(
            Event(
                epoch,
                day,
                time_str,
                "imessage",
                direction,
                who,
                sanitize_detail(rec.get("text") or ""),
            )
        )

    notes = []
    if bad_files:
        plural = "s" if bad_files != 1 else ""
        notes.append(f"imessage: {bad_files} unreadable snapshot{plural}")
    return events, notes


# --------------------------------------------------------------------------
# Source 4: Chrome history
# --------------------------------------------------------------------------


def webkit_to_epoch(visit_time):
    return visit_time / 1e6 - 11644473600


def chrome_events(history_path):
    tmp_dir = tempfile.mkdtemp(prefix="activity-export-chrome-")
    tmp_copy = os.path.join(tmp_dir, "History")
    try:
        shutil.copy2(history_path, tmp_copy)
        conn = sqlite3.connect(
            f"file:{tmp_copy}?mode=ro&immutable=1", uri=True
        )
        try:
            rows = list(
                conn.execute(
                    "SELECT v.visit_time, u.url, u.title, v.originator_cache_guid "
                    "FROM visits v JOIN urls u ON v.url = u.id "
                    "ORDER BY v.visit_time"
                )
            )
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    raw_by_day = {}
    for visit_time, url, title, guid in rows:
        epoch = webkit_to_epoch(visit_time)
        day, time_str = local_day_time_from_epoch(epoch)
        # This profile's History has NOT reliably distinguished a "Mac" vs a
        # "Windows" device by originator_cache_guid across the session (the
        # per-guid split has shifted mid-session -- see the export report).
        # Until that's settled: empty guid = written directly by this local
        # (Windows) Chrome; any non-empty guid = arrived via sync, labelled
        # "synced" rather than assuming it's specifically a Mac.
        device = "synced" if guid else "Windows"
        raw_by_day.setdefault(day, []).append(
            (epoch, time_str, url or "", title or "", device)
        )

    events = []
    for day, visits in raw_by_day.items():
        visits.sort(key=lambda v: v[0])
        group = None
        for epoch, time_str, url, title, device in visits:
            # Collapse rule: a visit joins the current group if it's the same
            # URL+device and within 60s of the GROUP'S FIRST (anchor) visit,
            # not the previous visit -- 60s is inclusive (<=).
            if (
                group is not None
                and group["url"] == url
                and group["device"] == device
                and (epoch - group["epoch"]) <= 60
            ):
                group["count"] += 1
            else:
                if group is not None:
                    events.append(_flush_chrome_group(day, group))
                group = {
                    "epoch": epoch,
                    "time_str": time_str,
                    "url": url,
                    "title": title,
                    "device": device,
                    "count": 1,
                }
        if group is not None:
            events.append(_flush_chrome_group(day, group))
    return events


def _flush_chrome_group(day, g):
    detail = f"{g['title']} — {g['url']}"
    if g["count"] > 1:
        detail += f" (×{g['count']})"
    return Event(
        g["epoch"], day, g["time_str"], "chrome", "visit", g["device"],
        sanitize_detail(detail),
    )


# --------------------------------------------------------------------------
# Source 5: Claude Code prompts (this box's transcripts) -- with mandatory
# redaction of a private project. See load_redact_config: if the redaction
# config can't be loaded, this source fails closed (zero rows) rather than
# risk leaking anything.
# --------------------------------------------------------------------------

_COMMAND_MESSAGE_RE = re.compile(r"<command-message>.*?</command-message>", re.DOTALL)
_COMMAND_NAME_RE = re.compile(r"<command-name>.*?</command-name>", re.DOTALL)
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

REDACTED_WHO = "autojournal"
REDACTED_DETAIL = "autojournal work"

# Noise filters -- things Oliver never typed himself: background-agent
# notices, skill-body injections, pasted tool/scrape output, automation
# personas. Checked as substrings of the RAW (pre-strip) content so nesting
# inside a wrapper tag can't hide them.
_NOISE_CONTAINS_MARKERS = ("<task-notification>", "<cross-session-message")
# Checked on the text AFTER wrapper-tag stripping, at the start only.
_NOISE_PREFIX_MARKERS = ("Base directory for this skill", "<", "[Image", "Caveat:")


def strip_claude_wrappers(text):
    text = _COMMAND_MESSAGE_RE.sub("", text)
    text = _COMMAND_NAME_RE.sub("", text)
    text = _SYSTEM_REMINDER_RE.sub("", text)
    return text.strip()


def extract_claude_prompt_text(record):
    """Returns the stripped prompt text for a genuine user prompt record, or
    None if this record isn't one (wrong type, isMeta, a tool_result turn,
    a background-agent/cross-session notice, nothing left after stripping
    <command-message>/<command-name>/<system-reminder> wrapper blocks, text
    that -- after stripping -- still looks like noise rather than something
    Oliver typed (a skill-body injection, a raw tag, a pasted image marker,
    a Caveat line), or a turn longer than CLAUDE_AUTOMATION_PROMPT_LEN --
    checked per-record, not just on a file's first turn, because at least
    one real automation pipeline on this box (a recurring WhatsApp-nudge
    watcher) appends a fresh ~30KB persona prompt to the SAME session file
    every time it runs, not only as that file's first turn)."""
    if not isinstance(record, dict) or record.get("type") != "user":
        return None
    if record.get("isMeta"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text_blocks = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if not text_blocks:
            return None
        text = "\n".join(text_blocks)
    else:
        return None
    if any(marker in text for marker in _NOISE_CONTAINS_MARKERS):
        return None
    text = strip_claude_wrappers(text)
    if not text:
        return None
    if text.startswith(_NOISE_PREFIX_MARKERS):
        return None
    if len(text) > CLAUDE_AUTOMATION_PROMPT_LEN:
        return None
    return text


def parse_claude_timestamp(ts):
    """'2026-08-26T14:54:29.026Z' (UTC ISO) -> epoch. None on anything
    unparseable -- caller skips the record rather than guessing a time."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_claude_jobs_cwd(cwd):
    """True for a .../.claude/jobs/<id>/... path -- a substring check (not
    anchored to this box's own ~/.claude/jobs) so it also matches jobs
    dirs under a different home directory, e.g. in tests. Shared by
    claude_who_from_cwd and the automation-file skip so the two can't
    silently drift onto different definitions of "a job path"."""
    if not cwd:
        return False
    norm = str(cwd).rstrip("/")
    return "/.claude/jobs/" in norm or norm.endswith("/.claude/jobs")


def claude_who_from_cwd(cwd):
    """Project name for the 'who' column: <repo> for a plain repo path,
    <repo> (the part before '/.claude/worktrees/<name>') for a worktree,
    literally 'job' for a .../.claude/jobs/<id>/... path, else the basename."""
    if not cwd:
        return "(unknown)"
    if _is_claude_jobs_cwd(cwd):
        return "job"
    norm = cwd.rstrip("/")
    marker = "/.claude/worktrees/"
    if marker in norm:
        repo = norm.split(marker, 1)[0]
        return os.path.basename(repo) or "(unknown)"
    return os.path.basename(norm) or "(unknown)"


def load_redact_config(path):
    """Returns {'private_roots': [realpath,...], 'private_keywords':
    [lowercased,...]} or None if the config is missing/unreadable/malformed.
    None means "fail closed" to the caller -- never treated as "no
    redaction needed". The config file is the only place real values for
    any private project live; this module never hardcodes one."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    roots = data.get("private_roots")
    keywords = data.get("private_keywords")
    if not isinstance(roots, list) or not isinstance(keywords, list):
        return None
    norm_roots = [
        os.path.realpath(r).rstrip("/") for r in roots if isinstance(r, str) and r
    ]
    norm_keywords = [k.lower() for k in keywords if isinstance(k, str) and k]
    if not norm_roots and not norm_keywords:
        return None
    return {"private_roots": norm_roots, "private_keywords": norm_keywords}


def _cwd_under_private_root(real_cwd, private_config):
    if not real_cwd:
        return False
    for root in private_config["private_roots"]:
        if real_cwd == root or real_cwd.startswith(root + "/"):
            return True
    return False


def _text_has_private_keyword(text, private_config):
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in private_config["private_keywords"])


CLAUDE_AUTOMATION_PROMPT_LEN = 800


CLAUDE_TEMPLATE_PREFIX_LEN = 80
CLAUDE_TEMPLATE_MIN_COUNT = 3


def _canonical_prefix(text, length=CLAUDE_TEMPLATE_PREFIX_LEN):
    """First `length` chars of the whitespace-collapsed, stripped text --
    used only to detect templated automation prompts (an automation
    pipeline sending the same opening line 3+ times in one file with a
    varying payload after it, e.g. 'Summarize this ~N-minute stretch...').
    Never computed for a redacted row (see _parse_claude_lines) -- that
    would store real private-project text in a new field, defeating the
    whole point of redaction."""
    return _collapse_whitespace(text).strip()[:length]


def _drop_templated_prompts(prompts):
    """Within one file's prompt list, if CLAUDE_TEMPLATE_MIN_COUNT or more
    non-redacted prompts share the same _canonical_prefix, drop ALL of
    them -- a repeated templated opening is automation, not something
    typed by hand each time. Redacted rows are never touched here; the
    mandatory redaction mechanism already decided their fate.

    Kept as a cheap first pass (harmless -- catches genuine within-file
    repeats), but it alone MISSES most real automation on this box: each
    `claude -p` call is its own one-shot transcript FILE with exactly one
    prompt, so no single file ever contains 3 copies of anything. The
    cross-file, per-day version below (_drop_day_templated_prompts) is
    what actually catches that pattern."""
    counts = {}
    for p in prompts:
        if not p["redacted"] and p["prefix80"] is not None:
            counts[p["prefix80"]] = counts.get(p["prefix80"], 0) + 1
    templated = {prefix for prefix, n in counts.items() if n >= CLAUDE_TEMPLATE_MIN_COUNT}
    if not templated:
        return prompts
    return [p for p in prompts if p["redacted"] or p["prefix80"] not in templated]


CLAUDE_DAY_TEMPLATE_PREFIX_LEN = 60
CLAUDE_DAY_TEMPLATE_MIN_COUNT = 3
_DIGIT_RUN_RE = re.compile(r"\d+")


def _canonical_day_prefix(text, length=CLAUDE_DAY_TEMPLATE_PREFIX_LEN):
    """Cross-file, per-day template-detection key: lowercase, collapse
    whitespace, replace every digit run with '#' (so '~1-minute' and
    '~6-minute' collapse to the same key -- payload numbers, not template
    identity), then take the first `length` chars. Never computed for a
    redacted row, same reasoning as _canonical_prefix."""
    collapsed = _collapse_whitespace(text).strip().lower()
    collapsed = _DIGIT_RUN_RE.sub("#", collapsed)
    return collapsed[:length]


def _drop_day_templated_prompts(items):
    """items: list of (day, time_str, prompt_dict). Drops every prompt
    whose day_prefix60 occurs CLAUDE_DAY_TEMPLATE_MIN_COUNT+ times within
    that SAME calendar day, across every transcript file -- this is the
    rule that actually catches one-shot `claude -p` automation (a
    classifier, an event-hook writer, our own ask-haiku period summarizer),
    where each invocation is a separate file with a single prompt."""
    counts = {}
    for day, _, p in items:
        if not p["redacted"] and p["day_prefix60"] is not None:
            key = (day, p["day_prefix60"])
            counts[key] = counts.get(key, 0) + 1
    templated = {key for key, n in counts.items() if n >= CLAUDE_DAY_TEMPLATE_MIN_COUNT}
    if not templated:
        return items
    return [
        (day, time_str, p) for day, time_str, p in items
        if p["redacted"] or (day, p["day_prefix60"]) not in templated
    ]


def _parse_claude_lines(lines, private_config, session_redacted, file_meta):
    """Parse raw jsonl lines into prompt dicts {epoch, session_id, redacted,
    who, detail, prefix80, day_prefix60}. `session_redacted` (a set) and `file_meta` (a
    dict with 'cwd_under_jobs'/'user_prompt_count') are mutated in place so
    a caller can persist/resume them across incremental reads of the same
    file -- this is what makes the per-file automation-skip (jobs cwd +
    exactly one prompt) and the per-session redaction-carry both correct
    across cache boundaries, not just within one call. Oversized individual
    prompts are already dropped per-record by extract_claude_prompt_text,
    so they never reach this counting at all (see
    CLAUDE_AUTOMATION_PROMPT_LEN). Templated-prompt dropping (see
    _drop_templated_prompts) happens later, on the full per-file list, not
    here -- it needs to see all of a file's prompts at once, which an
    incremental tail-read alone can't guarantee."""
    parsed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        text = extract_claude_prompt_text(rec)
        if text is None:
            continue
        epoch = parse_claude_timestamp(rec.get("timestamp"))
        if epoch is None:
            continue
        cwd = rec.get("cwd")
        real_cwd = os.path.realpath(cwd) if isinstance(cwd, str) and cwd else None
        session_id = rec.get("sessionId")

        if file_meta["user_prompt_count"] == 0:
            file_meta["cwd_under_jobs"] = _is_claude_jobs_cwd(real_cwd)
        file_meta["user_prompt_count"] += 1

        row_redacted = _cwd_under_private_root(real_cwd, private_config) or _text_has_private_keyword(text, private_config)
        if row_redacted and session_id:
            session_redacted.add(session_id)
        parsed.append((epoch, real_cwd, session_id, text))

    out = []
    for epoch, real_cwd, session_id, text in parsed:
        redacted = (
            _cwd_under_private_root(real_cwd, private_config)
            or _text_has_private_keyword(text, private_config)
            or (session_id is not None and session_id in session_redacted)
        )
        if redacted:
            who, detail, prefix80, day_prefix60 = REDACTED_WHO, REDACTED_DETAIL, None, None
        else:
            who = claude_who_from_cwd(real_cwd)
            detail = sanitize_detail(text)
            prefix80 = _canonical_prefix(text)
            day_prefix60 = _canonical_day_prefix(text)
        out.append({
            "epoch": epoch, "session_id": session_id, "redacted": redacted,
            "who": who, "detail": detail, "prefix80": prefix80,
            "day_prefix60": day_prefix60,
        })
    return out


def _claude_cache_key(path):
    """Opaque key for offset_cache -- NEVER the raw path. A transcript's
    path encodes its project directory name (Claude Code flattens cwd into
    the filename), so for the private project that path literally contains
    the private keyword/root basename -- using it as a JSON dict key would
    leak that into the cache file on disk even though every actual prompt
    row is correctly redacted. Hashed instead, same as period_hash."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def read_claude_file_cached(path, private_config, offset_cache):
    """Returns (prompts, file_meta) for one transcript file, using
    offset_cache to avoid re-reading unchanged bytes on every run (a
    transcript can be tens of MB). Mutates offset_cache[_claude_cache_key(path)]
    in place -- never offset_cache[path] itself (see _claude_cache_key).

    PRIVACY INVARIANT: offset_cache never holds real text for a row once
    that row (or any row sharing its sessionId, anywhere earlier in this
    same file) is redacted -- a redacted row's cached 'who'/'detail' are
    always the literal REDACTED_WHO/REDACTED_DETAIL, exactly like a
    non-cached row. If a newly-read tail reveals a session is redacted, the
    fix-up loop below overwrites that session's EARLIER cached rows too
    (discarding whatever real text they held) before returning, so the
    cache file on disk is never a wider disclosure than the current run's
    own redaction knowledge -- this is the same "whole file redacted, no
    trace" guarantee claude_events always gave, just computed
    incrementally instead of by re-reading from byte 0 every time.
    """
    try:
        st = os.stat(path)
    except OSError:
        return [], {"cwd_under_jobs": False, "user_prompt_count": 0}
    size, mtime = st.st_size, st.st_mtime
    key = _claude_cache_key(path)
    entry = offset_cache.get(key)

    if entry and entry.get("size") == size and entry.get("mtime") == mtime:
        return entry["prompts"], entry["file_meta"]  # byte-identical -- fully reuse, no read at all

    if entry and isinstance(entry.get("offset"), int) and size >= entry["offset"]:
        # Append-only growth: seek past what we've already parsed and read
        # only the new tail.
        session_redacted = set(entry.get("session_redacted", []))
        file_meta = dict(entry["file_meta"])
        try:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(entry["offset"])
                new_lines = f.readlines()
                new_offset = f.tell()
        except OSError:
            return entry["prompts"], entry["file_meta"]
        new_prompts = _parse_claude_lines(new_lines, private_config, session_redacted, file_meta)

        cached_prompts = entry["prompts"]
        newly_redacted_sessions = {
            p["session_id"] for p in new_prompts if p["redacted"] and p["session_id"] is not None
        }
        if newly_redacted_sessions:
            for p in cached_prompts:
                if p["session_id"] in newly_redacted_sessions and not p["redacted"]:
                    p["redacted"] = True
                    p["who"], p["detail"] = REDACTED_WHO, REDACTED_DETAIL

        all_prompts = cached_prompts + new_prompts
        offset_cache[key] = {
            "size": size, "mtime": mtime, "offset": new_offset,
            "session_redacted": sorted(session_redacted),
            "file_meta": file_meta,
            "prompts": all_prompts,
        }
        return all_prompts, file_meta

    # New file, or it shrank/was rewritten (not append-only) -- full re-parse.
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            new_offset = f.tell()
    except OSError:
        return [], {"cwd_under_jobs": False, "user_prompt_count": 0}
    session_redacted = set()
    file_meta = {"cwd_under_jobs": False, "user_prompt_count": 0}
    all_prompts = _parse_claude_lines(lines, private_config, session_redacted, file_meta)
    offset_cache[key] = {
        "size": size, "mtime": mtime, "offset": new_offset,
        "session_redacted": sorted(session_redacted),
        "file_meta": file_meta,
        "prompts": all_prompts,
    }
    return all_prompts, file_meta


# Bump whenever extract_claude_prompt_text / the noise filters / who
# derivation change what a cached prompt SHOULD look like -- otherwise an
# unchanged (same size+mtime) transcript keeps returning parse results
# computed under the OLD rules forever, since the whole point of the fast
# path is to never re-derive them. A version mismatch invalidates the
# entire cache (forces a full re-parse of every file under the new rules)
# rather than trying to reconcile old and new entries. Learned the hard
# way: v1->v2 bump here is what it took to clear a stale, pre-noise-filter
# cached automation prompt that kept reappearing after the filter fix.
CLAUDE_OFFSET_CACHE_VERSION = 4  # v4: day_prefix60 field + cross-file per-day templated-prompt drop


def load_claude_offset_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("__version__") == CLAUDE_OFFSET_CACHE_VERSION:
            data = dict(data)
            data.pop("__version__", None)
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_claude_offset_cache(path, offset_cache):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".tmp-claude-offsets-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            to_write = dict(offset_cache)
            to_write["__version__"] = CLAUDE_OFFSET_CACHE_VERSION
            json.dump(to_write, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def claude_events(projects_globs, days_requested, now_epoch, private_config, offset_cache):
    """private_config must be a validated config from load_redact_config --
    the caller is responsible for failing the whole source closed (see
    gather()) when it's None; this function assumes redaction is active.
    offset_cache: mutable dict from load_claude_offset_cache, mutated by
    read_claude_file_cached for each file touched."""
    cutoff = now_epoch - (days_requested + 1) * 86400
    kept = []  # (day, time_str, prompt_dict) across ALL files, pre-day-template-filter
    for path in sorted(p for g in projects_globs for p in glob.glob(g)):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            continue

        prompts, file_meta = read_claude_file_cached(path, private_config, offset_cache)

        # Whole-file automation skip: a job runner with only ever one
        # "prompt" (its task description) is not something Oliver typed.
        # (Oversized individual prompts -- persona/system turns -- are
        # already dropped per-record in extract_claude_prompt_text, not
        # here, because at least one automation pipeline on this box
        # appends a fresh long persona turn repeatedly to the same file
        # rather than only as its first turn.) Re-checked every run from
        # the live count so a file that later gains a second real prompt
        # stops being treated as automation.
        if file_meta["cwd_under_jobs"] and file_meta["user_prompt_count"] == 1:
            continue

        # Per-file templated-prompt skip: harmless first pass, catches a
        # genuine within-file repeat. Recomputed fresh every run from the
        # full (cached + any newly-read) per-file prompt list, so a prefix
        # that only reaches 3 occurrences in this run's newly-read tail
        # still correctly drops earlier same-prefix rows from prior runs.
        prompts = _drop_templated_prompts(prompts)

        for p in prompts:
            day, time_str = local_day_time_from_epoch(p["epoch"])
            kept.append((day, time_str, p))

    # Cross-file, per-day templated-prompt skip: this is the rule that
    # actually catches one-shot `claude -p` automation (each invocation is
    # its own file with a single prompt, so the per-file rule above never
    # sees the repetition). Must run AFTER collecting every file's survivors
    # for the day, not per-file, and is recomputed fresh every run so a
    # prefix crossing the 3-occurrence threshold in this run's newly-read
    # data still drops matching prompts collected from OTHER files too.
    kept = _drop_day_templated_prompts(kept)

    events = [
        Event(p["epoch"], day, time_str, "claude", "prompt", p["who"], p["detail"])
        for day, time_str, p in kept
    ]
    return events


# --------------------------------------------------------------------------
# Source 6: iPhone call history, exported by the Mac to
# ~/inbox/imessage-sync/calls-*.json (one JSON array per file). No files
# exist yet on this box -- zero files must mean zero rows, no error, not a
# failed source.
# --------------------------------------------------------------------------

APPLE_CALL_PROVIDERS = ("com.apple.Telephony", "com.apple.FaceTime")
FACETIME_CALL_TYPES = (8, 16)


def _imessage_chat_names(dir_path):
    """address -> chat_name, best-effort, scanned from the same iMessage
    snapshot files imessage_events reads. Last snapshot with a non-empty
    chat_name for an address wins; exact dedup precision doesn't matter
    here the way it does for imessage_events itself -- this is only a
    fallback name-resolution index for phone calls."""
    names = {}
    for path in sorted(glob.glob(os.path.join(dir_path, "messages-*.json"))):
        try:
            if os.path.getsize(path) == 0:
                continue
            with open(path, "r") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue
        except (OSError, ValueError):
            continue
        for rec in data:
            chat_name = rec.get("chat_name")
            if not chat_name:
                continue
            for key in (rec.get("sender"), rec.get("chat_id")):
                if key:
                    names[key] = chat_name
    return names


def resolve_phone_call_who(address, contacts, imessage_names):
    """address -> display name: WhatsApp contacts (by bare-digit phone
    number) first, same full_name > first_name > push_name preference
    already used for WhatsApp LID resolution; then any iMessage chat_name
    seen for that exact address; else the raw address."""
    if not address:
        return "(unknown)"
    digits = re.sub(r"\D", "", address)
    if digits:
        contact = contacts.get(f"{digits}@s.whatsapp.net")
        if contact:
            first_name, full_name, push_name = contact
            if full_name:
                return full_name
            if first_name:
                return first_name
            if push_name:
                return push_name
    if address in imessage_names:
        return imessage_names[address]
    return address


def format_phone_call_detail(originated, answered, duration_s, call_type):
    """'answered · 12m' (incoming, answered), 'outgoing · 3m' (outgoing,
    answered), or 'missed' (not answered, either direction -- an
    unanswered outgoing call and a declined/missed incoming call are
    indistinguishable in this data, so both render as 'missed'). '
    (FaceTime)' appended for call_type 8/16."""
    if answered:
        base = "outgoing" if originated else "answered"
        secs = int(duration_s or 0)
        length = f"{secs}s" if secs < 60 else f"{secs // 60}m"
        detail = f"{base} · {length}"
    else:
        detail = "missed"
    if call_type in FACETIME_CALL_TYPES:
        detail += " (FaceTime)"
    return detail


def phone_call_events(calls_glob, contacts, imessage_names):
    records = {}  # pk -> record; later (newer, sorted-last) files win
    for path in sorted(glob.glob(calls_glob)):
        try:
            if os.path.getsize(path) == 0:
                continue
            with open(path, "r") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue
        except (OSError, ValueError):
            continue
        for rec in data:
            pk = rec.get("pk")
            if pk is None:
                continue
            records[pk] = rec

    events = []
    for rec in records.values():
        # Third-party VoIP calls (WhatsApp etc.) can land in the iPhone log via
        # CallKit; those already arrive from their own source, so only Apple's
        # own providers count here.
        if rec.get("provider") not in APPLE_CALL_PROVIDERS:
            continue
        ts = rec.get("ts")
        try:
            dt_naive = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        # ts has no offset -- already local wall-clock
        # time, same handling as imessage_events.
        dt = dt_naive.replace(tzinfo=NY_TZ)
        epoch = dt.timestamp()
        day = dt.date()
        time_str = dt.strftime("%H:%M")

        originated = rec.get("originated") == 1
        answered = rec.get("answered") == 1
        duration_s = rec.get("duration_s") or 0
        call_type = rec.get("call_type")
        address = rec.get("address")

        direction = "outgoing" if originated else "incoming"
        who = resolve_phone_call_who(address, contacts, imessage_names)
        detail = format_phone_call_detail(originated, answered, duration_s, call_type)

        events.append(Event(epoch, day, time_str, "phone", direction, who, sanitize_detail(detail)))
    return events


# --------------------------------------------------------------------------
# Periods: cluster same-day events into gap-bounded stretches and summarize
# each one (LLM first, cached by content; deterministic heuristic fallback).
# --------------------------------------------------------------------------


def cluster_periods(events):
    """Cluster events (any order, one day's worth) into periods where each
    consecutive pair is <=15min apart (900s inclusive -- exactly 15min does
    NOT split). Returns a list of periods, each a time-sorted list of Event."""
    if not events:
        return []
    ev_sorted = sorted(events, key=lambda e: (e.epoch, e.source, e.who, e.detail))
    periods = [[ev_sorted[0]]]
    for e in ev_sorted[1:]:
        if e.epoch - periods[-1][-1].epoch <= PERIOD_GAP_SECONDS:
            periods[-1].append(e)
        else:
            periods.append([e])
    return periods


def split_period_by_length(period):
    """Split a period so no piece spans more than 45 minutes: cut at the
    largest internal gap and recurse on both sides (each side is a strict,
    non-empty subset, so this always terminates). Falls back to a hard cut
    at 45min from the piece's first event if a gap-split somehow leaves a
    piece unresolved -- not reachable from cluster_periods output (whose
    internal gaps are all <=15min, so any 3+ event piece keeps shrinking
    below the cap), but kept as a defensive boundary. Never drops or
    duplicates an event; pieces stay contiguous and in order."""
    if len(period) <= 1:
        return [period] if period else []
    duration = period[-1].epoch - period[0].epoch
    if duration <= PERIOD_MAX_SECONDS:
        return [period]

    best_i, best_gap = 0, -1
    for i in range(len(period) - 1):
        gap = period[i + 1].epoch - period[i].epoch
        if gap > best_gap:
            best_gap, best_i = gap, i
    left, right = period[: best_i + 1], period[best_i + 1:]

    if not left or not right:
        boundary = period[0].epoch + PERIOD_MAX_SECONDS
        left = [e for e in period if e.epoch <= boundary] or [period[0]]
        right = period[len(left):]
        if not right:
            return [period]

    return split_period_by_length(left) + split_period_by_length(right)


def period_row_key(e):
    """Canonical identity of an event for cache-hashing purposes -- content
    only, deliberately excluding time_str/epoch so a period whose rows are
    unchanged is never re-summarized even if re-clustering shifts its
    start/end."""
    return [e.source, e.direction, e.who, e.detail]


def period_hash(period):
    rows = [period_row_key(e) for e in period]
    payload = json.dumps(rows, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_period_prompt(period):
    duration_min = max(1, int(round((period[-1].epoch - period[0].epoch) / 60.0)))
    lines = [
        f"Summarize this ~{duration_min}-minute stretch of personal activity in "
        "one line under 100 characters, plain, no preamble. Mention who was "
        "messaged/called and what the browsing was about; skip noise like reloads."
    ]
    for e in period:
        lines.append(f"{e.source} | {e.direction} | {e.who} | {e.detail}")
    return "\n".join(lines)


def extract_chrome_url(detail):
    """Best-effort recovery of the url from a chrome Event.detail string,
    which is '{title} — {url}' or '{title} — {url} (×N)'."""
    if " — " not in detail:
        return None
    _, rest = detail.split(" — ", 1)
    idx = rest.rfind(" (×")
    if idx != -1 and rest.endswith(")"):
        rest = rest[:idx]
    return rest.strip()


def domain_label(url):
    """Best-effort short domain label, or None if url has no usable netloc --
    never a placeholder like '?', and never a bare scheme fragment. A url can
    arrive truncated (sanitize_detail's 80-char cap can cut mid-url, e.g.
    down to just 'https:'); urlparse("//" + "https:").netloc misreads that
    as netloc="https:", so any netloc with no dot or ending in ':' is
    rejected as not a real host rather than trusted."""
    if not url:
        return None
    netloc = urlparse(url).netloc.lower()
    if not netloc:
        netloc = urlparse("//" + url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if not netloc or netloc.endswith(":") or "." not in netloc:
        return None
    parts = [p for p in netloc.split(".") if p]
    if not parts:
        return None
    label = parts[-2] if len(parts) >= 2 else parts[0]
    return label.capitalize() if label else None


_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:(//)?$")


def looks_like_url_fragment(s):
    """True if s is a bare URL/scheme remnant ('https:', 'https://',
    'chrome://') rather than real page text -- guards the title fallback
    the same way domain_label guards the netloc, so neither path can hand
    back a scheme fragment as a label."""
    if not s:
        return False
    return bool(_URL_SCHEME_RE.match(s)) or s.startswith("http://") or s.startswith("https://")


def chrome_label(detail):
    """Label for a chrome heuristic bucket: prefer the URL's domain, fall
    back to the page title (text before ' — ') when the url can't be
    parsed (e.g. an empty or truncated-to-scheme url), and return None --
    never '?' and never a URL/scheme fragment -- if neither yields a real
    label, so the caller drops it from the top-3 instead."""
    url = extract_chrome_url(detail)
    label = domain_label(url)
    if label:
        return label
    title = detail.split(" — ", 1)[0].strip() if " — " in detail else detail.strip()
    if not title or looks_like_url_fragment(title):
        return None
    return title[:20]


SOURCE_LABEL = {"whatsapp": "WhatsApp", "imessage": "iMessage", "call": "Call", "claude": "Claude", "phone": "Phone"}


def heuristic_period_summary(period):
    """Deterministic fallback: top-3 who-by-count for messages/calls, then
    top-3 domains for chrome, e.g. 'WhatsApp Esme ×12 · iMessage Dad ×2 ·
    8 pages: Venmo, Slack'."""
    msg_counts = {}
    chrome_domains = {}
    chrome_total = 0
    for e in period:
        if e.source in SOURCE_LABEL:
            key = (SOURCE_LABEL[e.source], e.who or "(unknown)")
            msg_counts[key] = msg_counts.get(key, 0) + 1
        elif e.source == "chrome":
            chrome_total += 1
            label = chrome_label(e.detail)
            if label:
                chrome_domains[label] = chrome_domains.get(label, 0) + 1

    parts = []
    top_msgs = sorted(msg_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    for (label, who), cnt in top_msgs:
        parts.append(f"{label} {who} ×{cnt}")
    if chrome_total:
        top_domains = sorted(chrome_domains.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        dom_names = ", ".join(d for d, _ in top_domains)
        plural = "s" if chrome_total != 1 else ""
        page_part = f"{chrome_total} page{plural}"
        parts.append(f"{page_part}: {dom_names}" if dom_names else page_part)
    if not parts:
        n = len(period)
        parts.append(f"{n} event{'s' if n != 1 else ''}")
    return " · ".join(parts)


def default_llm_call(prompt):
    result = subprocess.run(
        [ASK_HAIKU, prompt],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=35,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ask-haiku.sh exit {result.returncode}: {stderr}")
    out = result.stdout.decode("utf-8", "replace").strip()
    if not out:
        raise RuntimeError("ask-haiku.sh returned empty output")
    return out


def period_is_open(period, now_epoch):
    """A period is 'open' while its last event is within 20min of the run's
    reference time -- it may still grow, so summarizing/caching it now would
    just burn a call on content that's about to change. Re-checked every
    run; once the gap exceeds 20min the period is closed and summarized
    (and cached) normally."""
    return (now_epoch - period[-1].epoch) <= PERIOD_OPEN_SECONDS


def summarize_period(period, cache, llm_call, budget, cap, now_epoch):
    """Returns (summary_text, is_llm_summary). Mutates `cache` (only on a
    successful fresh call) and `budget['used']` (on every attempt, success
    or failure -- an attempt consumes the per-run cap either way). An open
    period (see period_is_open) always gets the heuristic, uncached, with no
    LLM attempt -- it hasn't finished happening yet."""
    if period_is_open(period, now_epoch):
        return sanitize_period_summary(heuristic_period_summary(period)), False
    h = period_hash(period)
    if h in cache:
        return cache[h], True
    if budget["used"] < cap:
        budget["used"] += 1
        try:
            raw = llm_call(build_period_prompt(period))
            text = sanitize_period_summary(raw)
            if not text:
                raise ValueError("empty summary after sanitize")
            cache[h] = text
            return text, True
        except Exception:
            pass
    return sanitize_period_summary(heuristic_period_summary(period)), False


def load_summary_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_summary_cache(path, cache):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".tmp-cache-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

COUNT_KEYS = [
    "whatsapp_sent",
    "whatsapp_assistant",
    "whatsapp_received",
    "whatsapp_calls",
    "imessage_sent",
    "imessage_received",
    "chrome_windows",
    "chrome_synced",
    "claude_prompts",
    "phone_calls",
    "periods",
    "summaries_llm",
]


def build_markdown(day, generated_at_str, events, errors, cache, llm_call, budget, cap, now_epoch):
    counts = {k: 0 for k in COUNT_KEYS}
    last_seen = {}

    def bump_last_seen(key, time_str):
        if key not in last_seen or time_str > last_seen[key]:
            last_seen[key] = time_str

    for e in events:
        if e.source == "whatsapp":
            if e.direction == "sent":
                counts["whatsapp_sent"] += 1
            elif e.direction == "sent (assistant)":
                counts["whatsapp_assistant"] += 1
            elif e.direction == "received":
                counts["whatsapp_received"] += 1
            bump_last_seen("whatsapp", e.time_str)
        elif e.source == "call":
            counts["whatsapp_calls"] += 1
            bump_last_seen("calls", e.time_str)
        elif e.source == "imessage":
            if e.direction == "sent":
                counts["imessage_sent"] += 1
            elif e.direction == "received":
                counts["imessage_received"] += 1
            bump_last_seen("imessage", e.time_str)
        elif e.source == "chrome":
            if e.who == "Windows":
                counts["chrome_windows"] += 1
            elif e.who == "synced":
                counts["chrome_synced"] += 1
            bump_last_seen("chrome", e.time_str)
        elif e.source == "claude":
            counts["claude_prompts"] += 1
            bump_last_seen("claude", e.time_str)
        elif e.source == "phone":
            counts["phone_calls"] += 1
            bump_last_seen("phone", e.time_str)

    raw_periods = cluster_periods(events)
    periods = []
    for p in raw_periods:
        periods.extend(split_period_by_length(p))
    start_used = budget["used"]
    summaries_llm = 0
    period_summaries = []
    for period in periods:
        summary, is_llm = summarize_period(period, cache, llm_call, budget, cap, now_epoch)
        period_summaries.append(summary)
        if is_llm:
            summaries_llm += 1
    llm_calls_for_day = budget["used"] - start_used
    counts["periods"] = len(periods)
    counts["summaries_llm"] = summaries_llm

    lines = []
    lines.append("---")
    lines.append(f"date: {day.isoformat()}")
    lines.append(f"generated_at: {generated_at_str}")
    lines.append(f"timezone: {TZ_NAME}")
    counts_str = ", ".join(f"{k}: {counts[k]}" for k in COUNT_KEYS)
    lines.append(f"counts: {{{counts_str}}}")
    ls_order = ["whatsapp", "imessage", "chrome", "calls", "claude", "phone"]
    ls_str = ", ".join(
        f"{k}: {yaml_quote(last_seen[k])}" for k in ls_order if k in last_seen
    )
    lines.append(f"last_seen: {{{ls_str}}}")
    err_str = ", ".join(yaml_quote(e) for e in errors)
    lines.append(f"errors: [{err_str}]")
    lines.append(f"llm_calls: {llm_calls_for_day}")
    lines.append("---")
    lines.append("")

    if not events:
        lines.append("_no activity_")
    else:
        lines.append("| time | source | direction | who | detail |")
        lines.append("|---|---|---|---|---|")
        for e in sorted(events, key=lambda x: (x.epoch, x.source, x.who, x.detail)):
            who = sanitize_who(e.who)
            lines.append(f"| {e.time_str} | {e.source} | {e.direction} | {who} | {e.detail} |")

    if periods:
        lines.append("")
        lines.append("## Periods")
        lines.append("| start | end | summary |")
        lines.append("|---|---|---|")
        for period, summary in zip(periods, period_summaries):
            lines.append(f"| {period[0].time_str} | {period[-1].time_str} | {summary} |")
    lines.append("")
    return "\n".join(lines)


def write_day_file(vault_dir, day, content):
    os.makedirs(vault_dir, exist_ok=True)
    target = os.path.join(vault_dir, f"{day.isoformat()}.md")
    fd, tmp_path = tempfile.mkstemp(
        dir=vault_dir, prefix=".tmp-activity-", suffix=".md"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


TOTAL_SOURCES = 6  # whatsapp messages, calls, imessage, chrome, claude, phone


def gather(paths, days, now_epoch):
    """Read all sources. Returns (events_by_day, errors, failed_count)."""
    errors = []
    all_events = []
    failed = 0

    try:
        lid_map = load_lid_map_safe(paths["whatsapp_account_db"])
        contacts = load_contacts_safe(paths["whatsapp_account_db"])
        all_events.extend(whatsapp_events(paths["whatsapp_db"], lid_map, contacts))
    except Exception as e:
        errors.append(f"whatsapp: {e}")
        failed += 1

    try:
        self_ids = resolve_self_ids(paths["whatsapp_account_db"])
        chat_names = load_chat_names_safe(paths["whatsapp_db"])
        lid_map = load_lid_map_safe(paths["whatsapp_account_db"])
        contacts = load_contacts_safe(paths["whatsapp_account_db"])
        all_events.extend(
            call_events(paths["calls_jsonl"], self_ids, chat_names, lid_map, contacts)
        )
    except Exception as e:
        errors.append(f"calls: {e}")
        failed += 1

    try:
        events, notes = imessage_events(paths["imessage_dir"])
        all_events.extend(events)
        errors.extend(notes)
    except Exception as e:
        errors.append(f"imessage: {e}")
        failed += 1

    try:
        all_events.extend(chrome_events(paths["chrome_history"]))
    except Exception as e:
        errors.append(f"chrome: {e}")
        failed += 1

    # claude: fails CLOSED (zero rows) whenever the redaction config isn't
    # available -- never falls back to "no redaction needed". `paths["redact"]`
    # lets tests inject a config directly without touching the real file.
    # `paths["claude_offset_cache"]` likewise lets tests inject an
    # in-memory offset-cache dict instead of touching the real cache file.
    try:
        redact = paths.get("redact")
        if redact is None:
            redact = load_redact_config(paths.get("redact_path", REDACT_CONFIG_PATH))
        if redact is None:
            errors.append("claude: redaction config unavailable")
            failed += 1
        else:
            injected_offset_cache = paths.get("claude_offset_cache")
            persist_offset_cache = injected_offset_cache is None
            offset_cache_path = paths.get("claude_offset_cache_path", CLAUDE_OFFSET_CACHE_PATH)
            offset_cache = (
                injected_offset_cache if injected_offset_cache is not None
                else load_claude_offset_cache(offset_cache_path)
            )
            all_events.extend(
                claude_events(
                    paths.get("claude_projects_globs", CLAUDE_PROJECTS_GLOBS),
                    len(days), now_epoch, redact, offset_cache,
                )
            )
            if persist_offset_cache:
                save_claude_offset_cache(offset_cache_path, offset_cache)
    except Exception:
        errors.append("claude: source unavailable")
        failed += 1

    # phone: zero files (none exist yet on this box) must be zero rows, not
    # an error -- phone_call_events already returns [] for an empty glob,
    # so this only fails on a genuine read/parse problem.
    try:
        contacts = load_contacts_safe(paths["whatsapp_account_db"])
        imessage_names = _imessage_chat_names(paths["imessage_dir"])
        all_events.extend(
            phone_call_events(paths.get("phone_calls_glob", PHONE_CALLS_GLOB), contacts, imessage_names)
        )
    except Exception as e:
        errors.append(f"phone: {e}")
        failed += 1

    events_by_day = {}
    for e in all_events:
        events_by_day.setdefault(e.day, []).append(e)

    return events_by_day, errors, failed


def run(paths, days, now_epoch):
    events_by_day, errors, failed = gather(paths, days, now_epoch)
    generated_at_str = (
        datetime.fromtimestamp(now_epoch, tz=NY_TZ).strftime("%Y-%m-%dT%H:%M:%S")
        + utc_offset_str(now_epoch)
    )
    all_failed = failed == TOTAL_SOURCES
    if not all_failed:
        cache_path = paths.get("cache_path", CACHE_PATH)
        llm_call = paths.get("llm_call") or default_llm_call
        cap = paths.get("llm_call_cap", LLM_CALL_CAP)
        cache = load_summary_cache(cache_path)
        budget = {"used": 0}
        for day in days:
            content = build_markdown(
                day, generated_at_str, events_by_day.get(day, []), errors,
                cache, llm_call, budget, cap, now_epoch,
            )
            write_day_file(paths["vault_dir"], day, content)
        save_summary_cache(cache_path, cache)
    else:
        print(
            "activity-export: all sources failed: " + "; ".join(errors),
            file=sys.stderr,
        )
    return 1 if all_failed else 0


def default_paths():
    return {
        "vault_dir": VAULT_DIR,
        "whatsapp_db": WHATSAPP_DB,
        "whatsapp_account_db": WHATSAPP_ACCOUNT_DB,
        "calls_jsonl": CALLS_JSONL,
        "imessage_dir": IMESSAGE_DIR,
        "chrome_history": CHROME_HISTORY,
        "cache_path": CACHE_PATH,
        "claude_projects_globs": CLAUDE_PROJECTS_GLOBS,
        "redact_path": REDACT_CONFIG_PATH,
        "claude_offset_cache_path": CLAUDE_OFFSET_CACHE_PATH,
        "phone_calls_glob": PHONE_CALLS_GLOB,
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    bf = sub.add_parser("backfill")
    bf.add_argument("n", type=int)
    args = parser.parse_args(argv)

    now_epoch = time.time()
    today, _ = local_day_time_from_epoch(now_epoch)
    if args.cmd == "backfill":
        days = [today - timedelta(days=i) for i in range(max(args.n, 0))]
    else:
        days = [today, today - timedelta(days=1)]

    return run(default_paths(), days, now_epoch)


if __name__ == "__main__":
    sys.exit(main())
