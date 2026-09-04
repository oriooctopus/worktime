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


# Two Claude Code profiles feed this machine: interactive sessions under
# ~/.claude, and worktime's own background jobs under ~/.claude-personal, which
# has its own transcripts and no hooks of its own. Both hold prompts a person
# actually typed, so both are presence, and every consumer must walk both --
# counting one while change-detecting the other is exactly how the menu went
# stale without appearing to.
PROJECT_ROOTS = [
    os.path.expanduser("~/.claude/projects"),
    os.path.expanduser("~/.claude-personal/projects"),
]

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


def google_account_index(url):
    """The /u/<n> (or ?authuser=<n>) account index in a Google URL, or None."""
    lowered = (url or "").lower()
    match = (GOOGLE_ACCOUNT_IN_PATH.search(lowered)
             or GOOGLE_ACCOUNT_IN_QUERY.search(lowered))
    return int(match.group(1)) if match else None


def is_work_url(url, keywords=None, work_account=None):
    """True if `url` is work on its own: it sits on a host that is work for
    everyone, it names a work keyword, or it is a Google page signed in as the
    work account.

    Keywords are matched against the address only, never the query string.
    Googling "rubrik stock price" puts the employer's name in ?q= and nowhere
    else, and counting that as work would file idle curiosity about the share
    price -- or any search that merely mentions the company -- as a stretch at
    the job. Somewhere the name is in the host or the path, you were on their
    system; in ?q= you were only typing about them.

    `keywords` and `work_account` are passed in by callers that already hold
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
    if work_account is None:
        return False
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
    tmp_dir = tempfile.mkdtemp(prefix="worktime-history-")
    try:
        tmp = os.path.join(tmp_dir, "History")
        shutil.copy2(path, tmp)
        conn = sqlite3.connect(tmp)
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
