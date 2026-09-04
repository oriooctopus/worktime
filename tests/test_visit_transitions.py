#!/usr/bin/env python3
"""Which Chrome visits count as somebody having navigated somewhere.

The rows here are the real shapes out of Chrome's History: an open Google
Docs tab re-authenticating itself every few minutes wrote three or four
visits a round, and the probe counted 36 minutes of an evening nobody
worked. The distinguishing mark is the redirect chain -- a navigation a
person makes starts one, a keepalive is a hop off a chain that started when
the tab was opened, hours earlier.

Run: pytest tests/test_visit_transitions.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "bin"))
import worktime_common as wc  # noqa: E402

SEC = 1_000_000  # Chrome's visit_time is microseconds
T0 = 13_400_000_000 * SEC

CHAIN_START = 0x10000000
CHAIN_END = 0x20000000
CLIENT_REDIRECT = 0x40000000
SERVER_REDIRECT = 0x80000000

# A self-contained navigation: typed, clicked, restored. No redirect.
PLAIN = CHAIN_START | CHAIN_END


def ids(rows):
    return wc.user_initiated_visit_ids(rows)


# --- navigations a person made ---

def test_plain_navigation_counts():
    assert ids([(1, 0, T0, PLAIN)]) == {1}


def test_typed_url_counts():
    # TYPED core (1) with FROM_ADDRESS_BAR, as Chrome writes it.
    assert ids([(1, 0, T0, 0x12000000 | 1)]) == {1}


def test_redirect_hops_off_a_real_click_count():
    # One click that server-redirects twice: the destination is what the
    # person ended up reading, and it is the row carrying the real title.
    rows = [
        (1, 0, T0, CHAIN_START),
        (2, 1, T0, SERVER_REDIRECT),
        (3, 2, T0 + 1 * SEC, SERVER_REDIRECT | CHAIN_END),
    ]
    assert ids(rows) == {1, 2, 3}


# --- chains nobody started ---

def test_keepalive_off_an_hours_old_visit_is_dropped():
    # The Google Docs shape: the tab was opened at T0, and at T0+8h the page
    # re-authenticates itself. Every hop names the morning's visit as parent.
    rows = [
        (1, 0, T0, PLAIN),
        (2, 1, T0 + 8 * 3600 * SEC, CLIENT_REDIRECT),
        (3, 2, T0 + 8 * 3600 * SEC, SERVER_REDIRECT),
        (4, 3, T0 + 8 * 3600 * SEC, CLIENT_REDIRECT | CHAIN_END),
    ]
    assert ids(rows) == {1}


def test_fast_refresh_cannot_chain_its_way_back_to_the_morning():
    # Why the window is measured to the chain's root and not between hops: a
    # tab reloading itself every 20s links each reload to the last, so a
    # per-hop window would keep an all-day chain alive.
    rows = [(1, 0, T0, PLAIN)]
    for i in range(1, 200):
        rows.append((i + 1, i, T0 + i * 20 * SEC, CLIENT_REDIRECT))
    kept = ids(rows)
    assert 1 in kept
    assert 2 in kept          # 20s after the click -- inside the window
    assert kept == {1, 2}     # and nothing after it


def test_redirect_whose_parent_is_missing_is_dropped():
    # The parent fell outside the day that was read. A real chain's hops are
    # in the same second, so this is not one.
    assert ids([(2, 1, T0, CLIENT_REDIRECT | CHAIN_END)]) == set()


def test_orphan_redirect_with_no_parent_at_all_is_dropped():
    assert ids([(2, 0, T0, SERVER_REDIRECT)]) == set()


def test_cycle_does_not_hang():
    rows = [(1, 2, T0, CLIENT_REDIRECT), (2, 1, T0, CLIENT_REDIRECT)]
    assert ids(rows) == set()


def test_window_boundary_is_inclusive():
    at = [(1, 0, T0, CHAIN_START),
          (2, 1, T0 + wc.REDIRECT_CHAIN_SEC * SEC, SERVER_REDIRECT)]
    past = [(1, 0, T0, CHAIN_START),
            (2, 1, T0 + (wc.REDIRECT_CHAIN_SEC + 1) * SEC, SERVER_REDIRECT)]
    assert ids(at) == {1, 2}
    assert ids(past) == {1}


def test_parent_after_child_is_not_a_chain():
    rows = [(1, 0, T0, CHAIN_START), (2, 1, T0 - 5 * SEC, SERVER_REDIRECT)]
    assert ids(rows) == {1}
