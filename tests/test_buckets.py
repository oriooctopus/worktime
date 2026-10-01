#!/usr/bin/env python3
"""Tests for the work buckets: weighted meetings, special time and its target.

Pins the arithmetic the menu and the dashboard both read, because every figure
there is derived and nothing on screen would say if one of these drifted:

  * a meeting is credited at two thirds PER MINUTE, and a minute holding real
    work evidence inside the call is credited in full -- one call can blend;
  * the meeting app in front is the call, not evidence, and which app that is
    comes from the meeting's own record;
  * special time is cut out of main, exactly, so the two totals add up;
  * the special target is a total over its span and a newer one replaces it;
  * a meeting routed to Main pauses special and it resumes afterwards;
  * a past day is rebuilt with the weighting by backfill.

Run: pytest tests/test_buckets.py
"""

import importlib.util
import json
import os
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-03-04"
ZOOM, SLACK, ZED = "us.zoom.xos", "com.tinyspeck.slackmacgap", "dev.zed.Zed"


def at(h, m, s=0, day=4):
    return datetime(2026, 3, day, h, m, s, tzinfo=wp.LOCAL)


class World:
    """One fake day: the inputs the snapshot reads, and the files it writes."""

    def __init__(self, tmp, monkeypatch):
        self.tmp = str(tmp)
        self.mp = monkeypatch
        self.now = at(23, 0)
        self.prompts: dict[str, list[datetime]] = {}
        self.focus: dict[str, list[tuple[datetime, str]]] = {}
        p = lambda n: os.path.join(self.tmp, n)  # noqa: E731
        for name, f in (("STATE", ""), ("MARKS", "marks.jsonl"),
                        ("MEETINGS", "meetings.jsonl"),
                        ("SPECIAL_LOG", "special.jsonl"),
                        ("SPECIAL_TARGETS", "special-targets.jsonl"),
                        ("GOALS_FILE", "worktime-goals.json"),
                        ("SESSION_END", "session-end.json"),
                        ("MODEFILE", "mode.jsonl"),
                        ("LABELS", "labels.jsonl"),
                        ("VAULT_SNAPSHOT_DIR", "vault")):
            monkeypatch.setattr(wp, name, p(f) if f else self.tmp)
        monkeypatch.setattr(wp, "now_local", lambda: self.now)
        monkeypatch.setattr(wp, "prompts_for",
                            lambda d: self.prompts.get(d, []))
        monkeypatch.setattr(wp, "approvals_for", lambda d: [])
        monkeypatch.setattr(wp, "github_visits_for", lambda d: [])
        monkeypatch.setattr(wp, "notes_for", lambda d: [])
        monkeypatch.setattr(wp, "focus_for", self._focus_for)
        monkeypatch.setattr(wp, "events_for", self._events_for)
        monkeypatch.setattr(wp, "mode_timeline", lambda d: [(0, "focused")])
        monkeypatch.setattr(wp, "marks_for", lambda d, s=None: [])
        monkeypatch.setattr(wp, "slack_for", lambda d: [])
        monkeypatch.setattr(wp, "summarize_span", lambda d, a, b: "")
        monkeypatch.setattr(wp, "sessions_in", lambda d, a, b: [])
        monkeypatch.setattr(wp, "focus_apps", lambda d, a, b: [])
        monkeypatch.setattr(wp, "desktop_prompts_for", lambda d: [])
        monkeypatch.setattr(wp, "idle_cut", lambda d, p: [])
        monkeypatch.setattr(wp, "read_session_ends", lambda d=None: [])
        monkeypatch.setattr(wp, "activity_fingerprint", lambda d: "fp")
        monkeypatch.setattr(wp, "mode_now", lambda: "focused")

    def _focus_for(self, day, exclude_bundles=frozenset()):
        return sorted(t for t, b in self.focus.get(day, [])
                      if b not in exclude_bundles)

    def _events_for(self, day):
        return sorted(self.prompts.get(day, []) + self._focus_for(day))

    def meeting(self, start, end, app=None, day=DAY):
        rec = {"day": day, "start": start, "end": end, "title": "call"}
        if app:
            rec["app"] = app
        with open(wp.MEETINGS, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def special(self, on_at, off_at=None, day=4):
        ts = lambda t: t.timestamp()  # noqa: E731
        with open(wp.SPECIAL_LOG, "a") as fh:
            fh.write(json.dumps({"event": "on", "ts": ts(on_at)}) + "\n")
            if off_at:
                fh.write(json.dumps({"event": "off", "ts": ts(off_at)}) + "\n")

    def snapshot(self, day=DAY):
        wp.write_vault_snapshot(day, self._events_for(day))
        return json.load(open(wp.snapshot_path(day)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# ---------------------------------------------------------------- weighting

def test_meeting_minutes_blend_full_and_two_thirds(world):
    """Ten minutes of call, two of them holding real work: 2*60 + 8*60*2/3."""
    world.meeting(10 * 60, 10 * 60 + 10, app=ZOOM)
    world.prompts[DAY] = [at(10, 2, 30)]
    # Zoom in front is the call itself -- no credit. Zed in front is work.
    world.focus[DAY] = [(at(10, 4, 10), ZOOM), (at(10, 6, 10), ZED)]
    snap = world.snapshot()
    m = snap["buckets"]["meetings"]
    assert m["raw_sec"] == 600
    assert m["full_sec"] == 120
    assert m["credited_sec"] == 120 + 480 * 2 // 3  # 440
    assert snap["work_sec"] == 600
    assert snap["credited_sec"] == 440
    w = snap["worked"][0]
    assert w["bucket"] == "main" and w["weight"] == pytest.approx(440 / 600, abs=1e-3)
    assert w["meetings"][0]["raw_sec"] == 600
    assert w["meetings"][0]["credited_sec"] == 440
    # Which minutes earned full rate, so the dashboard can draw them.
    assert w["meetings"][0]["full_spans"] == [[602, 603], [606, 607]]


def test_host_app_is_the_one_left_out(world):
    """In a Slack huddle Slack is the call; Zoom in front then is work."""
    world.meeting(10 * 60, 10 * 60 + 4, app=SLACK)
    world.focus[DAY] = [(at(10, 1, 10), SLACK), (at(10, 2, 10), ZOOM)]
    snap = world.snapshot()
    m = snap["buckets"]["meetings"]
    # minute 10:01 Slack (host, weighted), 10:02 Zoom (work, full), 2 bare.
    assert m["raw_sec"] == 240 and m["full_sec"] == 60
    assert m["credited_sec"] == 60 + 180 * 2 // 3


def test_legacy_meeting_without_app_reads_as_zoom(world):
    world.meeting(10 * 60, 10 * 60 + 2)
    assert wp.meetings_for(DAY)[0]["app"] == wp.LEGACY_MEETING_APP == ZOOM


def test_start_meeting_records_the_host_app(world):
    wp.start_meeting("call", 600, SLACK)
    assert wp.meetings_for(DAY)[0]["app"] == SLACK


def test_no_meeting_means_no_discount(world):
    world.prompts[DAY] = [at(9, 0), at(9, 3), at(9, 6)]
    snap = world.snapshot()
    assert snap["credited_sec"] == snap["work_sec"] > 0
    assert snap["buckets"]["meetings"]["raw_sec"] == 0


# ------------------------------------------------------------------ special

def test_special_is_cut_out_of_main_exactly(world):
    world.prompts[DAY] = [at(10, m) for m in range(50, 60, 2)] + \
        [at(11, m) for m in range(0, 60, 2)] + [at(12, 0)]
    base = world.snapshot()
    world.special(at(11, 0), at(11, 30))
    snap = world.snapshot()
    assert snap["buckets"]["special"]["sec"] == 1800
    assert base["work_sec"] - snap["work_sec"] == 1800
    sp = snap["buckets"]["special"]["spans"][0]
    assert all(w["end_sec"] <= sp["start_sec"] or w["start_sec"] >= sp["end_sec"]
               for w in snap["worked"])
    # The two buckets add up to what main alone used to claim.
    assert snap["work_sec"] + snap["buckets"]["special"]["sec"] == base["work_sec"]


def test_open_special_runs_to_now_and_end_session_closes_it(world):
    world.now = at(14, 0)
    assert wp.set_special(True)["changed"]
    assert not wp.set_special(True)["changed"]          # idempotent
    world.now = at(14, 40)
    assert wp.special_sec_for(DAY) == 40 * 60
    # Ending the session closes it at the declared minute, not at now.
    wp.set_special(False, at(14, 30).timestamp())
    assert wp.special_sec_for(DAY) == 30 * 60
    world.now = at(18, 0)
    assert wp.special_sec_for(DAY) == 30 * 60


def test_special_span_across_midnight_splits_between_days(world):
    world.now = at(1, 0, day=5)
    world.special(at(23, 0), at(0, 30, day=5))
    assert wp.special_sec_for("2026-03-04") == 3600
    assert wp.special_sec_for("2026-03-05") == 1800


# ------------------------------------------------------------ special target

def test_target_is_a_total_over_the_span_and_needs_no_carryover(world):
    world.now = at(9, 0)
    wp.set_special_target(6, 3)                          # 6h across Mar 4-6
    world.special(at(10, 0), at(11, 0))                  # 1h on day 1
    world.now = at(12, 0, day=5)
    world.special(at(10, 0, day=5), at(12, 0, day=5))    # 2h on day 2
    t = wp.special_target_for("2026-03-05")
    assert t["target_sec"] == 6 * 3600 and t["days"] == 3
    assert t["done_sec"] == 3 * 3600 and t["remaining_sec"] == 3 * 3600
    # Day 1 saw only its own hour; day 4 is outside the span.
    assert wp.special_target_for(DAY)["done_sec"] == 3600
    world.now = at(9, 0, day=7)
    assert wp.special_target_for("2026-03-07") is None


def test_newer_target_replaces_and_does_not_resurrect_the_old_one(world):
    world.now = at(9, 0)
    wp.set_special_target(6, 7)
    world.now = at(9, 0, day=5)
    wp.set_special_target(2, 1)                          # today only
    assert wp.special_target_for("2026-03-04")["target_sec"] == 6 * 3600
    assert wp.special_target_for("2026-03-05")["target_sec"] == 2 * 3600
    # The week-long target would still cover Mar 6; it was replaced.
    assert wp.special_target_for("2026-03-06") is None


def write_goals(world, goals):
    with open(wp.GOALS_FILE, "w") as fh:
        json.dump(goals, fh)


def test_goal_is_four_hours_on_workdays_and_none_on_weekends(world):
    # No goals file at all: the defaults.
    for d in ("2026-03-02", "2026-03-04", "2026-03-06"):      # Mon, Wed, Fri
        assert wp.goal_sec_for(d) == 4 * 3600
    for d in ("2026-03-07", "2026-03-08"):                    # Sat, Sun
        assert wp.goal_sec_for(d) == 0


def test_goal_day_beats_week_beats_default(world):
    write_goals(world, {"default_hours": 5,
                        "weeks": {"2026-03-02": 3},          # week of Mon Mar 2
                        "days": {"2026-03-02": 0, "2026-03-07": 2}})
    assert wp.goal_sec_for("2026-03-02") == 0                # day off, inside a 3h week
    assert wp.goal_sec_for("2026-03-03") == 3 * 3600         # the week's goal
    assert wp.goal_sec_for("2026-03-06") == 3 * 3600
    assert wp.goal_sec_for("2026-03-07") == 2 * 3600         # a Saturday goal set by day
    assert wp.goal_sec_for("2026-03-08") == 0                # weeks never reach the weekend
    assert wp.goal_sec_for("2026-03-09") == 5 * 3600         # next week: the default


def test_carries_spread_stack_and_floor_at_zero(world):
    week = ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]  # Tue-Fri
    write_goals(world, {"days": {"2026-03-02": 0}, "carries": [
        # a 4h surplus from the week before, spread over Tue-Fri: -1h each
        {"from_week": "2026-02-23", "delta_sec": 4 * 3600, "days": week},
        # a 2h deficit landing on Wed and Thu only: +1h each
        {"from_week": "2026-02-02", "delta_sec": -2 * 3600, "days": week[1:3]},
        # a surplus far bigger than Friday's goal stops at zero
        {"from_week": "2026-02-16", "delta_sec": 20 * 3600, "days": week[3:]}]})
    assert wp.goal_sec_for("2026-03-02") == 0               # day off untouched
    assert wp.goal_sec_for("2026-03-03") == 3 * 3600
    assert wp.goal_sec_for("2026-03-04") == 4 * 3600        # -1h +1h
    assert wp.goal_sec_for("2026-03-05") == 4 * 3600
    assert wp.goal_sec_for("2026-03-06") == 0               # floored
    assert wp.goal_sec_for("2026-03-09") == 4 * 3600        # outside every carry


def test_goal_is_the_target_and_the_special_deduction_comes_off_it(world):
    world.now = at(9, 0)
    write_goals(world, {"weeks": {"2026-03-02": 3}})
    assert wp.main_target_for(DAY) == 3 * 3600
    wp.set_special_target(1, 1, main_pct=50)                 # 30m off main
    assert wp.main_target_for(DAY) == 2 * 3600 + 30 * 60
    main = world.snapshot()["buckets"]["main"]
    assert main["target_sec"] == 2 * 3600 + 30 * 60
    assert main["deduct_sec"] == 30 * 60


def test_a_day_off_has_a_zero_target(world):
    world.now = at(9, 0)
    write_goals(world, {"days": {DAY: 0}})
    assert world.snapshot()["buckets"]["main"]["target_sec"] == 0


def test_main_pct_takes_part_of_the_special_target_off_main(world):
    world.now = at(9, 0)
    assert wp.main_target_for(DAY) == 4 * 3600
    wp.set_special_target(0.5, 1, main_pct=50)           # 30m special, 15m off main
    assert wp.main_target_for(DAY) == 3 * 3600 + 45 * 60
    assert wp.special_target_for(DAY)["target_sec"] == 1800   # special unchanged
    assert wp.main_target_for("2026-03-05") == 4 * 3600  # outside the span
    # Published where the bar and the widget read main's target.
    assert world.snapshot()["buckets"]["main"]["target_sec"] == 3 * 3600 + 45 * 60


def test_main_pct_is_spread_over_the_span_and_replaced_with_the_target(world):
    world.now = at(9, 0)
    wp.set_special_target(6, 3, main_pct=50)             # 3h off main over 3 days
    for d in ("2026-03-04", "2026-03-05", "2026-03-06"):
        assert wp.main_target_for(d) == 3 * 3600         # 1h off each day
    world.now = at(9, 0, day=5)
    wp.set_special_target(2, 1)                          # replaces it, 0% off main
    assert wp.main_target_for("2026-03-05") == 4 * 3600
    assert wp.main_target_for("2026-03-04") == 3 * 3600  # the day it was set under


def test_main_pct_is_bounded_and_clearing_the_target_clears_it(world):
    world.now = at(9, 0)
    for bad in (-1, 101):
        with pytest.raises(ValueError):
            wp.set_special_target(1, 1, main_pct=bad)
    wp.set_special_target(1, 1, main_pct=100)
    assert wp.main_target_for(DAY) == 3 * 3600
    wp.set_special_target(0, 1, main_pct=100)            # 0h clears it all
    assert wp.main_target_for(DAY) == 4 * 3600


def test_zero_hours_clears_the_target_and_hides_the_bar(world):
    world.now = at(9, 0)
    assert wp.special_block(DAY)["visible"] is False      # default: 0h
    wp.set_special_target(3, 1)
    assert wp.special_block(DAY)["visible"] is True       # planned
    wp.set_special_target(0, 1)
    assert wp.special_block(DAY)["target"] is None
    assert wp.special_block(DAY)["visible"] is False
    world.now = at(12, 0)
    world.special(at(10, 0), at(10, 10))
    assert wp.special_block(DAY)["visible"] is True       # tracked


# ------------------------------------------------ meeting during special time

def test_meeting_routed_to_main_pauses_special_then_it_resumes(world):
    world.now = at(16, 0)
    world.special(at(13, 0))                             # still on
    world.meeting(13 * 60 + 20, 13 * 60 + 50)
    # Unanswered: all wall-clock time is special, the call included.
    assert wp.special_spans_for(DAY) == [[13 * 3600, 16 * 3600]]
    with open(wp.SPECIAL_LOG, "a") as fh:
        fh.write(json.dumps({"event": "route", "day": DAY,
                             "start": 13 * 60 + 20, "to": "main"}) + "\n")
    assert wp.special_spans_for(DAY) == [
        [13 * 3600, 13 * 3600 + 20 * 60], [13 * 3600 + 50 * 60, 16 * 3600]]


def test_meeting_routed_to_special_stays_special(world):
    world.now = at(16, 0)
    world.special(at(13, 0))
    world.meeting(13 * 60 + 20, 13 * 60 + 50)
    with open(wp.SPECIAL_LOG, "a") as fh:
        fh.write(json.dumps({"event": "route", "day": DAY,
                             "start": 13 * 60 + 20, "to": "special"}) + "\n")
    assert wp.special_spans_for(DAY) == [[13 * 3600, 16 * 3600]]


def test_route_meeting_keys_the_answer_to_the_open_meeting(world):
    world.now = at(13, 30)
    wp.start_meeting("call", 13 * 60 + 20, ZOOM)
    assert wp.route_meeting("main")["meeting_start"] == "13:20"
    with pytest.raises(ValueError):
        wp.route_meeting("neither")
    wp.close_open_meetings()
    with pytest.raises(ValueError):                      # nothing open now
        wp.route_meeting("main")


def test_route_meeting_can_name_a_finished_call_by_its_start(world):
    world.now = at(16, 0)
    world.meeting(13 * 60 + 20, 13 * 60 + 50)
    assert wp.route_meeting("main", "13:20")["meeting_start"] == "13:20"
    with pytest.raises(ValueError):
        wp.route_meeting("main", "09:00")


def test_gap_that_is_mostly_special_is_explained_by_it(world):
    world.now = at(16, 0)
    world.prompts[DAY] = [at(9, m) for m in range(0, 10, 2)] + \
        [at(11, m) for m in range(0, 10, 2)]
    world.special(at(9, 20), at(10, 50))
    snap = world.snapshot()
    assert [g["reason"] for g in snap["gaps"]] == ["special time"]


def test_main_routed_call_is_weighted_main_time_inside_special(world):
    world.now = at(16, 0)
    world.special(at(13, 0))
    world.meeting(13 * 60 + 20, 13 * 60 + 50, app=ZOOM)
    with open(wp.SPECIAL_LOG, "a") as fh:
        fh.write(json.dumps({"event": "route", "day": DAY,
                             "start": 13 * 60 + 20, "to": "main"}) + "\n")
    snap = world.snapshot()
    assert snap["buckets"]["meetings"]["raw_sec"] == 1800
    assert snap["buckets"]["meetings"]["credited_sec"] == 1200
    assert snap["buckets"]["special"]["sec"] == 16 * 3600 - 13 * 3600 - 1800


# ----------------------------------------------------------------- backfill

def test_backfill_rebuilds_a_past_day_with_the_weighting(world):
    world.now = at(9, 0, day=5)
    world.meeting(10 * 60, 10 * 60 + 30)                 # legacy record, no app
    world.prompts[DAY] = [at(10, 1)]
    # A snapshot from before buckets existed.
    os.makedirs(wp.VAULT_SNAPSHOT_DIR, exist_ok=True)
    json.dump({"date": DAY, "work_sec": 1800, "worked": [], "gaps": []},
              open(wp.snapshot_path(DAY), "w"))
    wp.backfill(2)
    snap = json.load(open(wp.snapshot_path(DAY)))
    assert snap["work_sec"] == 1800
    assert snap["buckets"]["meetings"]["raw_sec"] == 1800
    # One minute carried a prompt; the other 29 are discounted.
    assert snap["credited_sec"] == 60 + 29 * 60 * 2 // 3 + 0
    assert snap["worked"][0]["meetings"][0]["app"] == ZOOM


# ------------------------------------------------------ special as sessions

def _row(t, what="x"):
    return {"t": t, "kind": "prompt", "what": what, "n": 1}


def _period(a, b):
    return {"start": a, "end": b, "len_sec": (b - a) * 60, "what": ""}


def test_special_time_is_its_own_session_not_part_of_the_main_one():
    """An event inside a special span joins that span, never the period beside it."""
    rows = [_row("10:40"), _row("10:10"), _row("09:30")]
    worked = [_period(9 * 60, 9 * 60 + 45), _period(10 * 60 + 30, 11 * 60)]
    special = [[10 * 3600, 10 * 3600 + 25 * 60]]          # 10:00-10:25
    out = wp.group_sessions(rows, worked, special=special)
    assert [(s["start"], s["end"], s.get("special", False)) for s in out] == [
        (10 * 60 + 30, 11 * 60, False), (600, 625, True), (540, 585, False)]
    assert [s["n"] for s in out] == [1, 1, 1]
    assert [r["session"] for r in rows] == [0, 1, 2]
    assert out[1]["len_sec"] == 25 * 60 and out[1]["counted"]


def test_a_special_span_with_no_events_still_gets_a_session():
    out = wp.group_sessions([_row("09:30")], [_period(540, 585)],
                            special=[[10 * 3600, 10 * 3600 + 600]])
    assert [(s["start"], s.get("special", False), s["n"]) for s in out] == [
        (600, True, 0), (540, False, 1)]


def test_the_live_special_span_is_the_current_session():
    out = wp.group_sessions([_row("10:05")], [], special=[[36000, 36600]],
                            special_live=True)
    assert out[0]["special"] and out[0]["current"]


def test_convert_info_reaches_back_to_the_last_special_session():
    # special 08:00-08:30, main 08:30-09:00 is gone (nothing main between),
    # current main starts 09:10.
    worked = [_period(9 * 60 + 10, 9 * 60 + 40)]
    special = [[8 * 3600, 8 * 3600 + 1800]]
    out = wp.group_sessions([_row("09:20"), _row("08:10")], worked,
                            special=special)
    cur = out[0]
    assert cur["current"]
    assert cur["convert"] == {"to": "special", "from": 8 * 60 + 30, "until": None}
    assert out[1]["convert"] == {"to": "main", "from": 480, "until": 510}


def test_convert_info_never_reaches_over_another_main_session():
    worked = [_period(8 * 60 + 40, 9 * 60), _period(9 * 60 + 10, 9 * 60 + 40)]
    special = [[8 * 3600, 8 * 3600 + 1800]]
    out = wp.group_sessions([_row("09:20"), _row("08:50")], worked,
                            special=special)
    assert out[0]["convert"]["from"] == 9 * 60 + 10   # its own start
    assert out[1]["convert"] == {"to": "special", "from": 8 * 60 + 40,
                                 "until": 9 * 60}      # finished: as itself


def test_uncounted_runs_have_nothing_to_convert():
    out = wp.group_sessions([_row("12:00")], [_period(540, 570)])
    assert out[0]["convert"] is None


def test_converting_a_finished_session_moves_it_between_buckets(world):
    world.prompts[DAY] = [at(9, 0), at(9, 4), at(9, 8), at(9, 12)]
    world.now = at(12, 0)
    before = world.snapshot()
    assert before["buckets"]["special"]["sec"] == 0
    main_before = before["work_sec"]
    assert main_before > 0

    w = before["worked"][0]
    res = wp.convert_session("special", hhmm_of(w["start"]), hhmm_of(w["end"]))
    assert res["converted"]
    after = json.load(open(wp.snapshot_path(DAY)))
    assert after["buckets"]["special"]["sec"] == (w["end"] - w["start"]) * 60
    assert after["work_sec"] == 0
    # Not left on: the session was finished.
    assert not wp._special_is_on()

    wp.convert_session("main", hhmm_of(w["start"]), hhmm_of(w["end"]))
    back = json.load(open(wp.snapshot_path(DAY)))
    assert back["buckets"]["special"]["sec"] == 0
    assert back["work_sec"] == main_before


def test_converting_the_current_session_leaves_special_on(world):
    world.prompts[DAY] = [at(9, 0), at(9, 4), at(9, 8)]
    world.now = at(9, 10)
    wp.convert_session("special", "09:00", "now")
    assert wp._special_is_on()
    assert wp.special_spans_for(DAY) == [[9 * 3600, 9 * 3600 + 600]]
    world.now = at(9, 20)
    assert wp.special_spans_for(DAY) == [[9 * 3600, 9 * 3600 + 1200]]

    wp.convert_session("main", "09:00", "now")
    assert not wp._special_is_on()
    assert wp.special_spans_for(DAY) == []


def test_a_cut_splits_a_special_span_and_an_add_merges_touching_ones(world):
    world.special(at(10, 0), at(11, 0))
    world.now = at(12, 0)
    wp.convert_session("main", "10:20", "10:40")
    assert wp.special_spans_for(DAY) == [[36000, 37200], [38400, 39600]]
    wp.convert_session("special", "10:20", "10:40")
    assert wp.special_spans_for(DAY) == [[36000, 39600]]


def hhmm_of(m):
    return wp.hhmm_of(m)
