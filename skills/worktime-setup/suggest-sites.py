#!/usr/bin/env python3
"""Print the domains this person actually visits most, so the setup skill can
propose real options instead of asking someone to invent a list from memory.

With --google-accounts, print the Google account numbers that appear in their
browsing instead, so the setup skill can ask which one is work without asking
them to know what an account number is.

Read-only. Prints domains, account numbers and visit counts -- never full URLs
and never page titles.
"""
import argparse
import collections
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "bin"))
import worktime_common as wc  # noqa: E402


def recent_urls(days):
    """Every URL visited in the last `days` days."""
    path = wc.chrome_history_path()
    if path is None:
        print("no Chrome profile found to read history from", file=sys.stderr)
        sys.exit(1)
    since = datetime.now(tz=wc.local_tz()) - timedelta(days=days)
    return [url for (url,) in wc.read_history(
        path,
        "SELECT urls.url FROM visits JOIN urls ON urls.id = visits.url "
        "WHERE visits.visit_time > ?",
        (wc.chrome_micros(since),))]


def print_sites(urls, days):
    counts = collections.Counter()
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            counts[host] += 1
    print("Most-visited sites over the last {} days:".format(days))
    for host, n in counts.most_common(25):
        print("  {:6}  {}".format(n, host))


def print_google_accounts(urls, days):
    """Each Google account number seen, with what it was used for.

    The number is all Chrome's history holds -- the address signed in to it is
    never in the URL -- so the sites are printed as the evidence a person can
    actually recognise, and the address itself is confirmed by opening the
    account rather than guessed here.
    """
    seen = collections.defaultdict(collections.Counter)
    for url in urls:
        index = wc.google_account_index(url)
        if index is None:
            continue
        host = (urlparse(url).hostname or "").lower()
        seen[index][host] += 1
    if not seen:
        print("No signed-in Google accounts appear in the last {} days of "
              "browsing.".format(days))
        return
    print("Google accounts seen in the last {} days:".format(days))
    for index in sorted(seen):
        hosts = seen[index]
        top = ", ".join(h for h, _ in hosts.most_common(4))
        print("  account {}  ({} visits: {})".format(index, sum(hosts.values()), top))
    print("\nTo see which address each one is, open "
          "https://mail.google.com/mail/u/<number>/ -- the address is in the "
          "top right.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", nargs="?", type=int, default=14)
    parser.add_argument("--google-accounts", action="store_true",
                        help="list Google account numbers instead of sites")
    args = parser.parse_args()

    urls = recent_urls(args.days)
    if args.google_accounts:
        print_google_accounts(urls, args.days)
    else:
        print_sites(urls, args.days)


if __name__ == "__main__":
    main()
