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
