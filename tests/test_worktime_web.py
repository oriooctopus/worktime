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
