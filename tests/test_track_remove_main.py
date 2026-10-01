#!/usr/bin/env python3
"""Tests for `track -<n> clip|split` -- taking minutes back off main.

A negative count under a main rule removes the newest n counted minutes from
today. The cut is recorded, not applied to the evidence: it has to survive the
probe regenerating the snapshot, and a later prompt must not earn it back.

Run: pytest tests/test_track_remove_main.py
"""

from test_buckets import DAY, at, wp, world  # noqa: F401  (world is a fixture)


def main_min(snap):
    return sum(w["end"] - w["start"] for w in snap["worked"])


def busy_hour(world):
    world.now = at(12, 30)
    world.prompts[DAY] = [at(11, m) for m in range(0, 60, 2)]


def test_negative_removes_exactly_that_many_minutes_from_main(world):
    busy_hour(world)
    base = main_min(world.snapshot())

    out = wp.remove_main(25, "clip")

    assert out["claimed"] == 25 and out["tracked"] is True
    assert main_min(world.snapshot()) == base - 25


def test_the_cut_survives_a_rebuild(world):
    busy_hour(world)
    base = main_min(world.snapshot())
    wp.remove_main(25, "clip")
    assert main_min(world.snapshot()) == main_min(world.snapshot()) == base - 25


def test_asking_for_more_than_there_is_removes_only_what_there_is(world):
    busy_hour(world)
    base = main_min(world.snapshot())

    out = wp.remove_main(base + 30, "split")

    assert out["claimed"] == base and out["unplaced"] == 30
    assert main_min(world.snapshot()) == 0


def test_removing_twice_stacks(world):
    busy_hour(world)
    base = main_min(world.snapshot())
    wp.remove_main(10, "clip")
    wp.remove_main(15, "clip")
    assert main_min(world.snapshot()) == base - 25


def test_remove_leaves_special_time_alone(world):
    busy_hour(world)
    world.special(at(11, 40), at(11, 50))
    snap = world.snapshot()
    wp.remove_main(20, "clip")
    after = world.snapshot()
    assert after["buckets"]["special"]["sec"] == snap["buckets"]["special"]["sec"]
    assert main_min(after) == main_min(snap) - 20
