"""Tests for calendar-export.py. Run with:
    python3 -m pytest test_calendar_export.py -v
    TZ=UTC python3 -m pytest test_calendar_export.py -v

The HTTP layer (token refresh + calendarList + events.list) is mocked via the
http_post/http_get injection points in run()/get_access_token()/
fetch_calendar_list()/fetch_events() -- no real network call and no real
~/.claude/tokens.env read for most tests. Uses a real temp file for the
atomic-write / untouched-on-failure checks (no fake filesystem), since that's
the actual invariant under test.
"""
import importlib.util
import io
import json
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta

import pytest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "calendar-export.py")
spec = importlib.util.spec_from_file_location("calendar_export", MODULE_PATH)
ce = importlib.util.module_from_spec(spec)
sys.modules["calendar_export"] = ce
spec.loader.exec_module(ce)

FIXED_NOW = datetime(2026, 8, 27, 10, 32, 0, tzinfo=ce.NY_TZ)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens.env"
    path.write_text(
        "GOOGLE_TASKS_CLIENT_ID=fake-client-id\n"
        "GOOGLE_TASKS_CLIENT_SECRET=fake-secret\n"
        "GOOGLE_CALENDAR_REFRESH_TOKEN=fake-refresh-token\n"
    )
    return str(path)


def fake_http_post(url, data):
    return json.dumps({"access_token": "fake-access-token"}).encode()


def calendar_list_items(personal_ids=("oliverullman@gmail.com",)):
    items = [{"id": cid, "accessRole": "owner"} for cid in personal_ids]
    items.append({"id": ce.WORK_CALENDAR_ID, "accessRole": "freeBusyReader"})
    return items


def make_http_get(per_calendar, personal_ids=("oliverullman@gmail.com",)):
    """Serve calendarList, then per-calendar events dispatched on the calendar
    id in the request URL (urlencoded, so '@' becomes '%40')."""
    def _http_get(url, access_token):
        assert access_token == "fake-access-token"
        if "calendarList" in url:
            return json.dumps({"items": calendar_list_items(personal_ids)}).encode()
        for calendar_id, events in per_calendar.items():
            if urllib.parse.quote(calendar_id) in url:
                return json.dumps({"items": events}).encode()
        raise AssertionError(f"unexpected calendar in url: {url}")
    return _http_get


def timed_event(start_iso, end_iso, summary=None, **extra):
    ev = {"start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}
    if summary is not None:
        ev["summary"] = summary
    ev.update(extra)
    return ev


def all_day_event(date_str, summary=None):
    ev = {"start": {"date": date_str}, "end": {"date": date_str}}
    if summary is not None:
        ev["summary"] = summary
    return ev


def rows_of(personal, work, local_date=date(2026, 8, 27)):
    return ce.render_rows(ce.merge_events(personal, work), local_date)


# --------------------------------------------------------------------------
# day_window: DST-transition-day correctness
# --------------------------------------------------------------------------


def test_day_window_regular_day():
    time_min, time_max = ce.day_window(date(2026, 8, 25))
    start = datetime.fromisoformat(time_min)
    end = datetime.fromisoformat(time_max)
    assert start.hour == 0 and start.minute == 0
    assert end.hour == 0 and end.minute == 0
    assert (end - start) == timedelta(hours=24)
    assert start.date() == date(2026, 8, 25)
    assert end.date() == date(2026, 8, 26)


def test_day_window_spring_forward_dst_day():
    # 2026-03-08 is the US DST "spring forward" day for America/New_York
    # (clocks jump 2am -> 3am), so the wall-clock day is only 23 real hours.
    time_min, time_max = ce.day_window(date(2026, 3, 8))
    start = datetime.fromisoformat(time_min)
    end = datetime.fromisoformat(time_max)
    assert start.date() == date(2026, 3, 8)
    assert end.date() == date(2026, 3, 9)
    # The wall-clock window is still midnight-to-midnight even though the
    # elapsed real duration is 23 hours, not 24 -- proves this is built from
    # two independently localized wall-clock boundaries, not date + 24h.
    assert (end - start) == timedelta(hours=23)
    assert start.utcoffset() != end.utcoffset()


# --------------------------------------------------------------------------
# personal_calendar_ids: which calendars count as "his own"
# --------------------------------------------------------------------------


def test_personal_calendar_ids_keeps_owned_and_writable():
    items = [
        {"id": "oliverullman@gmail.com", "accessRole": "owner"},
        {"id": "family@group.calendar.google.com", "accessRole": "owner"},
        {"id": "republic@group.calendar.google.com", "accessRole": "writer"},
    ]
    assert ce.personal_calendar_ids(items) == [
        "oliverullman@gmail.com",
        "family@group.calendar.google.com",
        "republic@group.calendar.google.com",
    ]


def test_personal_calendar_ids_drops_read_only_subscriptions():
    # Holidays and football fixtures are not appointments he attends; a row
    # from one would be counted as occupied time.
    items = [
        {"id": "oliverullman@gmail.com", "accessRole": "owner"},
        {"id": "en.usa#holiday@group.v.calendar.google.com", "accessRole": "reader"},
        {"id": "brighton@group.calendar.google.com", "accessRole": "reader"},
    ]
    assert ce.personal_calendar_ids(items) == ["oliverullman@gmail.com"]


def test_personal_calendar_ids_never_returns_the_work_calendar():
    items = [{"id": ce.WORK_CALENDAR_ID, "accessRole": "owner"}]
    assert ce.personal_calendar_ids(items) == []


# --------------------------------------------------------------------------
# should_include: the brief's exclusion contract
# --------------------------------------------------------------------------


def test_all_day_event_is_excluded():
    assert ce.should_include(all_day_event("2026-08-27", summary="PTO")) is False


def test_declined_invitation_is_excluded():
    event = timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Standup",
        attendees=[
            {"email": "other@example.com", "responseStatus": "accepted"},
            {"email": "oliverullman@gmail.com", "self": True, "responseStatus": "declined"},
        ],
    )
    assert ce.should_include(event) is False


def test_free_transparent_event_is_excluded():
    event = timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Reminder",
        transparency="transparent",
    )
    assert ce.should_include(event) is False


def test_cancelled_event_is_excluded():
    event = timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Gone",
        status="cancelled",
    )
    assert ce.should_include(event) is False


def test_accepted_event_is_included():
    event = timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Standup",
        attendees=[{"email": "oliverullman@gmail.com", "self": True, "responseStatus": "accepted"}],
    )
    assert ce.should_include(event) is True


def test_unanswered_invite_is_included():
    # An unanswered invite usually still gets attended.
    event = timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Standup",
        attendees=[{"email": "oliverullman@gmail.com", "self": True, "responseStatus": "needsAction"}],
    )
    assert ce.should_include(event) is True


def test_someone_elses_declined_response_does_not_exclude():
    event = timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Standup",
        attendees=[
            {"email": "other@example.com", "responseStatus": "declined"},
            {"email": "oliverullman@gmail.com", "self": True, "responseStatus": "accepted"},
        ],
    )
    assert ce.should_include(event) is True


def test_excluded_event_never_reaches_a_row():
    personal = [
        timed_event("2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00", summary="Real"),
        all_day_event("2026-08-27", summary="PTO"),
    ]
    assert rows_of(personal, []) == [("09:00", "10:00", "Real", "personal")]


# --------------------------------------------------------------------------
# render_rows: labels, tags, ordering
# --------------------------------------------------------------------------


def test_tentative_response_is_marked_in_the_title():
    event = timed_event(
        "2026-08-27T15:00:00-04:00", "2026-08-27T16:00:00-04:00", summary="Design review",
        attendees=[{"email": "oliverullman@gmail.com", "self": True, "responseStatus": "tentative"}],
    )
    assert rows_of([event], []) == [("15:00", "16:00", "Design review (tentative)", "personal")]


def test_summary_with_pipe_is_escaped():
    events = [timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T09:30:00-04:00", summary="Design | Review",
    )]
    assert rows_of(events, []) == [("09:00", "09:30", "Design \\| Review", "personal")]


def test_summary_with_newline_is_collapsed():
    events = [timed_event(
        "2026-08-27T09:00:00-04:00", "2026-08-27T09:30:00-04:00", summary="Line one\nLine two",
    )]
    assert rows_of(events, []) == [("09:00", "09:30", "Line one Line two", "personal")]


def test_event_spanning_midnight_is_clamped_to_local_start():
    # Started 22:00 yesterday, runs to 01:00 today -- must clamp to 00:00.
    events = [timed_event("2026-08-26T22:00:00-04:00", "2026-08-27T01:00:00-04:00", summary="Late")]
    assert rows_of(events, []) == [("00:00", "01:00", "Late", "personal")]


def test_rows_are_sorted_by_start_time_across_calendars():
    personal = [timed_event("2026-08-27T20:30:00-04:00", "2026-08-27T22:30:00-04:00", summary="Jazz")]
    work = [timed_event("2026-08-27T09:00:00-04:00", "2026-08-27T10:00:00-04:00")]
    assert [r[0] for r in rows_of(personal, work)] == ["09:00", "20:30"]


# --------------------------------------------------------------------------
# merge_events: mirrors dropped, real work meetings kept
# --------------------------------------------------------------------------


def test_work_mirrors_of_a_personal_event_are_dropped():
    # The real failing case from 2026-08-27: a personal therapy session
    # mirrored onto the work calendar twice, untitled, and credited as work.
    personal = [timed_event(
        "2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00",
        summary="Talkspace therapy session",
    )]
    work = [
        timed_event("2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00"),
        timed_event("2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00"),
    ]
    assert rows_of(personal, work) == [
        ("08:15", "09:00", "Talkspace therapy session", "personal"),
    ]


def test_work_only_block_is_kept_as_an_untitled_work_row():
    work = [timed_event("2026-08-27T17:00:00-04:00", "2026-08-27T18:00:00-04:00")]
    assert rows_of([], work) == [("17:00", "18:00", "(busy)", "work")]


def test_duplicate_work_only_blocks_collapse_to_one_row():
    work = [
        timed_event("2026-08-27T17:00:00-04:00", "2026-08-27T18:00:00-04:00"),
        timed_event("2026-08-27T17:00:00-04:00", "2026-08-27T18:00:00-04:00"),
    ]
    assert rows_of([], work) == [("17:00", "18:00", "(busy)", "work")]


def test_mirror_written_in_another_timezone_still_matches():
    # A work-calendar copy expressed in Pacific offsets is the same instant,
    # so it must still be recognised as a mirror.
    personal = [timed_event(
        "2026-08-27T17:00:00-04:00", "2026-08-27T18:00:00-04:00", summary="1:1",
    )]
    work = [timed_event("2026-08-27T14:00:00-07:00", "2026-08-27T15:00:00-07:00")]
    assert rows_of(personal, work) == [("17:00", "18:00", "1:1", "personal")]


def test_work_block_at_a_free_slot_is_kept_alongside_personal_events():
    personal = [timed_event(
        "2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00", summary="Therapy",
    )]
    work = [timed_event("2026-08-27T17:00:00-04:00", "2026-08-27T18:00:00-04:00")]
    assert rows_of(personal, work) == [
        ("08:15", "09:00", "Therapy", "personal"),
        ("17:00", "18:00", "(busy)", "work"),
    ]


def test_same_event_on_two_personal_calendars_appears_once():
    duplicate = timed_event(
        "2026-08-27T12:00:00-04:00", "2026-08-27T13:00:00-04:00", summary="Lunch with Esme",
    )
    assert rows_of([duplicate, dict(duplicate)], []) == [
        ("12:00", "13:00", "Lunch with Esme", "personal"),
    ]


def test_two_different_personal_events_in_the_same_slot_both_survive():
    # Same time, different titles -- a genuine double-booking, not a mirror.
    personal = [
        timed_event("2026-08-27T12:00:00-04:00", "2026-08-27T13:00:00-04:00", summary="Lunch"),
        timed_event("2026-08-27T12:00:00-04:00", "2026-08-27T13:00:00-04:00", summary="Dentist"),
    ]
    assert len(rows_of(personal, [])) == 2


def test_declined_personal_event_does_not_mask_a_real_work_meeting():
    # He declined the personal event, so the overlapping work block is not its
    # mirror -- dropping it would erase a real meeting.
    personal = [timed_event(
        "2026-08-27T11:00:00-04:00", "2026-08-27T12:00:00-04:00", summary="Skipped",
        attendees=[{"email": "oliverullman@gmail.com", "self": True, "responseStatus": "declined"}],
    )]
    work = [timed_event("2026-08-27T11:00:00-04:00", "2026-08-27T12:00:00-04:00")]
    assert rows_of(personal, work) == [("11:00", "12:00", "(busy)", "work")]


# --------------------------------------------------------------------------
# render_markdown: exact output shape
# --------------------------------------------------------------------------


def test_render_markdown_matches_expected_shape():
    rows = [
        ("08:15", "09:00", "Talk with Sam", "personal"),
        ("09:30", "10:00", "(busy)", "work"),
    ]
    content = ce.render_markdown(date(2026, 8, 27), rows, now=FIXED_NOW)
    expected = (
        "---\n"
        "updated: 2026-08-27T10:32:00-04:00\n"
        "generated: 2026-08-27T10:32:00-04:00\n"
        "---\n"
        "# Calendar — Thursday 2026-08-27\n"
        "\n"
        "Personal calendars on oliverullman@gmail.com, plus any work-calendar "
        "(oliver.ullman@rubrik.com) block with no personal counterpart. "
        "Timezone America/New_York.\n"
        "\n"
        "| Start | End | Event | Calendar |\n"
        "|-------|-----|-------|----------|\n"
        "| 08:15 | 09:00 | Talk with Sam | personal |\n"
        "| 09:30 | 10:00 | (busy) | work |\n"
        "\n"
        "Notes:\n"
        "- `personal` rows are appointments off Oliver's own calendars; `work` rows are "
        "opaque blocks from the Rubrik free/busy share, which exposes no titles.\n"
        "- Work-calendar mirrors of personal appointments are dropped, so each event appears once.\n"
    )
    assert content == expected


@pytest.mark.parametrize("key", ["updated", "generated"])
def test_timestamp_carries_an_offset_whatever_the_process_timezone(key):
    # Defaulted (not injected) now, so this proves the module's own clock is
    # Eastern-aware even when the test runner is under TZ=UTC.
    content = ce.render_markdown(date(2026, 8, 27), [])
    line = [l for l in content.splitlines() if l.startswith(f"{key}: ")][0]
    stamp = datetime.fromisoformat(line[len(key) + 2:])
    assert stamp.utcoffset() is not None
    assert stamp.utcoffset() == datetime.now(ce.NY_TZ).utcoffset()


def test_generated_key_is_written_even_though_updated_gets_rewritten():
    # The vault plugin owns `updated:`; `generated:` is the copy that keeps
    # its offset, so it must always be present.
    content = ce.render_markdown(date(2026, 8, 27), [], now=FIXED_NOW)
    assert "generated: 2026-08-27T10:32:00-04:00" in content


def test_empty_day_renders_no_rows_note():
    content = ce.render_markdown(date(2026, 8, 27), [], now=FIXED_NOW)
    assert "| Start | End | Event | Calendar |" in content
    assert "| 08:15" not in content
    assert "- No qualifying events on either calendar today." in content


# --------------------------------------------------------------------------
# run(): end-to-end with mocked HTTP, real temp file
# --------------------------------------------------------------------------


def test_run_merges_every_personal_calendar_with_the_work_share(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    http_get = make_http_get(
        {
            "oliverullman@gmail.com": [timed_event(
                "2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00",
                summary="Talkspace therapy session",
            )],
            "family@group.calendar.google.com": [timed_event(
                "2026-08-27T19:00:00-04:00", "2026-08-27T20:00:00-04:00", summary="Soccer",
            )],
            ce.WORK_CALENDAR_ID: [
                timed_event("2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00"),
                timed_event("2026-08-27T08:15:00-04:00", "2026-08-27T09:00:00-04:00"),
                timed_event("2026-08-27T19:00:00-04:00", "2026-08-27T20:00:00-04:00"),
                timed_event("2026-08-27T17:00:00-04:00", "2026-08-27T18:00:00-04:00"),
            ],
        },
        personal_ids=("oliverullman@gmail.com", "family@group.calendar.google.com"),
    )
    ce.run(
        local_date=date(2026, 8, 27),
        tokens_path=tokens_file,
        output_path=output_path,
        overrides_path=str(tmp_path / "no-overrides.json"),
        http_post=fake_http_post,
        http_get=http_get,
        now=FIXED_NOW,
    )
    with open(output_path) as f:
        written = f.read()
    event_rows = [l for l in written.splitlines() if l.startswith("| 0") or l.startswith("| 1")]
    assert event_rows == [
        "| 08:15 | 09:00 | Talkspace therapy session | personal |",
        "| 17:00 | 18:00 | (busy) | work |",
        "| 19:00 | 20:00 | Soccer | personal |",
    ]


def test_run_writes_the_file_it_returns(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    content = ce.run(
        local_date=date(2026, 8, 27),
        tokens_path=tokens_file,
        output_path=output_path,
        overrides_path=str(tmp_path / "no-overrides.json"),
        http_post=fake_http_post,
        http_get=make_http_get({
            "oliverullman@gmail.com": [timed_event(
                "2026-08-27T12:15:00-04:00", "2026-08-27T12:45:00-04:00", summary="Call",
            )],
            ce.WORK_CALENDAR_ID: [],
        }),
        now=FIXED_NOW,
    )
    with open(output_path) as f:
        assert f.read() == content
    assert "# Calendar — Thursday 2026-08-27" in content


def test_run_empty_day_writes_note_line(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    ce.run(
        local_date=date(2026, 8, 27),
        tokens_path=tokens_file,
        output_path=output_path,
        overrides_path=str(tmp_path / "no-overrides.json"),
        http_post=fake_http_post,
        http_get=make_http_get({"oliverullman@gmail.com": [], ce.WORK_CALENDAR_ID: []}),
        now=FIXED_NOW,
    )
    with open(output_path) as f:
        assert "- No qualifying events on either calendar today." in f.read()


def test_api_error_leaves_existing_file_untouched_and_exits_nonzero(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    original = "# Calendar — Wednesday 2026-08-26\n\nold content\n"
    with open(output_path, "w") as f:
        f.write(original)
    original_mtime = os.path.getmtime(output_path)

    def failing_http_get(url, access_token):
        raise ce.urllib.error.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(b"boom"))

    with pytest.raises(ce.CalendarExportError):
        ce.run(
            local_date=date(2026, 8, 27),
            tokens_path=tokens_file,
            output_path=output_path,
            http_post=fake_http_post,
            http_get=failing_http_get,
        )

    with open(output_path) as f:
        assert f.read() == original
    assert os.path.getmtime(output_path) == original_mtime
    # No leftover tmp file from a partial atomic write.
    assert not os.path.exists(str(tmp_path / ".calendar-today.md.tmp"))


def test_auth_failure_leaves_existing_file_untouched(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    original = "# Calendar — Wednesday 2026-08-26\n\nold content\n"
    with open(output_path, "w") as f:
        f.write(original)

    def failing_http_post(url, data):
        raise ce.urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"nope"))

    with pytest.raises(ce.CalendarExportError):
        ce.run(
            local_date=date(2026, 8, 27),
            tokens_path=tokens_file,
            output_path=output_path,
            http_post=failing_http_post,
            http_get=make_http_get({"oliverullman@gmail.com": [], ce.WORK_CALENDAR_ID: []}),
        )

    with open(output_path) as f:
        assert f.read() == original


# --------------------------------------------------------------------------
# main(): exit code on failure
# --------------------------------------------------------------------------


def test_main_exits_nonzero_on_missing_tokens_key(tmp_path, monkeypatch, capsys):
    bad_tokens = tmp_path / "tokens.env"
    bad_tokens.write_text("GOOGLE_TASKS_CLIENT_ID=only-this-one\n")
    monkeypatch.setattr(ce, "TOKENS_PATH", str(bad_tokens))
    # Also redirect OUTPUT_PATH so a future refactor that reorders checks
    # can never make this test write the real vault file.
    monkeypatch.setattr(ce, "OUTPUT_PATH", str(tmp_path / "calendar-today.md"))
    monkeypatch.setattr(sys, "argv", ["calendar-export.py"])
    with pytest.raises(SystemExit) as exc_info:
        ce.main()
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "GOOGLE_TASKS_CLIENT_SECRET" in captured.err


# --- overrides: correcting a meeting that overran its scheduled end ---

D = date(2026, 8, 31)
ROWS = [
    ("10:00", "10:30", "(busy)", "work"),
    ("13:45", "14:00", "(busy)", "work"),
    ("18:00", "19:00", "Soccer", "personal"),
]


def ov(entries):
    return {"2026-08-31": {"overrides": entries}}


def add(entries):
    return {"2026-08-31": {"add": entries}}


def test_missing_overrides_file_is_not_an_error(tmp_path):
    assert ce.load_overrides(str(tmp_path / "nope.json")) == {}


def test_no_overrides_leaves_rows_untouched():
    assert ce.apply_overrides(ROWS, D, {}) == ROWS


def test_override_extends_the_end_time():
    out = ce.apply_overrides(ROWS, D, ov([{"start": "13:45", "end": "14:07"}]))
    assert ("13:45", "14:07", "(busy)", "work") in out
    assert ("13:45", "14:00", "(busy)", "work") not in out


def test_override_preserves_the_other_rows():
    out = ce.apply_overrides(ROWS, D, ov([{"start": "13:45", "end": "14:07"}]))
    assert ("10:00", "10:30", "(busy)", "work") in out
    assert ("18:00", "19:00", "Soccer", "personal") in out
    assert len(out) == len(ROWS)


def test_override_for_another_date_does_not_apply():
    out = ce.apply_overrides(ROWS, D, {"2026-08-30": {"overrides": [{"start": "13:45", "end": "14:07"}]}})
    assert out == ROWS


def test_override_can_relabel():
    out = ce.apply_overrides(ROWS, D, ov([{"start": "13:45", "label": "Standup"}]))
    assert ("13:45", "14:00", "Standup", "work") in out


def test_override_matching_nothing_warns_but_keeps_rows(capsys):
    out = ce.apply_overrides(ROWS, D, ov([{"start": "09:15", "end": "09:30"}]))
    assert out == ROWS
    assert "matched no event" in capsys.readouterr().err


def test_override_without_start_is_rejected():
    with pytest.raises(ce.CalendarExportError):
        ce.apply_overrides(ROWS, D, ov([{"end": "14:07"}]))


def test_malformed_overrides_file_raises(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text("{not json")
    with pytest.raises(ce.CalendarExportError):
        ce.load_overrides(str(p))


def test_non_object_overrides_file_raises(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text('["nope"]')
    with pytest.raises(ce.CalendarExportError):
        ce.load_overrides(str(p))


def test_bare_list_for_a_date_is_rejected(tmp_path):
    """The pre-'add' format; a silent no-op would drop real corrections."""
    p = tmp_path / "overrides.json"
    p.write_text('{"2026-08-31": [{"start": "13:45", "end": "14:07"}]}')
    with pytest.raises(ce.CalendarExportError):
        ce.load_overrides(str(p))


def test_unknown_key_for_a_date_is_rejected(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text('{"2026-08-31": {"ovrrides": []}}')
    with pytest.raises(ce.CalendarExportError):
        ce.load_overrides(str(p))


def test_overridden_rows_stay_sorted():
    rows = [("13:45", "14:00", "(busy)", "work"), ("14:05", "14:30", "Later", "personal")]
    out = ce.apply_overrides(rows, D, ov([{"start": "13:45", "end": "14:07"}]))
    assert [r[0] for r in out] == sorted(r[0] for r in out)


def test_override_reaches_the_rendered_markdown():
    md = ce.render_markdown(D, ce.apply_overrides(
        ROWS, D, ov([{"start": "13:45", "end": "14:07"}])), now=FIXED_NOW)
    assert "| 13:45 | 14:07 |" in md
    assert "| 13:45 | 14:00 |" not in md


# --- added marks: work with no calendar event and no prompts ---

def test_added_mark_becomes_a_row():
    out = ce.apply_overrides(ROWS, D, add([
        {"start": "14:40", "end": "15:10", "label": "Working"}]))
    assert ("14:40", "15:10", "Working", "personal") in out
    assert len(out) == len(ROWS) + 1


def test_added_mark_is_sorted_into_place():
    out = ce.apply_overrides(ROWS, D, add([
        {"start": "11:00", "end": "11:30", "label": "Working"}]))
    assert [r[0] for r in out] == ["10:00", "11:00", "13:45", "18:00"]


def test_added_mark_end_now_uses_the_clock():
    now = datetime(2026, 8, 31, 15, 3, 0, tzinfo=ce.NY_TZ)
    out = ce.apply_overrides(ROWS, D, add([
        {"start": "14:40", "end": "now", "label": "Working"}]), now=now)
    assert ("14:40", "15:03", "Working", "personal") in out


def test_added_mark_end_now_grows_with_later_exports():
    spec = add([{"start": "14:40", "end": "now", "label": "Working"}])
    early = ce.apply_overrides(ROWS, D, spec, now=datetime(2026, 8, 31, 15, 3, tzinfo=ce.NY_TZ))
    later = ce.apply_overrides(ROWS, D, spec, now=datetime(2026, 8, 31, 16, 21, tzinfo=ce.NY_TZ))
    assert ("14:40", "15:03", "Working", "personal") in early
    assert ("14:40", "16:21", "Working", "personal") in later


def test_added_mark_can_set_source():
    out = ce.apply_overrides(ROWS, D, add([
        {"start": "14:40", "end": "15:10", "label": "Working", "source": "work"}]))
    assert ("14:40", "15:10", "Working", "work") in out


def test_added_mark_missing_a_field_is_rejected():
    for bad in ({"end": "15:10", "label": "W"}, {"start": "14:40", "label": "W"},
                {"start": "14:40", "end": "15:10"}):
        with pytest.raises(ce.CalendarExportError):
            ce.apply_overrides(ROWS, D, add([bad]))


def test_added_mark_reaches_the_rendered_markdown():
    md = ce.render_markdown(D, ce.apply_overrides(
        ROWS, D, add([{"start": "14:40", "end": "now", "label": "Working"}]),
        now=FIXED_NOW), now=FIXED_NOW)
    assert "| 14:40 | 10:32 | Working | personal |" in md


def test_override_and_add_compose():
    out = ce.apply_overrides(ROWS, D, {"2026-08-31": {
        "overrides": [{"start": "13:45", "end": "14:07"}],
        "add": [{"start": "14:40", "end": "15:10", "label": "Working"}]}})
    assert ("13:45", "14:07", "(busy)", "work") in out
    assert ("14:40", "15:10", "Working", "personal") in out


# --- derived browsing blocks from chrome-work-blocks.py ---

def wb(tmp_path, payload):
    p = tmp_path / "chrome-work-blocks.json"
    p.write_text(json.dumps(payload))
    return str(p)


def test_missing_work_blocks_cache_is_not_an_error(tmp_path):
    assert ce.load_work_blocks(str(tmp_path / "nope.json"), D) == []


def test_work_blocks_become_work_tagged_rows(tmp_path):
    p = wb(tmp_path, {"date": "2026-08-31",
                      "blocks": [{"start": "14:03", "end": "15:13", "minutes": 70}]})
    assert ce.load_work_blocks(p, D) == [("14:03", "15:13", ce.BROWSING_LABEL, "work")]


def test_work_blocks_are_never_tagged_personal(tmp_path):
    """A personal row is not credited as working time, which would defeat these."""
    p = wb(tmp_path, {"date": "2026-08-31", "blocks": [{"start": "14:03", "end": "15:13"}]})
    assert all(r[3] == "work" for r in ce.load_work_blocks(p, D))


def test_work_blocks_for_another_day_are_ignored(tmp_path):
    p = wb(tmp_path, {"date": "2026-08-30", "blocks": [{"start": "14:03", "end": "15:13"}]})
    assert ce.load_work_blocks(p, D) == []


def test_empty_blocks_list_is_fine(tmp_path):
    assert ce.load_work_blocks(wb(tmp_path, {"date": "2026-08-31", "blocks": []}), D) == []


def test_malformed_work_blocks_cache_raises(tmp_path):
    p = tmp_path / "b.json"; p.write_text("{not json")
    with pytest.raises(ce.CalendarExportError):
        ce.load_work_blocks(str(p), D)


def test_block_missing_an_end_raises(tmp_path):
    p = wb(tmp_path, {"date": "2026-08-31", "blocks": [{"start": "14:03"}]})
    with pytest.raises(ce.CalendarExportError):
        ce.load_work_blocks(p, D)


def test_run_merges_work_blocks_into_the_table(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    blocks = wb(tmp_path, {"date": "2026-08-27",
                           "blocks": [{"start": "09:00", "end": "09:30"}]})
    ce.run(
        local_date=date(2026, 8, 27),
        tokens_path=tokens_file,
        output_path=output_path,
        overrides_path=str(tmp_path / "no-overrides.json"),
        work_blocks_path=blocks,
        http_post=fake_http_post,
        http_get=make_http_get({"oliverullman@gmail.com": [], ce.WORK_CALENDAR_ID: []}),
        now=FIXED_NOW,
    )
    with open(output_path) as f:
        written = f.read()
    assert f"| 09:00 | 09:30 | {ce.BROWSING_LABEL} | work |" in written


def test_work_blocks_sort_in_among_real_events(tokens_file, tmp_path):
    output_path = str(tmp_path / "calendar-today.md")
    blocks = wb(tmp_path, {"date": "2026-08-27",
                           "blocks": [{"start": "13:00", "end": "13:30"}]})
    ce.run(
        local_date=date(2026, 8, 27),
        tokens_path=tokens_file,
        output_path=output_path,
        overrides_path=str(tmp_path / "no-overrides.json"),
        work_blocks_path=blocks,
        http_post=fake_http_post,
        http_get=make_http_get({"oliverullman@gmail.com": [timed_event(
            "2026-08-27T12:00:00-04:00", "2026-08-27T12:30:00-04:00", summary="Standup")],
            ce.WORK_CALENDAR_ID: []}),
        now=FIXED_NOW,
    )
    with open(output_path) as f:
        lines = [l for l in f.read().splitlines() if l.startswith("| 1")]
    assert lines[0].startswith("| 12:00") and "13:00" in lines[1]


# --- per-person profile (makes the pipeline usable by someone other than its author) ---

def test_missing_profile_falls_back_to_defaults(tmp_path):
    p = ce.load_profile(str(tmp_path / "nope.json"))
    assert p["timezone"] and p["personal_account"] and p["work_calendar_id"]


def test_profile_overrides_identity(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text(json.dumps({"timezone": "Europe/London",
                             "personal_account": "her@gmail.com",
                             "work_calendar_id": "her@work.com"}))
    p = ce.load_profile(str(f))
    assert p == {"timezone": "Europe/London", "personal_account": "her@gmail.com",
                 "work_calendar_id": "her@work.com"}


def test_partial_profile_keeps_defaults_for_the_rest(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text(json.dumps({"timezone": "Europe/London"}))
    p = ce.load_profile(str(f))
    assert p["timezone"] == "Europe/London" and p["personal_account"]


def test_profile_with_a_typo_key_raises(tmp_path):
    """A silently ignored key means her answer never took effect."""
    f = tmp_path / "profile.json"
    f.write_text(json.dumps({"timezon": "Europe/London"}))
    with pytest.raises(ce.CalendarExportError):
        ce.load_profile(str(f))


def test_malformed_profile_raises(tmp_path):
    f = tmp_path / "profile.json"; f.write_text("{not json")
    with pytest.raises(ce.CalendarExportError):
        ce.load_profile(str(f))
