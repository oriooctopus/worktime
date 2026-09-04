"""Tests for activity-export.py. Run with:
    python3 -m pytest test_activity_export.py -v
    TZ=UTC python3 -m pytest test_activity_export.py -v

All fixtures are built fresh per test (tiny sqlite DBs, jsonl, json snapshots,
a fake Chrome History) -- nothing here touches the real WhatsApp/iMessage/
Chrome data on this box. The implementation uses aware zoneinfo datetimes
throughout and must produce identical results regardless of the process's
own OS timezone, which is why the whole suite is run under both the box TZ
and TZ=UTC.
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "activity-export.py")
spec = importlib.util.spec_from_file_location("activity_export", MODULE_PATH)
ae = importlib.util.module_from_spec(spec)
sys.modules["activity_export"] = ae
spec.loader.exec_module(ae)


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------


def make_whatsapp_db(path, chats=(), messages=()):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, "
        "timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT, filename TEXT, "
        "PRIMARY KEY (id, chat_jid))"
    )
    for jid, name in chats:
        conn.execute("INSERT INTO chats (jid, name) VALUES (?, ?)", (jid, name))
    for i, m in enumerate(messages):
        chat_jid, sender, content, ts, is_from_me, media_type = m
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, "
            "is_from_me, media_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"MSG{i}", chat_jid, sender, content, ts, is_from_me, media_type),
        )
    conn.commit()
    conn.close()


def make_whatsapp_account_db(path, device=None, lid_map=(), contacts=()):
    """device: (jid, lid) tuple for whatsmeow_device.
    lid_map: [(lid, pn), ...] for whatsmeow_lid_map.
    contacts: [(their_jid, first_name, full_name, push_name), ...] for whatsmeow_contacts.
    """
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, lid TEXT)")
    conn.execute(
        "CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE whatsmeow_contacts (their_jid TEXT PRIMARY KEY, first_name TEXT, "
        "full_name TEXT, push_name TEXT)"
    )
    if device:
        jid, lid = device
        conn.execute("INSERT INTO whatsmeow_device (jid, lid) VALUES (?, ?)", (jid, lid))
    for lid, pn in lid_map:
        conn.execute("INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)", (lid, pn))
    for their_jid, first_name, full_name, push_name in contacts:
        conn.execute(
            "INSERT INTO whatsmeow_contacts (their_jid, first_name, full_name, push_name) "
            "VALUES (?, ?, ?, ?)",
            (their_jid, first_name, full_name, push_name),
        )
    conn.commit()
    conn.close()


def make_calls_jsonl(path, lines):
    with open(path, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")


def call_line(call_id, kind, ts, call_creator, frm):
    return {
        "call_creator": call_creator,
        "call_id": call_id,
        "from": frm,
        "group_jid": "",
        "kind": kind,
        "ts": ts,
    }


def make_imessage_snapshot(dir_path, filename, records):
    with open(os.path.join(dir_path, filename), "w") as f:
        json.dump(records, f)


# CHAIN_START|CHAIN_END -- a navigation somebody made, which is what every
# fixture visit is unless it says otherwise.
PLAIN_VISIT = 0x30000000


def make_chrome_history(path, visits):
    """visits: list of (visit_time_webkit_int, url, title, originator_cache_guid)

    A visit may carry two more fields, (from_visit, transition), for the tests
    that care which visits a person actually made; they default to a plain
    navigation with no referring visit.
    """
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, "
        "title LONGVARCHAR, visit_count INTEGER DEFAULT 0, typed_count INTEGER "
        "DEFAULT 0, last_visit_time INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE visits(id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER, "
        "visit_time INTEGER, from_visit INTEGER DEFAULT 0, "
        "transition INTEGER DEFAULT 0, originator_cache_guid TEXT)"
    )
    for i, visit in enumerate(visits):
        vt, url, title, guid = visit[:4]
        from_visit, transition = (visit[4:] or (0, PLAIN_VISIT))
        conn.execute("INSERT INTO urls (id, url, title) VALUES (?, ?, ?)", (i + 1, url, title))
        conn.execute(
            "INSERT INTO visits (url, visit_time, from_visit, transition, "
            "originator_cache_guid) VALUES (?, ?, ?, ?, ?)",
            (i + 1, vt, from_visit, transition, guid),
        )
    conn.commit()
    conn.close()


def epoch_to_webkit(epoch):
    return int(round((epoch + 11644473600) * 1e6))


def local_epoch(y, mo, d, h, mi, s, offset_hours):
    """Epoch for a wall-clock time with an explicit UTC offset (no OS-TZ dependency)."""
    dt = datetime(y, mo, d, h, mi, s)
    tz = timezone(timedelta(hours=offset_hours))
    return dt.replace(tzinfo=tz).timestamp()


def _mock_llm_call(prompt):
    """Default test double for the LLM summarizer -- deterministic, no
    subprocess, never touches ask-haiku.sh. Tests that care about call
    counts/content wrap or replace this via paths['llm_call']."""
    return "mock summary"


@pytest.fixture
def paths(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    wa_db = tmp_path / "messages.db"
    wa_account_db = tmp_path / "whatsapp.db"
    calls_jsonl = tmp_path / "calls.jsonl"
    imessage_dir = tmp_path / "imessage"
    imessage_dir.mkdir()
    chrome_history = tmp_path / "History"
    cache_path = tmp_path / "summaries.json"
    claude_dir = tmp_path / "claude_projects"
    claude_dir.mkdir()

    make_whatsapp_db(str(wa_db))
    make_whatsapp_account_db(str(wa_account_db), device=("17205846358", "2152181272617"))
    make_calls_jsonl(str(calls_jsonl), [])
    make_chrome_history(str(chrome_history), [])

    return {
        "vault_dir": str(vault_dir),
        "whatsapp_db": str(wa_db),
        "whatsapp_account_db": str(wa_account_db),
        "calls_jsonl": str(calls_jsonl),
        "imessage_dir": str(imessage_dir),
        "chrome_history": str(chrome_history),
        "cache_path": str(cache_path),
        "llm_call": _mock_llm_call,
        # claude source: points at an empty tmp dir and a deliberately
        # nonexistent redact config by default, so every test fails the
        # claude source closed (0 rows) unless it explicitly injects
        # paths["redact"] or writes its own jsonl fixtures -- tests must
        # NEVER touch the real ~/.claude/projects or ~/.config/activity-export.
        "claude_dir": str(claude_dir),
        "claude_projects_globs": [str(claude_dir / "*" / "*.jsonl")],
        "redact_path": str(tmp_path / "redact-missing.json"),
        # Always a per-test tmp path, never the real cache file, whether or
        # not a given test also injects paths["claude_offset_cache"] directly.
        "claude_offset_cache_path": str(tmp_path / "claude-offsets.json"),
        # phone calls land in the same directory as iMessage snapshots in
        # production (~/inbox/imessage-sync/); mirrored here so a test that
        # writes both calls-*.json and messages-*.json fixtures into
        # paths["imessage_dir"] exercises the real cross-source name lookup.
        "phone_calls_glob": str(imessage_dir / "calls-*.json"),
    }


def read_day(paths, day):
    p = os.path.join(paths["vault_dir"], f"{day.isoformat()}.md")
    with open(p) as f:
        return f.read()


def events_only(content):
    """Content up to (not including) the Periods section -- so a test
    scanning the events table by time-prefix doesn't also match a Periods
    row that happens to start at the same HH:MM."""
    return content.split("\n## Periods", 1)[0]


def split_row(line):
    """Split a markdown table row on unescaped '|' (matches the export's \\| escaping)."""
    parts = re.split(r"(?<!\\)\|", line)
    return [p.strip() for p in parts if p != ""]


# --------------------------------------------------------------------------
# Local-day bucketing (not UTC)
# --------------------------------------------------------------------------


def test_late_local_events_stay_on_local_day_not_utc(paths):
    # 22:30 EDT on 08-25 is 02:30 UTC on 08-26 -- must land on 08-25, not 08-26.
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[
            ("111@s.whatsapp.net", "111", "late night msg", "2026-08-25 22:30:00-04:00", 0, None)
        ],
    )
    make_chrome_history(
        str(paths["chrome_history"]),
        [(epoch_to_webkit(local_epoch(2026, 8, 25, 21, 15, 0, -4)), "https://x.com", "X", "")],
    )
    days = [date(2026, 8, 25), date(2026, 8, 26)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 26, 12, 0, 0, -4))
    assert rc == 0
    d25 = read_day(paths, date(2026, 8, 25))
    d26 = read_day(paths, date(2026, 8, 26))
    assert "late night msg" in d25
    assert "22:30" in d25
    assert "late night msg" not in d26
    assert "21:15" in d25
    assert "21:15" not in d26


def test_adjacent_day_boundary_exact_midnight(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[
            ("111@s.whatsapp.net", "111", "last of the day", "2026-08-20 23:59:59-04:00", 0, None),
            ("111@s.whatsapp.net", "111", "first of the day", "2026-08-21 00:00:00-04:00", 0, None),
        ],
    )
    days = [date(2026, 8, 20), date(2026, 8, 21)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 21, 12, 0, 0, -4))
    assert rc == 0
    d20 = read_day(paths, date(2026, 8, 20))
    d21 = read_day(paths, date(2026, 8, 21))
    assert "last of the day" in d20 and "last of the day" not in d21
    assert "first of the day" in d21 and "first of the day" not in d20
    assert d20.count("last of the day") + d21.count("last of the day") == 1
    assert d20.count("first of the day") + d21.count("first of the day") == 1


# --------------------------------------------------------------------------
# DST fallback -- aware zoneinfo math, independent of OS/process TZ
# --------------------------------------------------------------------------


def test_dst_fallback_epochs_distinct_one_hour_apart_and_ordered(paths, monkeypatch):
    # Prove this doesn't depend on the box's own timezone.
    monkeypatch.setenv("TZ", "UTC")
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[
            ("111@s.whatsapp.net", "111", "edt one thirty", "2026-11-01 01:30:00-04:00", 0, None),
            ("111@s.whatsapp.net", "111", "est one thirty", "2026-11-01 01:30:00-05:00", 0, None),
        ],
    )
    lid_map = ae.load_lid_map_safe(paths["whatsapp_account_db"])
    contacts = ae.load_contacts_safe(paths["whatsapp_account_db"])
    events = ae.whatsapp_events(paths["whatsapp_db"], lid_map, contacts)
    edt_event = next(e for e in events if e.detail == "edt one thirty")
    est_event = next(e for e in events if e.detail == "est one thirty")

    assert edt_event.epoch != est_event.epoch
    assert abs((est_event.epoch - edt_event.epoch) - 3600) < 0.001
    assert edt_event.epoch < est_event.epoch

    days = [date(2026, 11, 1)]
    rc = ae.run(paths, days, local_epoch(2026, 11, 1, 12, 0, 0, -5))
    assert rc == 0
    content = read_day(paths, date(2026, 11, 1))
    rows = [l for l in events_only(content).splitlines() if l.startswith("| 01:30")]
    assert len(rows) == 2
    idx_edt = content.index("edt one thirty")
    idx_est = content.index("est one thirty")
    assert idx_edt < idx_est  # -04:00 instant is chronologically earlier


# --------------------------------------------------------------------------
# WhatsApp sent/assistant/received direction
# --------------------------------------------------------------------------


def test_whatsapp_assistant_vs_sent_vs_received(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[
            ("111@s.whatsapp.net", "me", "bridge sent this", "2026-08-25 09:00:00-04:00", 1, None),
            ("111@s.whatsapp.net", "2152181272617", "user's own phone", "2026-08-25 09:05:00-04:00", 1, None),
            ("111@s.whatsapp.net", "111", "incoming", "2026-08-25 09:10:00-04:00", 0, None),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "counts: {whatsapp_sent: 1, whatsapp_assistant: 1, whatsapp_received: 1" in content
    assert "sent (assistant)" in content
    lines = [l for l in content.splitlines() if l.startswith("|") and "bridge sent this" in l]
    assert lines and "sent (assistant)" in lines[0]
    lines = [l for l in content.splitlines() if l.startswith("|") and "user's own phone" in l]
    assert lines and split_row(lines[0])[2] == "sent"
    lines = [l for l in content.splitlines() if l.startswith("|") and "incoming" in l]
    assert lines and split_row(lines[0])[2] == "received"


def test_whatsapp_media_placeholder_image(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", "", "2026-08-25 09:00:00-04:00", 0, "image")],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("| 09:00")][0]
    assert split_row(row)[4] == "[image]"


# --------------------------------------------------------------------------
# Name resolution: whatsmeow_lid_map + whatsmeow_contacts
# --------------------------------------------------------------------------


def test_chat_name_real_name_takes_priority_over_lid_resolution(paths):
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("17205846358", "2152181272617"),
        lid_map=[("111", "19995551234")],
        contacts=[("19995551234@s.whatsapp.net", None, "Should Not Be Used", None)],
    )
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Real Chat Name")],
        messages=[("111@s.whatsapp.net", "111", "hi", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "Real Chat Name" in content
    assert "Should Not Be Used" not in content


def test_whatsapp_bare_lid_name_resolves_to_full_name(paths):
    # chats.name IS the bare LID -- must resolve via lid_map -> contacts.
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("17205846358", "2152181272617"),
        lid_map=[("182093845934191", "447582300805")],
        contacts=[("447582300805@s.whatsapp.net", "Jamie", "Jamie Smith", "jamiepush")],
    )
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("182093845934191@lid", "182093845934191")],
        messages=[("182093845934191@lid", "182093845934191", "hey", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("| 09:00")][0]
    assert split_row(row)[3] == "Jamie Smith"


def test_whatsapp_chat_named_with_own_lid_resolves_from_chat_jid(paths):
    # Real bridge data: chats.name holds the ACCOUNT's own LID for some chats.
    # Resolving that name would label the counterpart as the user himself.
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("17205846358", "2152181272617"),
        lid_map=[("2152181272617", "17205846358"), ("257186970222816", "13109801977")],
        contacts=[("13109801977@s.whatsapp.net", "", "Dad", "Jeff Ullman")],
    )
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("257186970222816@lid", "2152181272617")],
        messages=[("257186970222816@lid", "257186970222816", "hey", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    row = [l for l in read_day(paths, date(2026, 8, 25)).splitlines() if l.startswith("| 09:00")][0]
    assert split_row(row)[3] == "Dad"


def test_whatsapp_bare_lid_falls_back_to_plus_pn_when_no_contact(paths):
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("17205846358", "2152181272617"),
        lid_map=[("182093845934191", "447582300805")],
        contacts=[],
    )
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("182093845934191@lid", "182093845934191")],
        messages=[("182093845934191@lid", "182093845934191", "hey", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("| 09:00")][0]
    assert split_row(row)[3] == "+447582300805"


def test_call_counterpart_resolves_via_lid_map(paths):
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("17205846358", "2152181272617"),
        lid_map=[("182093845934191", "447582300805")],
        contacts=[("447582300805@s.whatsapp.net", None, None, "JamiePush")],
    )
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [
            call_line("CID", "offer", "2026-08-25T09:00:00-04:00", "182093845934191@lid", "182093845934191@lid"),
            call_line("CID", "accept", "2026-08-25T09:00:02-04:00", "182093845934191@lid", "2152181272617@lid"),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if "| call |" in l][0]
    assert split_row(row)[3] == "JamiePush"


def test_who_pipe_escaping(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Name|With|Pipes")],
        messages=[("111@s.whatsapp.net", "111", "hi", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("| 09:00")][0]
    # split_row only splits on UNescaped '|', so the escaped pipes stay
    # literal (backslash-preserved) inside the cell it returns.
    assert split_row(row)[3] == "Name\\|With\\|Pipes"
    assert "Name\\|With\\|Pipes" in row


# --------------------------------------------------------------------------
# Detail sanitization
# --------------------------------------------------------------------------


def test_detail_sanitization_pipe_newlines_trailing_ws(paths):
    nasty = "line one| has pipe\nline two\r\nline three   "
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", nasty, "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if l.startswith("| 09:00")]
    assert len(rows) == 1  # the row is a single logical line, no embedded \n
    row = rows[0]
    cells = split_row(row)
    assert len(cells) == 5
    assert "\n" not in row
    detail = cells[-1]
    assert "line one" in detail and "line two" in detail and "line three" in detail
    assert "line one\\| has pipe line two line three" in detail
    # raw line (before cell-stripping) must not carry trailing whitespace
    # before the closing pipe -- only the mandatory single separator space.
    before_closing_pipe = row[:-1]
    assert not before_closing_pipe.endswith("  ")


def test_detail_truncation_does_not_split_multibyte_char(paths):
    # euro sign sits exactly at index 79 (the 80th char) -- truncation to 80
    # chars must include it whole, never half of its UTF-8 encoding.
    text = "a" * 79 + "€" + "b" * 20
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", text, "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("| 09:00")][0]
    detail = split_row(row)[4]
    assert len(detail) <= 80
    detail.encode("utf-8").decode("utf-8")  # raises if a codepoint got split
    assert "€" in detail


# --------------------------------------------------------------------------
# iMessage dedup + no-offset local interpretation
# --------------------------------------------------------------------------


def test_imessage_dedup_newest_snapshot_wins(paths):
    rec_base = {
        "rowid": 501,
        "from_me": 0,
        "sender": "+15551234567",
        "chat_name": "",
        "chat_id": "+15551234567",
    }
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-20260825T090000.json",
        [dict(rec_base, ts="2026-08-25 08:59:00", text="oldest text")],
    )
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-20260825T091000.json",
        [dict(rec_base, ts="2026-08-25 08:59:00", text="middle text")],
    )
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-20260825T092000.json",
        [dict(rec_base, ts="2026-08-25 08:59:00", text="newest text")],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if l.startswith("| 08:59")]
    assert len(rows) == 1
    assert "newest text" in content
    assert "oldest text" not in content
    assert "middle text" not in content


def test_imessage_no_offset_interpreted_as_ny_local(paths, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-20260825T220000.json",
        [{"rowid": 1, "ts": "2026-08-25 21:40:00", "from_me": 1, "sender": "me",
          "chat_name": "", "chat_id": "x", "text": "no offset here"}],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 23, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "21:40" in content
    assert "no offset here" in content

    events, notes = ae.imessage_events(paths["imessage_dir"])
    ev = next(e for e in events if e.detail == "no offset here")
    expected_epoch = datetime(2026, 8, 25, 21, 40, tzinfo=ae.NY_TZ).timestamp()
    assert abs(ev.epoch - expected_epoch) < 0.001


def test_imessage_corrupt_snapshots_skipped_and_counted(paths):
    with open(os.path.join(paths["imessage_dir"], "messages-bad1.json"), "w") as f:
        pass  # 0 bytes
    with open(os.path.join(paths["imessage_dir"], "messages-bad2.json"), "w") as f:
        f.write("{not valid json")
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-good.json",
        [{"rowid": 1, "ts": "2026-08-25 10:00:00", "from_me": 0, "sender": "x",
          "chat_name": "y", "chat_id": "x", "text": "good row"}],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "good row" in content
    assert "imessage: 2 unreadable snapshots" in content


# --------------------------------------------------------------------------
# Chrome visit_time conversion (literal real-data value)
# --------------------------------------------------------------------------


def test_chrome_visit_time_known_instant_real_data_value():
    # Copied from a live row in /mnt/c/chrome-cdp-profile/Default/History on
    # 2026-08-26: visit_time=13432228715309359, url=lm-review dashboard,
    # independently cross-checked against sqlite's own
    # datetime(visit_time/1e6-11644473600,'unixepoch','localtime') on a box
    # whose TZ is America/New_York, which returned 2026-08-26 10:38:35.
    webkit = 13432228715309359
    epoch = ae.webkit_to_epoch(webkit)
    day, time_str = ae.local_day_time_from_epoch(epoch)
    assert day == date(2026, 8, 26)
    assert time_str == "10:38"


def test_chrome_corrupt_history_records_error_others_still_export(paths):
    with open(paths["chrome_history"], "wb") as f:
        f.write(b"not a real sqlite file, truncated garbage")
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", "still here", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "still here" in content
    assert any(l.startswith("errors: [") and "chrome:" in l for l in content.splitlines())


def test_all_sources_fail_leaves_existing_file_untouched(paths):
    day = date(2026, 8, 25)
    target = os.path.join(paths["vault_dir"], f"{day.isoformat()}.md")
    preexisting = "---\ndate: 2026-08-25\nPREEXISTING\n---\n\n_no activity_\n"
    with open(target, "w") as f:
        f.write(preexisting)

    with open(paths["whatsapp_db"], "wb") as f:
        f.write(b"corrupt")
    with open(paths["whatsapp_account_db"], "wb") as f:
        f.write(b"corrupt")
    with open(paths["calls_jsonl"], "w") as f:
        f.write("{not json\n")
    paths["imessage_dir"] = None  # glob.glob on a missing/empty dir doesn't raise; force a real error
    with open(paths["chrome_history"], "wb") as f:
        f.write(b"corrupt")

    rc = ae.run(paths, [day], local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 1
    with open(target) as f:
        after = f.read()
    assert after == preexisting


# --------------------------------------------------------------------------
# Calls (status only -- duration deliberately not computed)
# --------------------------------------------------------------------------


def test_calls_answered_status(paths):
    creator = "182093845934191@lid"
    lines = [
        call_line("CID1", "offer", "2026-08-25T09:00:00-04:00", creator, creator),
        call_line("CID1", "pre_accept", "2026-08-25T09:00:01-04:00", creator, creator),
        call_line("CID1", "accept", "2026-08-25T09:00:05-04:00", creator, "2152181272617@lid"),
        # real calls.jsonl pattern: terminate lands 0-13s after accept
        call_line("CID1", "terminate", "2026-08-25T09:00:09-04:00", creator, creator),
    ]
    make_calls_jsonl(str(paths["calls_jsonl"]), lines)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("| 09:00") and "| call |" in l]
    assert len(row) == 1
    cells = split_row(row[0])
    assert cells[4] == "answered"  # no duration suffix
    assert "whatsapp_calls: 1" in content


def test_calls_in_progress(paths):
    creator = "182093845934191@lid"
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [call_line("CID2", "offer", "2026-08-25T09:00:00-04:00", creator, creator)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if "| call |" in l][0]
    assert split_row(row)[4] == "in progress"


def test_calls_missed_no_accept(paths):
    creator = "182093845934191@lid"
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [
            call_line("CID3", "offer", "2026-08-25T09:00:00-04:00", creator, creator),
            call_line("CID3", "terminate", "2026-08-25T09:00:20-04:00", creator, creator),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if "| call |" in l][0]
    assert split_row(row)[4] == "missed"


def test_calls_rejected(paths):
    creator = "182093845934191@lid"
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [
            call_line("CID4", "offer", "2026-08-25T09:00:00-04:00", creator, creator),
            call_line("CID4", "reject", "2026-08-25T09:00:03-04:00", creator, "2152181272617@lid"),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if "| call |" in l][0]
    assert split_row(row)[4] == "rejected"


def test_call_crossing_midnight_attributed_to_start_day_once(paths):
    creator = "182093845934191@lid"
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [
            call_line("CID5", "offer", "2026-08-25T23:55:00-04:00", creator, creator),
            call_line("CID5", "accept", "2026-08-25T23:55:03-04:00", creator, "2152181272617@lid"),
            call_line("CID5", "terminate", "2026-08-26T00:10:00-04:00", creator, creator),
        ],
    )
    days = [date(2026, 8, 25), date(2026, 8, 26)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 26, 12, 0, 0, -4))
    assert rc == 0
    d25 = read_day(paths, date(2026, 8, 25))
    d26 = read_day(paths, date(2026, 8, 26))
    assert d25.count("| call |") == 1
    assert d26.count("| call |") == 0
    assert "23:55" in d25


def test_call_direction_uses_self_ids_from_device_fixture(paths):
    # A self-ID pair that is NOT DEFAULT_SELF_IDS -- must come from the DB.
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("19995551234", "88800011122"),
    )
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [
            # incoming: call_creator is some third party, not the fixture self-id
            call_line("CIN", "offer", "2026-08-25T09:00:00-04:00", "182093845934191@lid", "182093845934191@lid"),
            call_line("CIN", "accept", "2026-08-25T09:00:02-04:00", "182093845934191@lid", "88800011122@lid"),
            # outgoing: call_creator IS the fixture's own lid
            call_line("COUT", "offer", "2026-08-25T10:00:00-04:00", "88800011122@lid", "88800011122@lid"),
            call_line("COUT", "accept", "2026-08-25T10:00:02-04:00", "88800011122@lid", "182093845934191@lid"),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row_in = [l for l in content.splitlines() if l.startswith("| 09:00")][0]
    row_out = [l for l in content.splitlines() if l.startswith("| 10:00")][0]
    assert split_row(row_in)[2] == "incoming"
    assert split_row(row_out)[2] == "outgoing"


# --------------------------------------------------------------------------
# Chrome collapsing
# --------------------------------------------------------------------------


def test_chrome_collapse_within_anchor_window(paths):
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    visits = [
        (epoch_to_webkit(base + 0), "https://a.com", "A", ""),
        (epoch_to_webkit(base + 30), "https://a.com", "A", ""),
        (epoch_to_webkit(base + 59), "https://a.com", "A", ""),
        (epoch_to_webkit(base + 61), "https://a.com", "A", ""),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in content.splitlines() if "| chrome |" in l]
    assert len(rows) == 2


def test_chrome_a_tab_reauthenticating_itself_is_not_activity(paths):
    # 2026-09-03: a Google Docs tab left open re-authenticated every few
    # minutes from 19:29 to 21:13, three visits a round, and the evening read
    # as 36 minutes of work. Each round's first hop names the visit that
    # opened the tab that morning as its parent, so the chain is hours old
    # and nobody started it now.
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    opened = epoch_to_webkit(base)
    keepalive = epoch_to_webkit(base + 8 * 3600)
    visits = [
        (opened, "https://docs.google.com/document/d/1", "Doc", "", 0, PLAIN_VISIT),
        (keepalive, "https://docs.google.com/document/d/1", "Doc", "", 1, 0x40000000),
        (keepalive, "https://docs.google.com/document/u/1/d/1", "Doc", "", 2, 0x80000000),
        (keepalive, "https://docs.google.com/document/d/1", "Doc", "", 3, 0x60000000),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 23, 0, 0, -4))
    assert rc == 0
    rows = [l for l in read_day(paths, date(2026, 8, 25)).splitlines()
            if "| chrome |" in l]
    assert len(rows) == 1
    assert "| 09:00 |" in rows[0]


def test_chrome_the_destination_of_a_real_click_survives(paths):
    # The other half: a click that redirects writes hops too, and the last of
    # them is the page that was actually read. Dropping those would cost the
    # title of everything reached through a redirect.
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    visits = [
        (epoch_to_webkit(base), "https://a.com/go", "Go", "", 0, 0x10000000),
        (epoch_to_webkit(base + 1), "https://a.com/here", "Here", "", 1, 0xA0000000),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    rows = [l for l in read_day(paths, date(2026, 8, 25)).splitlines()
            if "| chrome |" in l]
    assert len(rows) == 2
    assert "Here" in rows[1]


def test_chrome_collapse_boundary_60s_is_inclusive(paths):
    # Intent: the collapse window is inclusive of 60s exactly (<=60 merges).
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    visits = [
        (epoch_to_webkit(base + 0), "https://a.com", "A", ""),
        (epoch_to_webkit(base + 60), "https://a.com", "A", ""),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in content.splitlines() if "| chrome |" in l]
    assert len(rows) == 1
    assert "(×2)" in rows[0]


def test_chrome_collapse_broken_by_different_url(paths):
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    visits = [
        (epoch_to_webkit(base + 0), "https://a.com", "A", ""),
        (epoch_to_webkit(base + 10), "https://b.com", "B", ""),
        (epoch_to_webkit(base + 20), "https://a.com", "A", ""),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in content.splitlines() if "| chrome |" in l]
    assert len(rows) == 3


def test_chrome_collapse_never_crosses_midnight(paths):
    day1_2359 = local_epoch(2026, 8, 25, 23, 59, 40, -4)
    day2_0000 = local_epoch(2026, 8, 26, 0, 0, 10, -4)
    visits = [
        (epoch_to_webkit(day1_2359), "https://a.com", "A", ""),
        (epoch_to_webkit(day2_0000), "https://a.com", "A", ""),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25), date(2026, 8, 26)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 26, 12, 0, 0, -4))
    assert rc == 0
    d25 = read_day(paths, date(2026, 8, 25))
    d26 = read_day(paths, date(2026, 8, 26))
    assert d25.count("| chrome |") == 1
    assert d26.count("| chrome |") == 1


def test_chrome_synced_vs_windows_labelling_and_counts(paths):
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    visits = [
        (epoch_to_webkit(base), "https://local.com", "Local", ""),
        (epoch_to_webkit(base + 120), "https://remote.com", "Remote", "SOMEGUID=="),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| chrome | visit | Windows |" in content
    assert "| chrome | visit | synced |" in content
    assert "chrome_windows: 1" in content
    assert "chrome_synced: 1" in content


# --------------------------------------------------------------------------
# Cross-source ordering + last_seen
# --------------------------------------------------------------------------


def test_cross_source_ordering(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", "wa event", "2026-08-25 09:10:00-04:00", 0, None)],
    )
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-1.json",
        [{"rowid": 1, "ts": "2026-08-25 09:05:00", "from_me": 0, "sender": "x",
          "chat_name": "y", "chat_id": "x", "text": "im event"}],
    )
    make_chrome_history(
        str(paths["chrome_history"]),
        [(epoch_to_webkit(local_epoch(2026, 8, 25, 9, 15, 0, -4)), "https://c.com", "C", "")],
    )
    make_calls_jsonl(
        str(paths["calls_jsonl"]),
        [
            call_line("CX", "offer", "2026-08-25T09:00:00-04:00", "182093845934191@lid", "182093845934191@lid"),
            call_line("CX", "accept", "2026-08-25T09:00:02-04:00", "182093845934191@lid", "2152181272617@lid"),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    body = events_only(content).split("|---|---|---|---|---|\n", 1)[1]
    rows = [l for l in body.splitlines() if l.startswith("|")]
    times = [split_row(r)[0] for r in rows]
    assert times == ["09:00", "09:05", "09:10", "09:15"]
    sources = [split_row(r)[1] for r in rows]
    assert sources == ["call", "imessage", "whatsapp", "chrome"]


def test_last_seen_values(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[
            ("111@s.whatsapp.net", "111", "first", "2026-08-25 08:00:00-04:00", 0, None),
            ("111@s.whatsapp.net", "111", "last", "2026-08-25 09:45:00-04:00", 0, None),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert 'last_seen: {whatsapp: "09:45"}' in content


# --------------------------------------------------------------------------
# Idempotency + empty day
# --------------------------------------------------------------------------


def test_rerun_identical_except_generated_at(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", "hi", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    first = read_day(paths, date(2026, 8, 25))
    ae.run(paths, days, local_epoch(2026, 8, 25, 12, 5, 0, -4))
    second = read_day(paths, date(2026, 8, 25))

    def strip_generated_at(s):
        # llm_calls also legitimately differs: run 1 attempts+caches the
        # period's summary (llm_calls: 1), run 2 hits the cache (llm_calls: 0).
        return "\n".join(
            l for l in s.splitlines()
            if not l.startswith("generated_at:") and not l.startswith("llm_calls:")
        )

    assert first != second  # generated_at (and llm_calls) differ
    assert strip_generated_at(first) == strip_generated_at(second)


def test_empty_day_has_frontmatter_and_no_activity_marker(paths):
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "_no activity_" in content
    assert "date: 2026-08-25" in content
    assert "counts: {whatsapp_sent: 0" in content


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def _wa_msg_at(hh, mm, text, chat="111@s.whatsapp.net"):
    return (chat, "111", text, f"2026-08-25 {hh:02d}:{mm:02d}:00-04:00", 0, None)


def periods_rows(content):
    """[(start, end, summary), ...] parsed from the '## Periods' table."""
    if "\n## Periods" not in content:
        return []
    section = content.split("\n## Periods", 1)[1]
    lines = [l for l in section.splitlines() if l.startswith("|")][2:]  # skip header+sep
    return [tuple(split_row(l)) for l in lines]


class CountingLLM:
    def __init__(self, reply="llm summary", fail=False):
        self.calls = 0
        self.reply = reply
        self.fail = fail

    def __call__(self, prompt):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated ask-haiku failure")
        return self.reply


def test_period_clustering_15min_boundary_splits_at_16(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[
            _wa_msg_at(9, 0, "a"), _wa_msg_at(9, 10, "b"),
            _wa_msg_at(9, 24, "c"), _wa_msg_at(9, 40, "d"),
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = periods_rows(content)
    assert [(r[0], r[1]) for r in rows] == [("09:00", "09:24"), ("09:40", "09:40")]
    assert "counts: {whatsapp_sent: 0, whatsapp_assistant: 0, whatsapp_received: 4" in content
    assert "periods: 2" in content


def test_period_clustering_exactly_15min_does_not_split(paths):
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 0, "a"), _wa_msg_at(9, 15, "b")],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = periods_rows(content)
    assert [(r[0], r[1]) for r in rows] == [("09:00", "09:15")]


def test_period_cache_hit_summarizer_called_once(paths):
    counting = CountingLLM()
    paths["llm_call"] = counting
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 0, "a"), _wa_msg_at(9, 5, "b")],
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    first = read_day(paths, date(2026, 8, 25))
    assert counting.calls == 1
    assert "summaries_llm: 1" in first
    assert periods_rows(first)[0][2] == "llm summary"

    with open(paths["cache_path"]) as f:
        cache = json.load(f)
    assert len(cache) == 1

    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 10, 0, -4)) == 0
    second = read_day(paths, date(2026, 8, 25))
    assert counting.calls == 1  # not called again -- cache hit
    assert "summaries_llm: 1" in second
    assert "llm_calls: 0" in second


def test_period_cache_miss_when_row_set_grows(paths):
    counting = CountingLLM()
    paths["llm_call"] = counting
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 0, "a")],
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    assert counting.calls == 1

    # Same period, one more row within the 15min window -- new hash, new call.
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 0, "a"), _wa_msg_at(9, 5, "b")],
    )
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 10, 0, -4)) == 0
    assert counting.calls == 2  # called again for the grown period


def test_period_summarizer_failure_falls_back_to_heuristic_uncached(paths):
    failing = CountingLLM(fail=True)
    paths["llm_call"] = failing
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Esme")],
        messages=[_wa_msg_at(9, 0, "a"), _wa_msg_at(9, 2, "b")],
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert failing.calls == 1
    assert "llm_calls: 1" in content  # the attempt still counts
    assert "summaries_llm: 0" in content
    summary = periods_rows(content)[0][2]
    assert "Esme" in summary and "×2" in summary  # heuristic text, not the LLM path

    with open(paths["cache_path"]) as f:
        cache = json.load(f)
    assert cache == {}  # failed summary is never cached

    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 10, 0, -4)) == 0
    assert failing.calls == 2  # retried next run, not treated as cached


def test_period_llm_cap_20_per_run(paths):
    msgs = [_wa_msg_at((i * 20) // 60, (i * 20) % 60, f"m{i}") for i in range(25)]
    make_whatsapp_db(str(paths["whatsapp_db"]), chats=[("111@s.whatsapp.net", "Alice")], messages=msgs)
    counting = CountingLLM()
    paths["llm_call"] = counting
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert counting.calls == 20
    assert "periods: 25" in content
    assert "summaries_llm: 20" in content
    assert "llm_calls: 20" in content
    rows = periods_rows(content)
    llm_summaries = [r[2] for r in rows if r[2] == "llm summary"]
    heuristic_summaries = [r[2] for r in rows if r[2] != "llm summary"]
    assert len(llm_summaries) == 20
    assert len(heuristic_summaries) == 5


def test_period_summary_sanitized_pipe_and_newline(paths):
    nasty = CountingLLM(reply="line one| with pipe\nline two")
    paths["llm_call"] = nasty
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 0, "a")],
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in content.splitlines() if l.startswith("## Periods")]
    section = content.split("\n## Periods", 1)[1]
    table_line = [l for l in section.splitlines() if l.startswith("| 09:00")][0]
    assert "\n" not in table_line
    cells = split_row(table_line)
    assert len(cells) == 3
    assert cells[2] == "line one\\| with pipe line two"


def test_periods_section_absent_on_empty_day_present_on_normal_day(paths):
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    empty_content = read_day(paths, date(2026, 8, 25))
    assert "## Periods" not in empty_content
    assert "periods: 0" in empty_content

    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 0, "a"), _wa_msg_at(10, 0, "b")],
    )
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "## Periods" in content
    assert "periods: 2" in content
    assert len(periods_rows(content)) == 2


# --------------------------------------------------------------------------
# Follow-up 1: open periods (last event within 20min of "now") never get an
# LLM call and are never cached.
# --------------------------------------------------------------------------


def test_period_open_gets_no_llm_call_closes_next_run(paths):
    counting = CountingLLM()
    paths["llm_call"] = counting
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[_wa_msg_at(9, 55, "a")],
    )
    days = [date(2026, 8, 25)]

    # T: last event (09:55) is 5min before "now" (10:00) -- open, no call.
    T = local_epoch(2026, 8, 25, 10, 0, 0, -4)
    assert ae.run(paths, days, T) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert counting.calls == 0
    assert "llm_calls: 0" in content
    assert "summaries_llm: 0" in content
    with open(paths["cache_path"]) as f:
        assert json.load(f) == {}  # never cached while open

    # T+25min: last event is now 30min in the past -- closed, one call.
    assert ae.run(paths, days, T + 25 * 60) == 0
    content2 = read_day(paths, date(2026, 8, 25))
    assert counting.calls == 1
    assert "llm_calls: 1" in content2
    assert "summaries_llm: 1" in content2


# --------------------------------------------------------------------------
# Follow-up 2: periods longer than 45min are split at internal gaps.
# --------------------------------------------------------------------------


def test_period_length_cap_splits_dense_3hr_stretch_without_loss(paths):
    base = local_epoch(2026, 8, 25, 0, 0, 0, -4)
    events = [
        ae.Event(base + i * 300, date(2026, 8, 25), "", "whatsapp", "received", "Alice", f"m{i}")
        for i in range(37)  # every 5min, 0:00 through 3:00
    ]
    raw = ae.cluster_periods(events)
    assert len(raw) == 1  # all <=15min apart -> one gap-cluster
    pieces = ae.split_period_by_length(raw[0])
    assert all((p[-1].epoch - p[0].epoch) <= ae.PERIOD_MAX_SECONDS for p in pieces)
    flat = [e for p in pieces for e in p]
    assert flat == sorted(events, key=lambda e: e.epoch)  # contiguous, no loss/dup
    assert len(flat) == 37


def test_period_length_cap_splits_at_the_12min_gap(paths):
    base = local_epoch(2026, 8, 25, 0, 0, 0, -4)
    offsets_min = [0, 5, 10, 22, 27, 32, 37, 42, 47, 52, 57, 60]  # one 12min gap (10->22)
    events = [
        ae.Event(base + m * 60, date(2026, 8, 25), "", "whatsapp", "received", "Alice", f"m{i}")
        for i, m in enumerate(offsets_min)
    ]
    pieces = ae.split_period_by_length(events)
    assert len(pieces) == 2
    assert pieces[0][-1].detail == "m2"  # last event before the 12min gap (t=10)
    assert pieces[1][0].detail == "m3"   # first event after the 12min gap (t=22)
    assert (pieces[0][-1].epoch - pieces[0][0].epoch) <= ae.PERIOD_MAX_SECONDS
    assert (pieces[1][-1].epoch - pieces[1][0].epoch) <= ae.PERIOD_MAX_SECONDS


# --------------------------------------------------------------------------
# Follow-up 3: heuristic chrome bucket never prints '?'.
# --------------------------------------------------------------------------


def test_chrome_label_falls_back_to_title_never_question_mark(paths):
    assert ae.domain_label("") is None
    assert ae.domain_label(None) is None
    assert ae.chrome_label("Weird Local Page — ") == "Weird Local Page"
    assert ae.chrome_label("No Dash Detail Only") == "No Dash Detail Only"

    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    period = [
        ae.Event(base, date(2026, 8, 25), "09:00", "chrome", "visit", "Windows", "Weird Local Page — "),
        ae.Event(base + 60, date(2026, 8, 25), "09:01", "chrome", "visit", "Windows", "Weird Local Page — "),
        ae.Event(base + 120, date(2026, 8, 25), "09:02", "chrome", "visit", "Windows", "Real Site — https://real.com"),
    ]
    summary = ae.heuristic_period_summary(period)
    assert "?" not in summary
    assert "Weird Local Page" in summary or "Real" in summary


def test_period_heuristic_chrome_never_shows_question_mark_end_to_end(paths):
    failing = CountingLLM(fail=True)
    paths["llm_call"] = failing
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    visits = [
        (epoch_to_webkit(base), "", "Weird Local Page", ""),
        (epoch_to_webkit(base + 60), "https://real.com", "Real Site", ""),
    ]
    make_chrome_history(str(paths["chrome_history"]), visits)
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    summary = periods_rows(content)[0][2]
    assert "?" not in summary


# --------------------------------------------------------------------------
# Follow-up 4: chrome_label never emits a URL scheme fragment (e.g. "Https:"
# from a url truncated down to just its scheme), and period summaries
# truncate at a word boundary instead of mid-word.
# --------------------------------------------------------------------------

# Exact detail string from a live run (2026-08-26, 10:54 chrome row): the
# combined "title — url" string hit sanitize_detail's 80-char cap right
# after "https:", so the stored url tail is the bare scheme with no host.
EXACT_TRUNCATED_CHROME_DETAIL = (
    "AMC Lincoln Square 13 Movie Showtimes & Tickets \\| New York \\| Fandango — https:"
)


def test_chrome_label_exact_truncated_https_row_never_scheme_fragment(paths):
    assert len(EXACT_TRUNCATED_CHROME_DETAIL) == 80  # confirms this is the real truncated row
    assert ae.extract_chrome_url(EXACT_TRUNCATED_CHROME_DETAIL) == "https:"
    # Old bug: urlparse("//" + "https:").netloc misparses as netloc="https:",
    # which domain_label used to accept and capitalize to "Https:".
    assert ae.domain_label("https:") is None
    assert ae.looks_like_url_fragment("https:")
    assert ae.looks_like_url_fragment("https://")
    assert not ae.looks_like_url_fragment("AMC Lincoln Square 13")

    label = ae.chrome_label(EXACT_TRUNCATED_CHROME_DETAIL)
    assert label == "AMC Lincoln Square 1"  # falls back to the real title, truncated to 20
    assert "https:" not in label.lower()

    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    period = [ae.Event(base, date(2026, 8, 25), "09:00", "chrome", "visit", "synced", EXACT_TRUNCATED_CHROME_DETAIL)]
    summary = ae.heuristic_period_summary(period)
    assert "Https:" not in summary
    assert "https:" not in summary.lower()


def test_chrome_label_url_only_no_title_is_dropped(paths):
    # No clean domain (truncated to scheme) AND no real title (title text
    # itself is a url) -- must return None so it's dropped from the top-3,
    # never a scheme fragment.
    assert ae.chrome_label("https://example.com/a-very-long-path — https:") is None


def test_truncate_at_word_boundary_unit():
    s = "Add implement route-registration and core-component-prop spark-ui benchmark tasks"
    out = ae.truncate_at_word_boundary(s, 20)
    assert len(out) <= 20
    assert out.endswith("…")
    words = s.split(" ")
    kept = out[:-1].strip()
    assert kept in ("",) or kept in [" ".join(words[:i]) for i in range(1, len(words) + 1)]


def test_truncate_at_word_boundary_single_long_token_hard_cuts():
    s = "x" * 200
    out = ae.truncate_at_word_boundary(s, 20)
    assert len(out) <= 20
    assert out.endswith("…")


def test_period_summary_truncation_never_splits_a_word(paths):
    # Reproduces the reported "175 pages: Google, ?, ..." / "Goo" / "Add
    # implemen" style mid-word cuts from the old maxlen=110 hard truncation.
    long_summary = (
        "WhatsApp Esme Robinson ×86 · iMessage +525545923618 ×37 · "
        "WhatsApp Estelle Reardon ×31 · 175 pages: Google, Youtube, "
        "Add implementation route registration benchmark tasks"
    )
    assert len(long_summary) > 110
    out = ae.sanitize_period_summary(long_summary)
    assert len(out) <= 110
    assert out.endswith("…")
    kept_words = out[:-1].strip().split(" ")
    source_words = long_summary.split(" ")
    # every kept word matches the source word at that position, up to the
    # trailing-punctuation strip before the ellipsis (e.g. "Google," -> "Google")
    for i, w in enumerate(kept_words):
        assert source_words[i].startswith(w)


# --------------------------------------------------------------------------
# Source 5: Claude Code prompts + mandatory redaction of the private project.
# Fake roots/keywords ONLY -- never the real ~/.config/activity-export/redact.json.
# --------------------------------------------------------------------------


def make_claude_record(timestamp, cwd, session_id, content, rec_type="user"):
    return {
        "type": rec_type,
        "timestamp": timestamp,
        "cwd": cwd,
        "sessionId": session_id,
        "message": {"role": "user", "content": content},
    }


def write_claude_project_file(claude_dir, project_name, filename, records, mtime_epoch):
    proj_dir = os.path.join(claude_dir, project_name)
    os.makedirs(proj_dir, exist_ok=True)
    path = os.path.join(proj_dir, filename)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    os.utime(path, (mtime_epoch, mtime_epoch))
    return path


def fake_redact(tmp_path, root_name="fixture/secret-project", keyword="zzzprivate"):
    """A fake, test-only private-project config in the already-normalized
    shape claude_events expects (realpath roots, lowercased keywords) --
    mirrors what load_redact_config would produce, but built directly so no
    real file is ever touched. Values here are obviously fake fixtures, not
    any real project's name or path."""
    root = os.path.realpath(str(tmp_path / root_name))
    return {"private_roots": [root], "private_keywords": [keyword.lower()]}, root


def test_claude_cwd_under_root_is_redacted(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", root, "sess-1", "do the secret thing")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    now = local_epoch(2026, 8, 25, 12, 0, 0, -4)
    assert ae.run(paths, days, now) == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| claude |" in l][0]
    assert row == "| 09:00 | claude | prompt | autojournal | autojournal work |"


def test_claude_worktree_path_under_root_is_redacted(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    worktree_cwd = os.path.join(root, ".claude", "worktrees", "some-branch")
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", worktree_cwd, "sess-1", "fix the bug here")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| claude |" in l][0]
    assert row == "| 09:00 | claude | prompt | autojournal | autojournal work |"


def test_claude_keyword_in_text_unrelated_cwd_is_redacted(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    other_cwd = str(tmp_path / "totally-unrelated-repo")
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", other_cwd, "sess-1", "let's talk about zzzprivate today")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| claude |" in l][0]
    assert row == "| 09:00 | claude | prompt | autojournal | autojournal work |"


def test_claude_keyword_match_is_case_insensitive(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    other_cwd = str(tmp_path / "totally-unrelated-repo")
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", other_cwd, "sess-1", "ZzzPrivate needs a fix")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| claude |" in l][0]
    assert row == "| 09:00 | claude | prompt | autojournal | autojournal work |"


def test_claude_session_redacted_once_stays_redacted_in_that_file(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    other_cwd = str(tmp_path / "totally-unrelated-repo")
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [
            make_claude_record("2026-08-25T13:00:00.000Z", root, "sess-1", "working in the secret repo"),
            make_claude_record("2026-08-25T13:05:00.000Z", other_cwd, "sess-1", "totally unrelated harmless prompt"),
        ],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 2
    for row in rows:
        assert row.endswith("| claude | prompt | autojournal | autojournal work |")
    assert "totally unrelated harmless prompt" not in content


def test_claude_missing_redact_config_fails_closed_other_sources_fine(paths, tmp_path):
    # Fixture default already points redact_path at a nonexistent file and
    # sets no paths["redact"] -- exercise that default explicitly.
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", "a normal prompt")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    make_whatsapp_db(
        str(paths["whatsapp_db"]),
        chats=[("111@s.whatsapp.net", "Alice")],
        messages=[("111@s.whatsapp.net", "111", "still here", "2026-08-25 09:00:00-04:00", 0, None)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0  # other 4 sources succeed -- not all-sources-failed
    content = read_day(paths, date(2026, 8, 25))
    assert "claude: redaction config unavailable" in content
    assert "| claude |" not in events_only(content)
    assert "claude_prompts: 0" in content
    assert "still here" in content  # whatsapp source unaffected


def test_claude_command_message_only_record_is_skipped(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    content_str = "<command-message>run the thing</command-message><command-name>/foo</command-name>"
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", content_str)],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)
    assert "claude_prompts: 0" in content


def test_claude_utc_timestamp_converts_to_ny_local(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-26T02:30:00Z", str(tmp_path / "anywhere"), "sess-1", "late night prompt")],
        local_epoch(2026, 8, 26, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25), date(2026, 8, 26)]
    assert ae.run(paths, days, local_epoch(2026, 8, 26, 12, 0, 0, -4)) == 0
    d25 = read_day(paths, date(2026, 8, 25))
    d26 = read_day(paths, date(2026, 8, 26))
    row25 = [l for l in events_only(d25).splitlines() if "| claude |" in l]
    row26 = [l for l in events_only(d26).splitlines() if "| claude |" in l]
    assert len(row25) == 1 and len(row26) == 0
    assert row25[0].startswith("| 22:30 |")
    assert "late night prompt" in row25[0]


def test_claude_who_derivation_repo_worktree_job():
    assert ae.claude_who_from_cwd("/home/esme/coding/myrepo") == "myrepo"
    assert ae.claude_who_from_cwd("/home/esme/coding/myrepo/.claude/worktrees/some-branch") == "myrepo"
    assert ae.claude_who_from_cwd("/home/esme/.claude/jobs/abc123/tmp/work") == "job"
    assert ae.claude_who_from_cwd(None) == "(unknown)"


def test_claude_redacted_row_prompt_never_leaks_keyword_or_root(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    base = local_epoch(2026, 8, 25, 9, 0, 0, -4)
    redacted_event = ae.Event(base, date(2026, 8, 25), "09:00", "claude", "prompt", "autojournal", "autojournal work")
    normal_event = ae.Event(base + 60, date(2026, 8, 25), "09:01", "whatsapp", "received", "Alice", "hi there")
    period = [redacted_event, normal_event]
    prompt = ae.build_period_prompt(period)
    assert "autojournal work" in prompt
    assert "zzzprivate" not in prompt.lower()
    assert root.lower() not in prompt.lower()


# --------------------------------------------------------------------------
# Incremental claude offset cache -- avoid re-reading unchanged transcript
# bytes every run. White-box tests against read_claude_file_cached directly:
# each "reuse" assertion poisons the cached value first, so a pass proves
# the real (correct) file content was NOT re-read, not just that the
# happens to match.
# --------------------------------------------------------------------------


def test_claude_offset_cache_reuses_unchanged_file_verbatim(tmp_path):
    path = str(tmp_path / "s1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "proj"), "sess-1", "real message")) + "\n")
    redact = {"private_roots": [], "private_keywords": []}
    offset_cache = {}
    prompts1, meta1 = ae.read_claude_file_cached(path, redact, offset_cache)
    assert len(prompts1) == 1
    assert prompts1[0]["detail"] == "real message"
    assert meta1["user_prompt_count"] == 1

    offset_cache[ae._claude_cache_key(path)]["prompts"][0]["detail"] = "POISONED"  # only reuse would return this
    prompts2, meta2 = ae.read_claude_file_cached(path, redact, offset_cache)
    assert prompts2[0]["detail"] == "POISONED"


def test_claude_offset_cache_incremental_read_only_new_tail(tmp_path):
    path = str(tmp_path / "s1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "proj"), "sess-1", "first message")) + "\n")
    redact = {"private_roots": [], "private_keywords": []}
    offset_cache = {}
    prompts1, meta1 = ae.read_claude_file_cached(path, redact, offset_cache)
    assert len(prompts1) == 1

    offset_cache[ae._claude_cache_key(path)]["prompts"][0]["detail"] = "POISONED"  # would be re-derived correctly, if re-read

    with open(path, "a") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:05:00.000Z", str(tmp_path / "proj"), "sess-1", "second message")) + "\n")

    prompts2, meta2 = ae.read_claude_file_cached(path, redact, offset_cache)
    assert len(prompts2) == 2
    assert prompts2[0]["detail"] == "POISONED"        # earlier row: cache reused, not re-read
    assert prompts2[1]["detail"] == "second message"  # new row: freshly parsed from the tail
    assert meta2["user_prompt_count"] == 2


def test_claude_offset_cache_retroactive_redaction_on_later_root_touch(tmp_path):
    redact, root = fake_redact(tmp_path)
    path = str(tmp_path / "s1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "unrelated"), "sess-1", "innocuous first message")) + "\n")
    offset_cache = {}
    prompts1, meta1 = ae.read_claude_file_cached(path, redact, offset_cache)
    assert prompts1[0]["redacted"] is False
    assert prompts1[0]["detail"] == "innocuous first message"

    with open(path, "a") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:05:00.000Z", root, "sess-1", "now touching the private repo")) + "\n")

    prompts2, meta2 = ae.read_claude_file_cached(path, redact, offset_cache)
    assert len(prompts2) == 2
    for p in prompts2:
        assert p["redacted"] is True
        assert p["who"] == ae.REDACTED_WHO
        assert p["detail"] == ae.REDACTED_DETAIL
    # The cache itself must no longer hold the real earlier text -- checked
    # directly on the persisted entry, not just the return value.
    assert offset_cache[ae._claude_cache_key(path)]["prompts"][0]["detail"] == ae.REDACTED_DETAIL
    assert offset_cache[ae._claude_cache_key(path)]["prompts"][0]["who"] == ae.REDACTED_WHO


def test_claude_offset_cache_full_reparse_on_shrink(tmp_path):
    path = str(tmp_path / "s1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "proj"), "sess-1", "aaaaaaaaaaaaaaaaaaaa")) + "\n")
    redact = {"private_roots": [], "private_keywords": []}
    offset_cache = {}
    ae.read_claude_file_cached(path, redact, offset_cache)

    # File shrinks (rewritten, not appended) -- must fall back to a full
    # re-parse rather than seeking to a now-invalid offset.
    with open(path, "w") as f:
        f.write(json.dumps(make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "proj"), "sess-1", "short")) + "\n")
    st = os.stat(path)
    os.utime(path, (st.st_mtime + 1, st.st_mtime + 1))

    prompts, meta = ae.read_claude_file_cached(path, redact, offset_cache)
    assert len(prompts) == 1
    assert prompts[0]["detail"] == "short"


def test_claude_run_end_to_end_reuses_cache_across_runs(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "proj"), "sess-1", "steady state prompt")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    with open(paths["claude_offset_cache_path"]) as f:
        cache_after_run1 = json.load(f)
    assert cache_after_run1["__version__"] == ae.CLAUDE_OFFSET_CACHE_VERSION
    file_entries = {k: v for k, v in cache_after_run1.items() if k != "__version__"}
    assert len(file_entries) == 1
    entry = next(iter(file_entries.values()))
    assert entry["prompts"][0]["detail"] == "steady state prompt"

    # Second run, nothing changed on disk -- cache file must exist and be
    # reused (same size/mtime fast path), output stays identical.
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 5, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "steady state prompt" in content


# --------------------------------------------------------------------------
# Prompt noise filters -- skip records/files that Oliver never typed.
# --------------------------------------------------------------------------


def test_claude_ismeta_record_skipped(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    rec = make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", "a meta record")
    rec["isMeta"] = True
    write_claude_project_file(paths["claude_dir"], "proj", "s1.jsonl", [rec], local_epoch(2026, 8, 25, 12, 0, 0, -4))
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)


@pytest.mark.parametrize("noise_text", [
    "Base directory for this skill: /home/esme/.claude/plugins/x",
    "<some-unknown-tag>content</some-unknown-tag>",
    "[Image #1]",
    "Caveat: this session is being run without approval",
])
def test_claude_noise_prefix_skipped(paths, tmp_path, noise_text):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", noise_text)],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)


@pytest.mark.parametrize("marker", ["<task-notification>", "<cross-session-message"])
def test_claude_notification_marker_skipped(paths, tmp_path, marker):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    text = f"some preamble {marker} details here"
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", text)],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)


def test_claude_automation_file_jobs_cwd_single_prompt_skipped(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    job_cwd = str(tmp_path / ".claude" / "jobs" / "job123" / "tmp")
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", job_cwd, "sess-1", "do the background task")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)


def test_claude_automation_file_jobs_cwd_multi_prompt_kept(paths, tmp_path):
    # Sanity check on the AND: a jobs-cwd session with real back-and-forth
    # (more than one prompt) is NOT automation and must still show up.
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    job_cwd = str(tmp_path / ".claude" / "jobs" / "job123" / "tmp")
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [
            make_claude_record("2026-08-25T13:00:00.000Z", job_cwd, "sess-1", "do the background task"),
            make_claude_record("2026-08-25T13:05:00.000Z", job_cwd, "sess-1", "actually also do this other thing"),
        ],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 2
    assert split_row(rows[0])[3] == "job"


def test_claude_oversized_prompt_dropped_per_record_not_whole_file(paths, tmp_path):
    # A persona/system automation turn is dropped individually wherever it
    # appears in the file -- NOT only if it's the file's first turn, since
    # at least one real automation pipeline on this box (a recurring
    # WhatsApp-nudge watcher) appends a fresh long persona prompt to the
    # SAME session file every time it runs, not just once at the start.
    # The short, genuinely-typed follow-up in the same file must survive.
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    long_persona_prompt = "You are a persona. " * 100  # > 800 chars
    assert len(long_persona_prompt) > 800
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [
            make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", "a short follow up"),
            make_claude_record("2026-08-25T13:05:00.000Z", str(tmp_path / "anywhere"), "sess-1", long_persona_prompt),
            make_claude_record("2026-08-25T13:10:00.000Z", str(tmp_path / "anywhere"), "sess-1", long_persona_prompt),
        ],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 1
    assert "a short follow up" in rows[0]
    assert "You are a persona" not in content


def test_claude_normal_prompt_under_800_chars_kept(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", "a perfectly normal typed prompt")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "a perfectly normal typed prompt" in events_only(content)


def test_claude_offset_cache_version_bump_invalidates_stale_entries(tmp_path):
    # Discovered live: an unchanged (same size+mtime) transcript's cached
    # parse result outlives a filter-rule change unless the cache format
    # itself is versioned. A stale-version cache must be discarded wholesale.
    path = str(tmp_path / "cache.json")
    stale_entry = {
        "size": 10, "mtime": 123.0, "offset": 10,
        "session_redacted": [],
        "file_meta": {"cwd_under_jobs": False, "user_prompt_count": 1},
        "prompts": [{"epoch": 1, "session_id": "s", "redacted": False, "who": "x", "detail": "STALE"}],
    }
    stale_data = {"__version__": ae.CLAUDE_OFFSET_CACHE_VERSION - 1, "/some/file.jsonl": stale_entry}
    with open(path, "w") as f:
        json.dump(stale_data, f)
    assert ae.load_claude_offset_cache(path) == {}


def test_claude_offset_cache_save_load_roundtrip_preserves_current_version(tmp_path):
    path = str(tmp_path / "cache.json")
    cache = {
        "/some/file.jsonl": {
            "size": 1, "mtime": 1.0, "offset": 1, "session_redacted": [],
            "file_meta": {"cwd_under_jobs": False, "user_prompt_count": 1},
            "prompts": [],
        }
    }
    ae.save_claude_offset_cache(path, cache)
    assert ae.load_claude_offset_cache(path) == cache


def test_claude_offset_cache_key_never_leaks_raw_path(paths, tmp_path):
    # Real finding: a transcript's path encodes its project directory name,
    # so for the private project the path ITSELF contains the fixture
    # keyword/root -- even with every prompt row correctly redacted, using
    # the raw path as the cache file's JSON dict key would leak that the
    # private project exists. The cache file must contain neither.
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    private_subdir_path = write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", root, "sess-1", "working here")],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    # Rename the transcript file itself into a path containing the keyword,
    # mirroring how Claude Code flattens cwd into the project dir name.
    leaky_dir = os.path.join(paths["claude_dir"], "proj-zzzprivate-podctl")
    os.makedirs(leaky_dir, exist_ok=True)
    leaky_path = os.path.join(leaky_dir, "s2.jsonl")
    shutil.move(private_subdir_path, leaky_path)
    os.utime(leaky_path, (local_epoch(2026, 8, 25, 12, 0, 0, -4),) * 2)

    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0

    with open(paths["claude_offset_cache_path"]) as f:
        raw_cache_text = f.read()
    assert "zzzprivate" not in raw_cache_text.lower()
    assert leaky_path not in raw_cache_text
    assert leaky_dir not in raw_cache_text


# --------------------------------------------------------------------------
# Templated-prompt filter -- 3+ same-first-80-chars prompts in one file
# means an automation pipeline, not something typed by hand each time.
# --------------------------------------------------------------------------


def _templated(payload):
    # Shared header is >80 chars on its own, so the varying payload always
    # lands after the canonical 80-char prefix -- otherwise a differing
    # digit count (5 vs 12 vs 30) inside the first 80 chars would itself
    # break the "same prefix" premise the test is trying to set up.
    header = "Summarize this stretch of personal activity in one line, plain, preamble-free response required: "
    assert len(header) > 80
    return header + f"payload={payload}"


def test_claude_three_templated_one_genuine_only_genuine_survives(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [
            make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", _templated(5)),
            make_claude_record("2026-08-25T13:05:00.000Z", str(tmp_path / "anywhere"), "sess-1", _templated(12)),
            make_claude_record("2026-08-25T13:10:00.000Z", str(tmp_path / "anywhere"), "sess-1", _templated(30)),
            make_claude_record("2026-08-25T13:15:00.000Z", str(tmp_path / "anywhere"), "sess-1", "hey can you check the fridge order for me"),
        ],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 1
    assert "fridge order" in rows[0]
    assert "Summarize this" not in content


def test_claude_two_templated_both_kept(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [
            make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", _templated(5)),
            make_claude_record("2026-08-25T13:05:00.000Z", str(tmp_path / "anywhere"), "sess-1", _templated(12)),
        ],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 2  # below the 3-occurrence threshold -- neither dropped


def test_claude_automation_prompt_length_boundary_800(paths, tmp_path):
    at_800 = "a" * 800
    over_800 = "a" * 801
    assert len(at_800) == 800 and len(over_800) == 801
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    write_claude_project_file(
        paths["claude_dir"], "proj", "s1.jsonl",
        [
            make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "anywhere"), "sess-1", at_800),
            make_claude_record("2026-08-25T13:05:00.000Z", str(tmp_path / "anywhere"), "sess-2", over_800),
        ],
        local_epoch(2026, 8, 25, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 1
    assert "a" * 80 in rows[0]  # the 800-char one, truncated for display but present


# --------------------------------------------------------------------------
# Cross-file, per-day templated-prompt filter -- catches one-shot `claude -p`
# automation where each invocation is its own transcript file with a single
# prompt, so the per-file rule alone never sees the repetition.
# --------------------------------------------------------------------------


def test_claude_three_files_one_identical_prompt_each_all_dropped(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    header = "You classify events into one subcategory tag for a personal event-review app. "
    assert len(header) > 60
    for i in range(3):
        write_claude_project_file(
            paths["claude_dir"], f"proj{i}", "s.jsonl",
            [make_claude_record(f"2026-08-25T13:0{i}:00.000Z", str(tmp_path / f"proj{i}"), f"sess-{i}", header + f"event {i}")],
            local_epoch(2026, 8, 25, 12, 0, 0, -4),
        )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)


def test_claude_three_files_varying_digits_still_collapse_and_drop(paths, tmp_path):
    # "~1-minute" and "~6-minute" must canonicalize to the same key --
    # digit runs are template PAYLOAD, not template identity.
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    header = "Summarize this ~{n}-minute stretch of personal activity in one line, plain here"
    assert len(header.format(n=1)) > 60
    for i, minutes in enumerate([1, 6, 42]):
        write_claude_project_file(
            paths["claude_dir"], f"proj{i}", "s.jsonl",
            [make_claude_record(f"2026-08-25T13:0{i}:00.000Z", str(tmp_path / f"proj{i}"), f"sess-{i}", header.format(n=minutes))],
            local_epoch(2026, 8, 25, 12, 0, 0, -4),
        )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "| claude |" not in events_only(content)


def test_claude_two_files_same_prompt_kept(paths, tmp_path):
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    header = "You classify events into one subcategory tag for a personal event-review app. "
    for i in range(2):
        write_claude_project_file(
            paths["claude_dir"], f"proj{i}", "s.jsonl",
            [make_claude_record(f"2026-08-25T13:0{i}:00.000Z", str(tmp_path / f"proj{i}"), f"sess-{i}", header + f"event {i}")],
            local_epoch(2026, 8, 25, 12, 0, 0, -4),
        )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| claude |" in l]
    assert len(rows) == 2  # below the 3-occurrence threshold -- neither dropped


def test_claude_day_template_isolated_per_day(paths, tmp_path):
    # 2 occurrences today + 1 yesterday must NOT combine to hit the
    # cross-file threshold -- the rule is scoped to one calendar day.
    redact, root = fake_redact(tmp_path)
    paths["redact"] = redact
    header = "You classify events into one subcategory tag for a personal event-review app. "
    write_claude_project_file(
        paths["claude_dir"], "proj0", "s.jsonl",
        [make_claude_record("2026-08-25T13:00:00.000Z", str(tmp_path / "proj0"), "sess-0", header + "event a")],
        local_epoch(2026, 8, 26, 12, 0, 0, -4),
    )
    write_claude_project_file(
        paths["claude_dir"], "proj1", "s.jsonl",
        [make_claude_record("2026-08-25T13:05:00.000Z", str(tmp_path / "proj1"), "sess-1", header + "event b")],
        local_epoch(2026, 8, 26, 12, 0, 0, -4),
    )
    write_claude_project_file(
        paths["claude_dir"], "proj2", "s.jsonl",
        [make_claude_record("2026-08-26T13:00:00.000Z", str(tmp_path / "proj2"), "sess-2", header + "event c")],
        local_epoch(2026, 8, 26, 12, 0, 0, -4),
    )
    days = [date(2026, 8, 25), date(2026, 8, 26)]
    assert ae.run(paths, days, local_epoch(2026, 8, 26, 12, 0, 0, -4)) == 0
    d25 = read_day(paths, date(2026, 8, 25))
    d26 = read_day(paths, date(2026, 8, 26))
    d25_rows = [l for l in events_only(d25).splitlines() if "| claude |" in l]
    d26_rows = [l for l in events_only(d26).splitlines() if "| claude |" in l]
    assert len(d25_rows) == 2  # only 2 that day, below threshold
    assert len(d26_rows) == 1  # only 1 that day


# --------------------------------------------------------------------------
# Source 6: iPhone call history (~/inbox/imessage-sync/calls-*.json)
# --------------------------------------------------------------------------


def make_calls_snapshot(dir_path, filename, records):
    with open(os.path.join(dir_path, filename), "w") as f:
        json.dump(records, f)


def call_rec(pk, ts, address, duration_s=0, originated=0, answered=0, call_type=1, provider="com.apple.Telephony"):
    return {
        "pk": pk, "ts": ts, "address": address, "duration_s": duration_s,
        "originated": originated, "answered": answered, "call_type": call_type,
        "provider": provider,
    }


def test_phone_calls_zero_files_zero_rows_no_error(paths):
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    assert "phone_calls: 0" in content
    assert "phone:" not in content  # no error entry for the (expected) empty glob
    assert "| phone |" not in events_only(content)


def test_phone_calls_dedupe_overlap_newer_file_wins(paths):
    make_calls_snapshot(
        paths["imessage_dir"], "calls-20260825T090000.json",
        [call_rec(501, "2026-08-25 09:00:00", "+15551234567", duration_s=60, originated=0, answered=1)],
    )
    make_calls_snapshot(
        paths["imessage_dir"], "calls-20260825T100000.json",
        [call_rec(501, "2026-08-25 09:00:00", "+15551234567", duration_s=600, originated=0, answered=1)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = [l for l in events_only(content).splitlines() if "| phone |" in l]
    assert len(rows) == 1
    assert split_row(rows[0])[4] == "answered · 10m"  # 600s = 10m, the NEWER file's value


def test_phone_calls_who_resolved_via_whatsapp_contacts(paths):
    make_whatsapp_account_db(
        str(paths["whatsapp_account_db"]),
        device=("17205846358", "2152181272617"),
        contacts=[("15551234567@s.whatsapp.net", "Jamie", "Jamie Smith", "jamiepush")],
    )
    make_calls_snapshot(
        paths["imessage_dir"], "calls-1.json",
        [call_rec(1, "2026-08-25 09:00:00", "+1 (555) 123-4567", duration_s=120, originated=0, answered=1)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| phone |" in l][0]
    assert split_row(row)[3] == "Jamie Smith"


def test_phone_calls_who_resolved_via_imessage_chat_name(paths):
    # No WhatsApp contact for this address -- falls back to any chat_name
    # seen for the same address in the iMessage snapshots.
    make_imessage_snapshot(
        paths["imessage_dir"], "messages-1.json",
        [{"rowid": 1, "ts": "2026-08-25 08:00:00", "from_me": 0, "sender": "+15559998888",
          "chat_name": "Dad", "chat_id": "+15559998888", "text": "hi"}],
    )
    make_calls_snapshot(
        paths["imessage_dir"], "calls-1.json",
        [call_rec(1, "2026-08-25 09:00:00", "+15559998888", duration_s=90, originated=0, answered=1)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| phone |" in l][0]
    assert split_row(row)[3] == "Dad"


def test_phone_calls_who_falls_back_to_raw_address(paths):
    make_calls_snapshot(
        paths["imessage_dir"], "calls-1.json",
        [call_rec(1, "2026-08-25 09:00:00", "+15550000000", duration_s=30, originated=0, answered=1)],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    row = [l for l in events_only(content).splitlines() if "| phone |" in l][0]
    assert split_row(row)[3] == "+15550000000"


def test_phone_calls_missed_answered_outgoing_detail_and_direction(paths):
    make_calls_snapshot(
        paths["imessage_dir"], "calls-1.json",
        [
            call_rec(1, "2026-08-25 09:00:00", "+15551111111", duration_s=125, originated=0, answered=1),  # answered incoming
            call_rec(2, "2026-08-25 09:05:00", "+15552222222", duration_s=0, originated=0, answered=0),    # missed incoming
            call_rec(3, "2026-08-25 09:10:00", "+15553333333", duration_s=180, originated=1, answered=1),  # outgoing answered
            call_rec(4, "2026-08-25 09:15:00", "+15554444444", duration_s=0, originated=1, answered=0),    # outgoing unanswered -> missed
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = {split_row(l)[3]: l for l in events_only(content).splitlines() if "| phone |" in l}
    r1 = split_row(rows["+15551111111"])
    assert r1[2] == "incoming" and r1[4] == "answered · 2m"
    r2 = split_row(rows["+15552222222"])
    assert r2[2] == "incoming" and r2[4] == "missed"
    r3 = split_row(rows["+15553333333"])
    assert r3[2] == "outgoing" and r3[4] == "outgoing · 3m"
    r4 = split_row(rows["+15554444444"])
    assert r4[2] == "outgoing" and r4[4] == "missed"


def test_phone_calls_facetime_suffix(paths):
    make_calls_snapshot(
        paths["imessage_dir"], "calls-1.json",
        [
            call_rec(1, "2026-08-25 09:00:00", "+15551111111", duration_s=60, originated=0, answered=1, call_type=8),   # FaceTime video
            call_rec(2, "2026-08-25 09:05:00", "+15552222222", duration_s=60, originated=0, answered=1, call_type=16),  # FaceTime audio
            call_rec(3, "2026-08-25 09:10:00", "+15553333333", duration_s=60, originated=0, answered=1, call_type=1),   # plain phone
        ],
    )
    days = [date(2026, 8, 25)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4))
    assert rc == 0
    content = read_day(paths, date(2026, 8, 25))
    rows = {split_row(l)[3]: l for l in events_only(content).splitlines() if "| phone |" in l}
    assert split_row(rows["+15551111111"])[4] == "answered · 1m (FaceTime)"
    assert split_row(rows["+15552222222"])[4] == "answered · 1m (FaceTime)"
    assert split_row(rows["+15553333333"])[4] == "answered · 1m"


def test_phone_calls_local_day_attribution_across_utc_boundary(paths):
    # 23:50 EDT local time -- must stay on the LOCAL day even though it's
    # already past midnight UTC, same invariant as the existing whatsapp/
    # chrome late-night tests.
    make_calls_snapshot(
        paths["imessage_dir"], "calls-1.json",
        [call_rec(1, "2026-08-25 23:50:00", "+15551111111", duration_s=60, originated=0, answered=1)],
    )
    days = [date(2026, 8, 25), date(2026, 8, 26)]
    rc = ae.run(paths, days, local_epoch(2026, 8, 26, 12, 0, 0, -4))
    assert rc == 0
    d25 = read_day(paths, date(2026, 8, 25))
    d26 = read_day(paths, date(2026, 8, 26))
    assert "| phone |" in events_only(d25)
    assert "23:50" in d25
    assert "| phone |" not in events_only(d26)


def test_phone_call_sub_minute_duration_shown_in_seconds():
    assert ae.format_phone_call_detail(answered=1, originated=0, duration_s=45, call_type=1) == "answered · 45s"
    assert ae.format_phone_call_detail(answered=1, originated=1, duration_s=60, call_type=1) == "outgoing · 1m"


def test_phone_calls_third_party_voip_provider_skipped(paths):
    make_calls_snapshot(
        paths["imessage_dir"], "calls-20260825T090000.json",
        [call_rec(601, "2026-08-25 09:00:00", "+15551234567", duration_s=120, originated=0, answered=1,
                  provider="net.whatsapp.WhatsApp"),
         call_rec(602, "2026-08-25 09:30:00", "+15551234567", duration_s=120, originated=0, answered=1)],
    )
    days = [date(2026, 8, 25)]
    assert ae.run(paths, days, local_epoch(2026, 8, 25, 12, 0, 0, -4)) == 0
    rows = [l for l in events_only(read_day(paths, date(2026, 8, 25))).splitlines() if "| phone |" in l]
    assert len(rows) == 1 and rows[0].startswith("| 09:30")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))