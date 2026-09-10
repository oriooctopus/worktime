"""Tests for web/server.py row filtering."""
import importlib.util, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("worktime_web", os.path.join(ROOT, "web", "server.py"))
web = importlib.util.module_from_spec(spec)
sys.modules["worktime_web"] = web
spec.loader.exec_module(web)

ROWS = [
    ("10:00", "10:30", "(busy)", "work"),
    ("14:03", "14:30", "Working (browsing)", "work"),
    ("16:30", "17:30", "(busy)", "work"),
    ("18:00", "19:00", "Soccer", "personal"),
]
NOON, DURING, EVENING = 12 * 60, 17 * 60, 20 * 60


def test_future_rows_are_hidden():
    """A row starting later today is a plan, not a record."""
    labels = [r["label"] for r in web.visible_rows(ROWS, NOON)]
    assert labels == ["(busy)"]  # only the 10:00 one has started


def test_tonights_personal_event_is_hidden_until_it_starts():
    assert all(r["label"] != "Soccer" for r in web.visible_rows(ROWS, DURING))
    assert any(r["label"] == "Soccer" for r in web.visible_rows(ROWS, EVENING))


def test_most_recent_first():
    starts = [r["start"] for r in web.visible_rows(ROWS, EVENING)]
    assert starts == sorted(starts, reverse=True)


def test_in_progress_row_is_clipped_to_now():
    """A 16:30-17:30 meeting at 17:00 has produced 30 minutes, not 60."""
    r = web.visible_rows(ROWS, 17 * 60)[0]
    assert r["ongoing"] and r["shown_end"] == "17:00" and r["end"] == "17:30"


def test_finished_row_keeps_its_real_end():
    r = [x for x in web.visible_rows(ROWS, EVENING) if x["start"] == "16:30"][0]
    assert not r["ongoing"] and r["shown_end"] == "17:30"


def test_row_starting_exactly_now_is_included_and_zero_width():
    r = web.visible_rows(ROWS, 16 * 60 + 30)[0]
    assert r["start"] == "16:30" and r["shown_end"] == "16:30" and r["ongoing"]


def test_nothing_yet_today_returns_empty():
    assert web.visible_rows(ROWS, 6 * 60) == []


# --- the probe's markdown sidecar (its own session conclusions, transported) ---

import importlib.util as _il
_pspec = _il.spec_from_file_location("probe_render", os.path.join(ROOT, "bin", "worktime-probe.py"))

SNAP = """---
generated: 2026-08-31T17:40:00-04:00
---
| Start | End | Secs | Prompts | Slack | Marked | Meeting | What |
|-------|-----|------|---------|-------|--------|---------|------|
| 09:40 | 11:00 | 4800 | 28 | 29 | yes | (busy) | audit benchmark tasks |
| 13:45 | 16:04 | 8340 | 12 | 4 |  | (busy) | ruby implementation |
| 16:20 | 16:23 | 180 | 0 | 0 | yes |  | marked as working |
"""


def test_parses_every_period():
    assert len(web.parse_snapshot(SNAP)) == 3


def test_periods_are_most_recent_first():
    assert [p["start"] for p in web.parse_snapshot(SNAP)] == ["16:20", "13:45", "09:40"]


def test_period_carries_its_constituent_signals():
    """The point of a session: one span, several kinds of evidence inside it."""
    p = [x for x in web.parse_snapshot(SNAP) if x["start"] == "09:40"][0]
    assert p["seconds"] == 4800 and p["n_prompts"] == 28 and p["n_slack"] == 29
    assert p["meeting"] == "(busy)" and p["marked"] is True


def test_empty_marked_column_is_false_not_truthy():
    p = [x for x in web.parse_snapshot(SNAP) if x["start"] == "13:45"][0]
    assert p["marked"] is False and p["meeting"] == "(busy)"


def test_header_and_separator_rows_are_skipped():
    assert all(p["start"] != "Start" for p in web.parse_snapshot(SNAP))


def test_gap_table_rows_are_not_mistaken_for_periods():
    doc = SNAP + "\n## Gaps\n\n| Start | End | Secs |\n|---|---|---|\n| 11:00 | 13:45 | 9900 |\n"
    assert len(web.parse_snapshot(doc)) == 3


def test_missing_sidecar_reports_none_not_empty():
    """None means 'no probe data on this machine'; [] would mean 'no work today'."""
    import datetime
    assert web.load_periods(datetime.datetime(1999, 1, 1)) is None


# --- round trip: what the probe writes must be what this parser reads ---

_probe = _il.module_from_spec(_pspec)
_pspec.loader.exec_module(_probe)

SNAP_OBJ = {
    "worked": [
        {"start": 580, "end": 660, "len_sec": 4800, "n_prompts": 28, "n_slack": 29,
         "marks": [{"note": "at the desk"}], "what": "audit benchmark tasks",
         "meetings": [{"start": 585, "end": 600, "title": "(busy)", "counts": True}]},
        {"start": 825, "end": 964, "len_sec": 8340, "n_prompts": 12, "n_slack": 4,
         "marks": [], "what": "ruby implementation",
         "meetings": [{"start": 930, "end": 960, "title": "Soccer", "counts": False}]},
    ],
    "gaps": [{"start": 660, "end": 825, "len_sec": 9900, "open": False}],
}


def test_probe_output_parses_back_to_the_same_periods():
    """The writer and the reader are a symmetric pair; test them as a round trip."""
    md = _probe.render_markdown_snapshot("2026-08-31", SNAP_OBJ)
    got = web.parse_snapshot(md)
    assert [p["start"] for p in got] == ["13:45", "09:40"]
    first = [p for p in got if p["start"] == "09:40"][0]
    assert first["seconds"] == 4800 and first["n_prompts"] == 28
    assert first["n_slack"] == 29 and first["marked"] is True


def test_round_trip_excludes_non_counting_meetings():
    """A personal appointment explains silence but is not time on the job."""
    md = _probe.render_markdown_snapshot("2026-08-31", SNAP_OBJ)
    second = [p for p in web.parse_snapshot(md) if p["start"] == "13:45"][0]
    assert "Soccer" not in second["meeting"]


def test_round_trip_gaps_are_not_read_as_periods():
    md = _probe.render_markdown_snapshot("2026-08-31", SNAP_OBJ)
    assert "## Gaps" in md and len(web.parse_snapshot(md)) == 2


def test_rendered_snapshot_publishes_no_prompt_text():
    """Prompt content must not reach a file that syncs to every device."""
    md = _probe.render_markdown_snapshot("2026-08-31", SNAP_OBJ)
    assert "sessions" not in md.lower() and "prompts\":" not in md
