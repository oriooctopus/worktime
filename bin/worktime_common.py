#!/usr/bin/env python3
"""Facts about this machine that more than one worktime script needs.

Every entry here was previously written out twice or more, and each copy had
drifted in a way that was invisible from the file it lived in:

- the Claude transcript roots: the probe's fingerprint walked one profile
  while prompt-count.py counted two, so a prompt to a background job was
  counted but never invalidated the cache, and the menu bar showed a reading
  from minutes earlier with no sign it was stale;
- the dashboard directory: calendar-export.py wrote to ~/obsidian-vault while
  the probe read ~/Documents/Main, and on this Mac only the second exists, so
  the exporter's output went somewhere nothing reads;
- the Chrome profile: the probe scans for the profile actually in use while
  chrome-work-blocks.py hardcoded "Default", which does not exist here;
- the timezone: four files hardcode it, one reads it from the profile, so
  configuring it produced a system that disagreed with itself.

They are one definition now because a constant cannot drift from itself.

Deliberately stdlib-only and free of import-time side effects. prompt-count.py
imports this on the Claude Code hook path and on the statusline, so anything
slow or fragile here is paid on every prompt. It must also stay Python 3.8
compatible: chrome-work-blocks.py runs on the Linux box, which is 3.8, so no
`X | None` annotations -- those are a TypeError at def time, not at call time.
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (the Linux box: 3.8.10)
    from backports.zoneinfo import ZoneInfo


# Claude Code keeps one transcript tree per profile, and this machine runs
# several: ~/.claude for interactive sessions, ~/.claude-personal for
# worktime's own background jobs, ~/.claude-bench for benchmark work. All of
# them hold prompts a person actually typed, so all of them are presence, and
# every consumer must walk all of them -- counting one while change-detecting
# another is exactly how the menu went stale without appearing to.
#
# They are discovered rather than listed because the list was wrong the moment
# a new profile appeared: a full interactive afternoon under ~/.claude-bench
# was invisible to the tracker for no reason other than that CLAUDE_CONFIG_DIR
# had been pointed somewhere the constant had never been told about. Nothing
# distinguishes a profile's transcripts from ~/.claude's except the directory
# name, so the directory name is what is matched.
#
# This is not a filter for real prompts: an eval harness driving the SDK writes
# `user` rows under these same roots, and it is the `entrypoint` check in
# prompt-count.py that keeps those out. Widening the roots only decides which
# transcripts are looked at, never which rows count.
PROJECT_ROOTS = sorted(
    p
    for p in (
        os.path.join(os.path.expanduser("~"), name, "projects")
        for name in os.listdir(os.path.expanduser("~"))
        if name == ".claude" or name.startswith(".claude-")
    )
    if os.path.isdir(p)
)

PROFILE_PATH = os.path.expanduser("~/.config/worktime/profile.json")

DEFAULT_TZ_NAME = "America/New_York"

# This machine keeps the vault under Documents/Main; the Linux box uses
# ~/obsidian-vault. Hardcoding either breaks the other, so it is resolved.
VAULT_DASHBOARDS = ["~/Documents/Main/Dashboard", "~/obsidian-vault/Dashboard"]

# Chrome stamps visits in microseconds since 1601, not since 1970.
CHROME_EPOCH = datetime(1601, 1, 1)

# Where Chrome keeps its profiles, per platform. The profile directory inside
# is NOT reliably "Default" -- this Mac's only one is "Profile 2" -- so these
# are the directories to search, not paths to a History file.
CHROME_DIRS = {
    "darwin": os.path.expanduser("~/Library/Application Support/Google/Chrome"),
    "linux": os.path.expanduser("~/.config/google-chrome"),
}

# This machine's one, resolved once. Callers read it rather than the table.
CHROME_DIR = CHROME_DIRS["darwin" if sys.platform == "darwin" else "linux"]

# The WSL box browses through a dedicated CDP profile rather than a normal
# install, so it is named outright instead of being searched for.
WSL_HISTORY = "/mnt/c/chrome-cdp-profile/Default/History"


def this_platform():
    """"darwin", "wsl", or "linux".

    WSL is not distinguishable from sys.platform -- it reports "linux" -- so it
    is read out of the kernel release string, which carries "microsoft" there.
    Getting this wrong is silent: the WSL box would look for Chrome under
    ~/.config/google-chrome, find nothing, and report a day with no browsing.
    """
    if sys.platform == "darwin":
        return "darwin"
    if "microsoft" in os.uname().release.lower():
        return "wsl"
    return "linux"


class ProfileError(Exception):
    """The profile file exists but could not be read."""


def load_profile(path=None):
    """The user's profile config, or {} when there is none.

    Absence is normal -- nothing here requires a profile, and the defaults are
    this machine's real layout -- so a missing file is not an error. A file
    that exists and does not parse IS one: it means somebody configured
    something and it is being silently ignored, which is the failure that
    hides itself. Callers that used to swallow this were choosing to render a
    confident answer from settings the user thought they had changed.
    """
    path = PROFILE_PATH if path is None else path
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise ProfileError("profile {} is unreadable: {}".format(path, e))


def tz_name(profile=None):
    profile = load_profile() if profile is None else profile
    return profile.get("timezone", DEFAULT_TZ_NAME)


def local_tz(profile=None):
    """The timezone every timestamp is resolved through.

    Ambient local time is not trustworthy: the sandbox some callers run under
    denies /var/db/timezone/zoneinfo, and tzset() answers that denial by
    silently falling back to UTC -- so the same script reports 16:28 or 12:28
    depending only on how it was invoked, with no error either way. That skew
    is enough to file an evening bout on the wrong day. ZoneInfo reads the
    bundled tzdata instead and is unaffected, which is why datetime.now() with
    no argument is never used anywhere in this project.
    """
    return ZoneInfo(tz_name(profile))


def dashboard_dir(env=None, profile_path=None):
    """First dashboard directory that actually exists on this machine.

    Order: WORKTIME_DASHBOARD env, the profile, then the known locations.
    Falls back to the first candidate so an error message names a real path
    rather than an empty string.
    """
    env = os.environ if env is None else env
    if env.get("WORKTIME_DASHBOARD"):
        return os.path.expanduser(env["WORKTIME_DASHBOARD"])
    configured = load_profile(profile_path).get("dashboard_dir")
    if configured:
        return os.path.expanduser(configured)
    for candidate in VAULT_DASHBOARDS:
        expanded = os.path.expanduser(candidate)
        if os.path.isdir(expanded):
            return expanded
    return os.path.expanduser(VAULT_DASHBOARDS[0])


# Words that make a URL work by themselves, wherever they appear in it. The
# employer's name is the one that earns its keep: "rubrik" catches the LMS, the
# IdP, the wiki, the ticket tracker and every internal tool nobody has thought
# to enumerate, which is the whole set that used to be listed two domains at a
# time and go stale the moment a new tool appeared.
DEFAULT_WORK_URL_KEYWORDS = ("rubrik",)

# Hosts that are work for everybody, whoever they work for, and so are not
# somebody's keyword to configure. The employer-name rule reaches internal
# tools because their addresses carry the company's name; a hosted HR system
# is the case it cannot reach. Workday serves every customer from
# myworkday.com and puts the tenant somewhere in the path, so whether the
# company's name appears at all is an accident of the page: the home screen
# is /rubrik/d/home.htmld and counts, while the export row for a task with a
# long title truncates to "... - Workday — https://w" and does not. Nobody
# opens their employer's Workday for fun -- the reviews, the time off and the
# expenses in it are all the job -- so the host settles it and the path stops
# mattering.
ALWAYS_WORK_HOSTS = ("myworkday.com",)

# A dev server on the machine is the work app itself. Nothing in the address
# says so -- "localhost:3000/dashboard" names no company and carries no
# account -- so the port is the only evidence, and the range the work app's
# dev servers listen on is a fact about this machine, like the timezone.
# Ports rather than plain "localhost": a personal side project served from
# 8118 is not the job, and counting every local server as work would file
# every evening spent on one as a stretch at it.
DEFAULT_WORK_LOCALHOST_PORTS = (3000, 3001, 3002, 3003, 3004, 3005)

# Both the full address and the export's "Title | localhost:3000/x" row reach
# here, so the host is matched wherever it sits rather than only at the start.
LOCALHOST_PORT = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})")

# Google serves every signed-in account from the same hostnames and separates
# them only by the account index in the path: the second account you added is
# /u/1, and Gmail, Calendar, Drive and Docs all carry it. So a work Google
# account is not a domain -- drive.google.com is personal and work at once --
# it is an index, and the index is the only thing that tells the two apart.
#
# Both spellings occur: "drive.google.com/drive/u/1/home" and the older
# "groups.google.com/u/1/...", plus the "?authuser=1" form that Cloud Console
# and several Workspace apps redirect through.
GOOGLE_ACCOUNT_IN_PATH = re.compile(r"\.google\.com/(?:[^/?#]+/)?u/(\d+)")
GOOGLE_ACCOUNT_IN_QUERY = re.compile(r"\.google\.com/[^?#]*[?&]authuser=(\d+)")

# Docs is the one Google host the account rule cannot read. Gmail, Calendar and
# Drive all keep the index in the address for the whole session, but a document
# settles at "docs.google.com/document/d/<id>/edit" with no index anywhere --
# 65 samples of one work doc in a single day carried none, while 118 Gmail and
# Calendar samples the same day carried theirs. So on this host the index is
# absent rather than personal, and reading its absence as "not the work
# account" deleted every hour spent reading a doc.
#
# The whole host, not a path prefix: docs.google.com serves only the editors
# (Docs, Sheets, Slides, Forms). A file LISTING is drive.google.com, which
# keeps its index and is still judged by it.
DOCS_HOST = "docs.google.com"

# A results page is never work, whoever is signed in. Searching the employer's
# name is the thing a keyword rule gets wrong most often, and it arrives in two
# shapes: the raw URL, where the name sits in ?q= and the address rule already
# handles it, and the exported row, which leads with the page title -- "rubrik
# stock price - Google Search" -- and puts the name in front of any query
# string at all. Only naming the results page itself catches both.
#
# No \b before the hyphen. There is no word boundary between a space and a
# hyphen -- both are non-word characters -- so "\b-\s*google search" matched
# "price- Google Search" and never the spaced form anybody actually has. The
# title half of this pattern was dead from the day it was written, and the one
# test covering it passed on the URL half of the same string. It went unnoticed
# while titles reached here only through the nightly export; the focus log now
# classifies the live tab title on every sample, where a stock-price search
# sitting in the foreground would have earned the afternoon.
SEARCH_RESULTS = re.compile(
    r"(?:google|bing|duckduckgo|search\.brave)\.com/(?:search|url)\b"
    r"|-\s*google search\b|\bat duckduckgo\b", re.I)


def work_url_keywords(profile=None):
    profile = load_profile() if profile is None else profile
    configured = profile.get("work_url_keywords")
    if configured is None:
        return list(DEFAULT_WORK_URL_KEYWORDS)
    return [str(k).lower() for k in configured]


def google_work_account(profile=None):
    """The Google account index that belongs to work, or None.

    None is the honest answer for somebody with one Google account: there is
    no index that distinguishes work from personal, and guessing one would
    file every visit to their own calendar as work.
    """
    profile = load_profile() if profile is None else profile
    index = profile.get("google_work_account")
    return None if index is None else int(index)


def work_localhost_ports(profile=None):
    """The localhost ports that serve the work app, as a set of ints."""
    profile = load_profile() if profile is None else profile
    configured = profile.get("work_localhost_ports")
    if configured is None:
        return set(DEFAULT_WORK_LOCALHOST_PORTS)
    return {int(p) for p in configured}


def focus_extra_apps(profile=None):
    """Bundle ids this person counts as work, beyond the built-in allow list.

    The built-in list is a claim about one machine, and two of its absences
    are judgements rather than facts. Obsidian is left out because that vault
    holds meeting notes and a grocery list in the same window, so being
    frontmost cannot say which -- true of that vault, not of every vault. For
    somebody whose notes app is only ever the job, the same exclusion deletes
    their working day.

    So the list stays as the default and this is how a person overrides it.
    Empty by default: the built-in list is what everybody gets until they say
    otherwise, and nothing here changes for a profile that omits the key.
    """
    profile = load_profile() if profile is None else profile
    configured = profile.get("focus_extra_apps")
    if configured is None:
        return set()
    if not isinstance(configured, list):
        raise ProfileError("profile: 'focus_extra_apps' must be a list of "
                           "bundle ids")
    return {str(b) for b in configured}


# How long somebody may go without touching anything before a page they are
# sitting on stops counting as read. Deliberately far looser than the ordinary
# two-minute gate: paragraphs take longer than that to read, and this setting
# only exists for people whose work IS reading.
DEFAULT_LONG_READ_IDLE_SEC = 300
# How often a continuing stay emits another event. Must stay under the probe's
# own five-minute silence cutoff or the events it produces will not chain, and
# a long read would come back as a string of disconnected minutes.
DEFAULT_LONG_READ_STRIDE_SEC = 240


def long_read(profile=None):
    """Settings for crediting a long stay on one page, or None when off.

    OFF by default, and that default is not timidity: crediting a stay is the
    exact mechanism that once billed forty-one minutes to a Slack window
    nobody had touched. What makes it safe to offer at all is that the stay
    has to keep being vouched for by input -- see focus_dwell_for() -- so it
    is not the old span model returning, it is the span model with the thing
    that was missing from it.

    Whether to turn it on is a fact about a person, not about a machine. Fast
    navigation leaves a trail of switches and needs none of this; reading one
    article for forty minutes leaves a single switch, and without this the
    other thirty-nine minutes are silence.
    """
    profile = load_profile() if profile is None else profile
    configured = profile.get("long_read")
    if configured is None:
        return None
    if not isinstance(configured, dict):
        raise ProfileError("profile: 'long_read' must be an object")
    if not configured.get("enabled"):
        return None
    cfg = {
        "max_idle_sec": int(configured.get(
            "max_idle_sec", DEFAULT_LONG_READ_IDLE_SEC)),
        "stride_sec": int(configured.get(
            "stride_sec", DEFAULT_LONG_READ_STRIDE_SEC)),
    }
    for key, value in cfg.items():
        if value <= 0:
            raise ProfileError(
                "profile: long_read '{}' must be positive".format(key))
    return cfg


def google_account_index(url):
    """The /u/<n> (or ?authuser=<n>) account index in a Google URL, or None."""
    lowered = (url or "").lower()
    match = (GOOGLE_ACCOUNT_IN_PATH.search(lowered)
             or GOOGLE_ACCOUNT_IN_QUERY.search(lowered))
    return int(match.group(1)) if match else None


def is_work_url(url, keywords=None, work_account=None, localhost_ports=None):
    """True if `url` is work on its own: it sits on a host that is work for
    everyone, it names a work keyword, it is a dev server on one of the work
    app's localhost ports, or it is a Google page signed in as the work
    account -- with Docs, where no index is served to read, counted whenever a
    work account exists at all.

    Keywords are matched against the address only, never the query string.
    Googling "rubrik stock price" puts the employer's name in ?q= and nowhere
    else, and counting that as work would file idle curiosity about the share
    price -- or any search that merely mentions the company -- as a stretch at
    the job. Somewhere the name is in the host or the path, you were on their
    system; in ?q= you were only typing about them.

    `keywords`, `localhost_ports` and `work_account` are passed in by callers that already hold
    the config, so a per-visit call does not re-read the profile from disk.
    """
    lowered = (url or "").lower()
    if SEARCH_RESULTS.search(lowered):
        return False
    address = lowered.split("?")[0].split("#")[0]
    if any(h in address for h in ALWAYS_WORK_HOSTS):
        return True
    keywords = work_url_keywords() if keywords is None else keywords
    if any(k in address for k in keywords):
        return True
    ports = work_localhost_ports() if localhost_ports is None else localhost_ports
    if any(int(m) in ports for m in LOCALHOST_PORT.findall(address)):
        return True
    if work_account is None:
        return False
    if DOCS_HOST in address:
        return True
    return google_account_index(lowered) == work_account


def chrome_history_path(profile_path=None, platform=None, chrome_dir=None):
    """The History DB of the Chrome profile actually in use, or None.

    Found by scanning rather than hardcoded, because the profile directory is
    not reliably "Default": this Mac's only profile is "Profile 2" and has no
    Default at all, so a hardcoded path reads an empty history forever while
    looking exactly like somebody who did not browse. Most-recently-written
    wins, which is what "the profile in use" means when several exist.
    """
    configured = load_profile(profile_path).get("chrome_history_path")
    if configured:
        return os.path.expanduser(configured)
    platform = this_platform() if platform is None else platform
    if platform == "wsl":
        return WSL_HISTORY
    root = CHROME_DIR if chrome_dir is None else chrome_dir
    if not os.path.isdir(root):
        return None
    found = [os.path.join(root, name, "History")
             for name in os.listdir(root)
             if os.path.exists(os.path.join(root, name, "History"))]
    if not found:
        return None
    return max(found, key=lambda p: os.stat(p).st_mtime)


def chrome_micros(dt):
    """A tz-aware datetime as Chrome's microseconds-since-1601."""
    return int((dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                - CHROME_EPOCH).total_seconds() * 1e6)


def chrome_time(micros, tz):
    """Chrome's microseconds-since-1601 back to a local datetime."""
    return ((CHROME_EPOCH + timedelta(microseconds=micros))
            .replace(tzinfo=ZoneInfo("UTC")).astimezone(tz))


def read_history(path, sql, params):
    """Run one query against a copy of Chrome's History.

    Chrome holds the database open, so it is copied first -- about 0.04s for
    58MB, which is what makes reading it from a five-second poll affordable.
    """
    return read_history_queries(path, [(sql, params)])[0]


def read_history_queries(path, queries):
    """Run several queries against ONE copy of Chrome's History.

    The copy is the whole cost of reading it, so a caller needing two answers
    about the same moment -- the visits to work pages, and the redirect chains
    that say which of them somebody made -- asks for both here rather than
    paying it twice on a five-second poll.

    The journal is copied with it. Chrome runs History in `journal_mode=delete`,
    so an in-flight write leaves the original pages in `History-journal` while
    the partially-rewritten ones sit in `History` itself. Copying the database
    alone captures that half-applied state with nothing to undo it, and sqlite
    rejects the result as `database disk image is malformed` -- the probe died
    that way 95 times before the sidecar came along. With the journal beside it
    sqlite replays the rollback against the copy and opens the consistent
    pre-transaction snapshot instead.

    Chrome's lock is why this is a file copy at all: `Connection.backup()` is
    the tidy way to snapshot a live database, but it waits on that lock and
    never returns, which the menu bar's 30s watchdog turns into a red dot.

    A History holding no tables at all reads as no browsing, not as an error.
    Chrome recreates the file empty when it cannot open the old one, which is
    what happened when this machine's disk hit 100% full: a 32KB History with
    zero tables where 61MB of visits had been. Every query then failed with
    `no such table: visits`, and since the probe raises rather than guessing,
    that killed every poll -- a permanently red dot for a browser that was
    simply empty. An empty database is a real state (a fresh profile, cleared
    history, a Chrome that had to start over) and belongs with the absent one
    the caller already handles.

    The check is for zero tables specifically, not for the error text. A
    `no such table` from a database that HAS tables means the schema is not
    what this code expects, which is a bug and still raises -- as does every
    other sqlite failure, including the malformed-image case above.
    """
    tmp_dir = tempfile.mkdtemp(prefix="worktime-history-")
    try:
        tmp = os.path.join(tmp_dir, "History")
        shutil.copy2(path, tmp)
        # Copied after the database, not before: a journal read first could be
        # deleted by a commit landing mid-copy, which would leave a stale undo
        # log pointing at pages the copy has already moved past.
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                shutil.copy2(path + suffix, tmp + suffix)
        conn = sqlite3.connect(tmp)
        try:
            if not conn.execute(
                    "SELECT count(*) FROM sqlite_master "
                    "WHERE type='table'").fetchone()[0]:
                return [[] for _ in queries]
            return [list(conn.execute(sql, params)) for sql, params in queries]
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Chrome visit transitions
# --------------------------------------------------------------------------

# Bits from Chrome's PageTransition (page_transition_types.h). Only the two
# that separate a navigation somebody performed from a hop generated on its
# way matter here.
CHAIN_START = 0x10000000
REDIRECT_QUALIFIERS = 0xC0000000  # CLIENT_REDIRECT | SERVER_REDIRECT

# How far apart two visits can be and still belong to one redirect chain.
# Chrome writes a chain's hops within the same second; 30s is slack, not a
# judgement call. It is the whole load-bearing part of the test below: a
# keepalive's first hop *does* carry a from_visit, but it points at the visit
# that opened the tab hours ago.
REDIRECT_CHAIN_SEC = 30


def user_initiated_visit_ids(rows, window_sec=REDIRECT_CHAIN_SEC):
    """The ids of the visits in `rows` that a person actually navigated to.

    `rows` are (id, from_visit, visit_time, transition) tuples, visit_time in
    Chrome's microseconds. The set comes back rather than a filtered list so a
    caller can apply it to a differently-shaped row of its own.

    A visit counts when it starts a chain (CHAIN_START -- every navigation a
    person performs has it, typed or clicked), or when it is a redirect hop
    that lands within `window_sec` of the chain's start. Anything else is a
    chain nobody started: a background tab a page decided to reload or
    re-authenticate on its own. Google Docs does this to an open tab every few
    minutes and GitHub does it to a pull request, and each round wrote three
    or four visits indistinguishable from reading the page.

    The window is measured from the hop back to the chain's root, not between
    consecutive hops. Per-hop would readmit exactly what this exists to
    exclude: a tab refreshing itself every 20s chains each refresh to the last
    one, so a root from this morning stays reachable all day.
    """
    by_id = {row[0]: row for row in rows}
    kept = set()

    for row in rows:
        vid, _, when, _ = row
        current = row
        seen = set()
        while True:
            cid, from_visit, _, transition = current
            if transition & CHAIN_START:
                kept.add(vid)
                break
            if not transition & REDIRECT_QUALIFIERS or cid in seen:
                break
            seen.add(cid)
            parent = by_id.get(from_visit)
            if parent is None:
                # The root is outside the rows we were given, which for a
                # window of a day or more means it is not seconds old.
                break
            gap = (when - parent[2]) / 1e6
            if not 0 <= gap <= window_sec:
                break
            current = parent

    return kept
