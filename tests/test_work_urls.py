#!/usr/bin/env python3
"""The two rules that decide work without anyone listing a domain.

Both live in worktime_common because the probe and chrome-work-blocks.py
classify the same visits and must agree; a copy in either would be a
disagreement nobody could see from the file it lived in.

Run: pytest tests/test_work_urls.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "bin"))
import worktime_common as wc  # noqa: E402

KEYWORDS = ["rubrik"]
WORK_ACCOUNT = 1


def is_work(url, keywords=KEYWORDS, account=WORK_ACCOUNT):
    return wc.is_work_url(url, keywords, account)


# --- the keyword rule ---

def test_keyword_in_host_is_work():
    assert is_work("https://sso.rubrik.com/login")
    assert is_work("https://rubrik.docebosaas.com/learn/course/123")


def test_keyword_in_path_is_work():
    assert is_work("https://github.com/scaledata/rubrik-tools/pull/4")


def test_keyword_match_is_case_insensitive():
    assert is_work("HTTPS://SSO.RUBRIK.COM/Login")


def test_searching_the_company_name_is_not_work():
    # The failure this rule exists for: a keyword-anywhere match filed idle
    # curiosity about the share price as a stretch at the job.
    assert not is_work(
        "https://www.google.com/search?q=rubrik+stock+price&oq=rubrik&ie=UTF-8")


def test_a_search_result_row_from_the_export_is_not_work():
    # The exported row leads with the page title, so the company name lands
    # ahead of any query string and the address-only rule alone would miss it.
    assert not is_work("rubrik stock price - Google Search | google.com/search?q=x")


def test_a_search_title_on_its_own_is_not_work():
    # Without the URL beside it -- which is how the focus log sees a tab, since
    # title and address are classified separately. This is the case the missing
    # word boundary in SEARCH_RESULTS used to let through: the assertion above
    # passed on the "google.com/search" half and never exercised the title.
    assert not is_work("rubrik stock price - Google Search")


def test_a_search_title_without_the_space_is_still_not_work():
    assert not is_work("rubrik stock price- Google Search")


def test_keyword_in_a_query_string_is_not_work():
    assert not is_work("https://news.ycombinator.com/item?id=1&ref=rubrik")


def test_no_keyword_and_no_google_account_is_not_work():
    assert not is_work("https://news.ycombinator.com/")


def test_keywords_are_configurable():
    assert is_work("https://wiki.acme-corp.com/onboarding", keywords=["acme-corp"])
    assert not is_work("https://wiki.acme-corp.com/onboarding", keywords=["rubrik"])


# --- the hosted-tool rule ---

def test_workday_is_work_wherever_the_tenant_sits_in_the_path():
    assert is_work("https://wd5.myworkday.com/rubrik/d/home.htmld")
    assert is_work("https://wd5.myworkday.com/wday/authgwy/rubrik/login.htmld")
    assert is_work("https://wd5-identity.myworkday.com/wday/authgwy/rubrik/upc/login")


def test_workday_is_work_for_a_company_the_keywords_never_name():
    # The point of the rule: the keyword list is the employer's name, and a
    # hosted HR system is the one work address that need not carry it.
    assert is_work("https://wd5.myworkday.com/acme-corp/d/home.htmld",
                   keywords=["rubrik"])


def test_an_empty_keyword_list_does_not_turn_workday_off():
    # ALWAYS_WORK_HOSTS is not somebody's keyword to clear -- switching the
    # employer-name rule off says nothing about whose Workday this is.
    assert is_work("https://wd5.myworkday.com/acme-corp/d/home.htmld",
                   keywords=[], account=None)


def test_searching_for_workday_is_not_work():
    assert not is_work("https://www.google.com/search?q=myworkday.com+login")


# --- the Google account rule ---

def test_work_account_drive_calendar_and_mail_are_work():
    assert is_work("https://drive.google.com/drive/u/1/home")
    assert is_work("https://calendar.google.com/calendar/u/1/r/week")
    assert is_work("https://mail.google.com/mail/u/1/#inbox")
    assert is_work("https://docs.google.com/document/u/1/d/abc/edit")


def test_personal_account_on_the_same_sites_is_not_work():
    # Same hostnames, same person, different account: the index is the only
    # thing separating the work Drive from the personal one.
    assert not is_work("https://drive.google.com/drive/u/0/home")
    assert not is_work("https://mail.google.com/mail/u/0/#inbox")


def test_account_index_directly_after_the_host_is_work():
    assert is_work("https://groups.google.com/u/1/g/some-list")


def test_authuser_query_form_is_work():
    assert is_work("https://console.cloud.google.com/home?authuser=1")


def test_google_with_no_account_configured_is_not_work():
    assert not is_work("https://drive.google.com/drive/u/1/home", account=None)


def test_google_account_index_reads_the_number():
    assert wc.google_account_index("https://drive.google.com/drive/u/2/home") == 2
    assert wc.google_account_index("https://news.ycombinator.com/") is None


# --- where the two answers come from ---

def test_defaults_when_the_profile_says_nothing():
    assert wc.work_url_keywords({}) == list(wc.DEFAULT_WORK_URL_KEYWORDS)
    assert wc.google_work_account({}) is None


def test_profile_overrides_both():
    profile = {"work_url_keywords": ["Acme"], "google_work_account": 2}
    assert wc.work_url_keywords(profile) == ["acme"]
    assert wc.google_work_account(profile) == 2


def test_an_empty_keyword_list_is_honoured_not_replaced():
    # Somebody who wants only the listed sites must be able to say so; falling
    # back to the default here would keep classifying pages they turned off.
    assert wc.work_url_keywords({"work_url_keywords": []}) == []
