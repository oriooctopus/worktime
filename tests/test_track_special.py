#!/usr/bin/env python3
"""Tests for `track <n> special_extra|special_take` -- logging special time by hand.

The track panel logs main or special time. For special there are two readings
of "the last ten minutes were special", and they differ on exactly one thing:
what happens to minutes the tracker already counted as main.

`special_take` converts them: special rises, main falls by the same amount, and
the two buckets still add up to what they did before. `special_extra` never
touches main -- it places the minutes only where nothing is counted yet, so the
day gets longer, and it reports the shortfall rather than overlapping.

Run: pytest tests/test_track_special.py
"""

import json

from test_buckets import DAY, at, wp, world  # noqa: F401  (world is a fixture)


def worked_prompts(world, *minutes):
    world.prompts[DAY] = [at(12, m) for m in minutes]


def test_take_converts_main_minutes_to_special(world):
    world.now = at(12, 15)
    worked_prompts(world, 8, 10, 12, 14)
    base = world.snapshot()

    out = wp.track_special(10, "special_take")

    snap = world.snapshot()
    assert out["bucket"] == "special" and out["claimed"] == 10
    assert snap["buckets"]["special"]["sec"] == 600
    assert snap["work_sec"] < base["work_sec"]          # main lost minutes
    assert snap["work_sec"] + 600 <= base["work_sec"] + 600


def test_take_does_not_double_count_what_is_already_special(world):
    world.now = at(12, 15)
    wp.track_special(10, "special_take")
    out = wp.track_special(10, "special_take")
    assert out["claimed"] == 0 and out["tracked"] is False
    assert wp.special_sec_for(DAY) == 600


def test_extra_adds_special_without_touching_main(world):
    world.now = at(12, 15)
    worked_prompts(world, 0, 1, 2)          # main work well before the window
    base = world.snapshot()

    out = wp.track_special(10, "special_extra")

    snap = world.snapshot()
    assert out["claimed"] == 10
    assert [(s["start"], s["end"]) for s in out["spans"]] == [("12:05", "12:15")]
    assert snap["buckets"]["special"]["sec"] == 600
    assert snap["work_sec"] == base["work_sec"]


def test_extra_stops_at_main_work_and_reports_the_shortfall(world):
    world.now = at(12, 15)
    worked_prompts(world, 13, 14)           # main work reaching almost to now
    snap = world.snapshot()
    end = max(w["end"] for w in snap["worked"])

    out = wp.track_special(10, "special_extra")

    assert out["claimed"] == out["asked"] - out["unplaced"] < 10
    # Nothing placed on top of a counted minute.
    assert all(wp.to_min(s["start"]) >= end for s in out["spans"])


def test_extra_never_overlaps_existing_special(world):
    world.now = at(12, 15)
    wp.track_special(5, "special_extra")                 # 12:10-12:15
    out = wp.track_special(5, "special_extra")          # blocked right behind now
    assert out["claimed"] == 0
    assert wp.special_sec_for(DAY) == 300


def test_take_stops_at_midnight(world):
    world.now = at(0, 3)
    out = wp.track_special(30, "special_take")
    assert out["claimed"] == 3


def test_refuses_unknown_mode_and_empty_count(world):
    assert wp.track_special(5, "clip")["tracked"] is False
    assert wp.track_special(0, "special_take")["tracked"] is False
    assert not wp.read_special_log()


def test_rows_are_the_same_add_rows_convert_session_writes(world):
    world.now = at(12, 15)
    wp.track_special(4, "special_take")
    rows = wp.read_special_log()
    assert [r["event"] for r in rows] == ["add"]
    assert rows[0]["to"] - rows[0]["from"] == 240
    json.dumps(rows)  # serialisable, i.e. plain data


# ------------------------------------------------- negative count: special -> main

def marks_written(world):
    path = wp.MARKS
    return [json.loads(l) for l in open(path) if l.strip()] if __import__("os").path.exists(path) else []


def test_negative_hands_the_newest_special_minutes_back(world):
    world.now = at(12, 15)
    wp.track_special(10, "special_extra")               # 12:05-12:15

    out = wp.track_special(-2, "special_take")

    assert out["returned"] and out["bucket"] == "main"
    assert out["claimed"] == 2 and out["unplaced"] == 0
    assert wp.special_sec_for(DAY) == 480
    # The two minutes are main now even though nothing tracked them: the
    # special log alone would have left them in neither bucket.
    assert [(m["start"], m["end"]) for m in marks_written(world)
            if m["note"] == wp.TRACK_NOTE][-1] == (12 * 60 + 13, 12 * 60 + 15)


def test_negative_works_with_either_special_rule(world):
    world.now = at(12, 15)
    wp.track_special(10, "special_extra")
    out = wp.track_special(-3, "special_extra")
    assert out["claimed"] == 3 and wp.special_sec_for(DAY) == 420


def test_negative_takes_older_special_when_none_is_recent(world):
    world.now = at(12, 15)
    wp.track_special(10, "special_extra")               # 12:05-12:15
    world.now = at(13, 0)                               # special ended 45m ago
    out = wp.track_special(-2, "special_take")
    assert out["claimed"] == 2
    assert [(s["start"], s["end"]) for s in out["spans"]] == [("12:13", "12:15")]


def test_negative_spans_several_special_blocks(world):
    world.now = at(12, 15)
    wp.track_special(3, "special_extra")                # 12:12-12:15
    world.now = at(13, 0)
    wp.track_special(3, "special_extra")                # 12:57-13:00
    out = wp.track_special(-5, "special_take")
    assert out["claimed"] == 5 and wp.special_sec_for(DAY) == 60


def test_negative_more_than_exists_reports_the_shortfall(world):
    world.now = at(12, 15)
    wp.track_special(4, "special_extra")
    out = wp.track_special(-10, "special_take")
    assert out["claimed"] == 4 and out["unplaced"] == 6
    assert wp.special_sec_for(DAY) == 0


def test_negative_with_no_special_changes_nothing(world):
    world.now = at(12, 15)
    out = wp.track_special(-2, "special_take")
    assert out["tracked"] is False and out["claimed"] == 0
    assert marks_written(world) == []

