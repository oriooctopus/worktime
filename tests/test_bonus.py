#!/usr/bin/env python3
"""Tests for `bonus` -- banking a fixed block of time pressed with a chord.

The bonus runs forward from the press. If work resumes before it has finished,
what elapsed is placed forward and the rest is inserted behind the press using
the same walk as `track`'s split rule. Marks are minute-granular, so seconds
accumulate in a per-day carry and only whole minutes are written.

Run: pytest tests/test_bonus.py
"""

from test_buckets import DAY, at, wp, world  # noqa: F401  (world is a fixture)


def spans(out):
    return [(s["start"], s["end"]) for s in out["spans"]]


def test_full_bonus_lands_after_the_press(world):
    world.now = at(12, 15)
    out = wp.bonus(300, 300, "main")
    assert out["claimed"] == 5 and spans(out) == [("12:10", "12:15")]


def test_early_return_inserts_remainder_behind_press(world):
    world.now = at(12, 15)
    # Pressed 12:12, resumed after 3 of 5 minutes.
    out = wp.bonus(300, 180, "main")
    assert out["claimed"] == 5 and out["after"] == 3 and out["before"] == 2
    assert spans(out) == [("12:10", "12:12"), ("12:12", "12:15")]


def test_remainder_steps_over_counted_work(world):
    world.now = at(12, 15)
    world.prompts[DAY] = [at(12, 11)]
    out = wp.bonus(600, 180, "main")
    assert out["claimed"] == 10


def test_thirty_seconds_accumulate_across_presses(world):
    world.now = at(12, 15)
    first = wp.bonus(30, 30, "main")
    assert first["claimed"] == 0 and first["carry_sec"] == 30
    world.now = at(12, 20)
    second = wp.bonus(30, 30, "main")
    assert second["claimed"] == 1 and second["carry_sec"] == 0


def test_special_bucket_adds_special_time(world):
    world.now = at(12, 15)
    out = wp.bonus(300, 300, "special")
    assert out["bucket"] == "special"
    assert wp.special_sec_for(DAY) == 300


def test_main_bucket_leaves_special_alone(world):
    world.now = at(12, 15)
    wp.bonus(300, 300, "main")
    assert wp.special_sec_for(DAY) == 0
