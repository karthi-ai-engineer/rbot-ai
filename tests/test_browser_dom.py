"""Tests for browser_dom.py + the browser parts of perception.py.   python -m pytest tests/test_browser_dom.py -q
Unit tests only (pure functions, no browser, no screen). Set BUDDY_LIVE=1 to also run the page script in a running
buddy browser (python -m rbot.browser.browser_dom launch <url> first) - it only reads the page.
"""
import os

import pytest

from rbot.browser import browser_dom  # noqa: E402
from rbot.desktop import perception  # noqa: E402

EDGE = "Microsoft​ Edge"


# ============================================================================ window title -> CDP target
def _t(title, tid="x"):
    return {"id": tid, "title": title, "type": "page", "webSocketDebuggerUrl": "ws://" + tid}


def test_match_target_edge_title_with_more_pages_and_profile():
    tabs = [_t("Sign in – Test Shop", "a"), _t("Sales reports – Test Shop Admin", "b")]
    hits = browser_dom.match_target(f"Sales reports – Test Shop Admin and 11 more pages - Work - {EDGE}", tabs)
    assert [t["id"] for t in hits] == ["b"]


def test_match_target_single_page_and_one_more_page():
    tabs = [_t("Your cart – Test Shop", "a")]
    assert browser_dom.match_target(f"Your cart – Test Shop - Profile 1 - {EDGE}", tabs)[0]["id"] == "a"
    assert browser_dom.match_target(f"Your cart – Test Shop and 1 more page - Work - {EDGE}", tabs)[0]["id"] == "a"


def test_match_target_prefix_of_other_title_is_not_a_match():
    # 'Shop' is a prefix of the window title but followed by ' Admin', not ' - ' / ' and N more'
    tabs = [_t("Shop", "short"), _t("Shop Admin", "long")]
    hits = browser_dom.match_target(f"Shop Admin - Work - {EDGE}", tabs)
    assert [t["id"] for t in hits] == ["long"]


def test_match_target_title_containing_dash_and_duplicates():
    tabs = [_t("A - B", "1"), _t("A", "2"), _t("A - B", "3")]
    hits = browser_dom.match_target(f"A - B - Work - {EDGE}", tabs)
    assert [t["id"] for t in hits][:2] == ["1", "3"]           # longest (exact) first, duplicates kept
    assert browser_dom.match_target("Something else - Google Chrome", tabs) == []


# ============================================================================ CSS px -> screen px
def test_css_to_screen_scale_125():
    doc = (5, 143, 1865, 1016)                                 # UIA Document box, physical px
    vw = 1488                                                  # window.innerWidth at 125 % -> 1860 / 1.25
    assert browser_dom.css_to_screen((0, 0, 1488, 698.4), doc, vw) == [5, 143, 1865, 1016]
    assert browser_dom.css_to_screen((100, 40, 120, 60), doc, vw) == [130, 193, 155, 218]


def test_css_to_screen_scale_100_with_zoom():
    doc = (0, 100, 1000, 900)
    assert browser_dom.css_to_screen((10, 10, 20, 20), doc, 1000) == [10, 110, 20, 120]
    assert browser_dom.css_to_screen((10, 10, 20, 20), doc, 500) == [20, 120, 40, 140]   # 200 % page zoom


def test_to_marks_origin_clip_and_frames():
    raw = {"vw": 800, "marks": [[10, 10, 50, 30, "Button", "(icon button: ⋮ more options)"],
                                [790, 590, 900, 700, "Hyperlink", "half outside"],
                                [0, 0, 0.2, 0.2, "Button", "tiny"],
                                [5, 5, 30, 30, "Button", ""]],
           "frames": [[100, 100, 300, 200]]}
    doc = (1920 + 0, 100, 1920 + 1600, 1300)                  # second monitor at x=1920, scale 2
    marks, frames = browser_dom.to_marks(raw, doc, origin=(1920, 0))
    assert marks[0] == {"name": "(icon button: ⋮ more options)", "type": "Button", "box": [20, 120, 100, 160]}
    assert marks[1]["box"] == [1580, 1280, 1600, 1300]          # clipped to the content box
    assert len(marks) == 2                                     # tiny + unnamed dropped
    assert frames == [[200, 300, 600, 500]]


def test_to_marks_failure_means_none():
    assert browser_dom.to_marks(None, (0, 0, 10, 10))[0] is None
    assert browser_dom.to_marks({"vw": 800, "marks": []}, None)[0] is None
    assert browser_dom.to_marks({"vw": 800, "marks": []}, (0, 0, 10, 10))[0] == []


def test_page_script_formats():
    js = browser_dom.JS % {"maxn": 10, "tbudget": 200}
    assert "MAXN = 10" in js and "TBUDGET = 200" in js


def test_not_buddy_window():
    assert browser_dom.is_buddy_window(0) is False
    assert browser_dom.fetch_raw(0, "x - Microsoft Edge") is None
    assert browser_dom.page_marks(0, "x", (0, 0, 100, 100)) is None


# ============================================================================ perception: browser filter + merge
def test_is_browser():
    assert perception.is_browser("Chrome_WidgetWin_1", f"Sales reports and 3 more pages - Work - {EDGE}")
    assert perception.is_browser("Chrome_WidgetWin_1", "New Tab - Google Chrome")
    assert not perception.is_browser("Chrome_WidgetWin_1", "main.py - project - Visual Studio Code")
    assert not perception.is_browser("TeamsWebView", "Chat | Microsoft Teams")


def test_tab_name_cleanup():
    assert perception._tab_name("Thirdwave Group - Sleeping - Memory usage - 31.9 MB") == "Thirdwave Group"
    assert perception._tab_name("LLM Gateway - Memory usage - 616 MB") == "LLM Gateway"
    assert perception._tab_name("A - B") == "A - B"


def test_dom_merge_replaces_page_marks_only():
    doc = [0, 100, 1000, 900]
    uia = [{"name": "Back", "type": "Button", "box": [0, 50, 40, 90]},
           {"name": "Page", "type": "Document", "box": doc},
           {"name": "old link", "type": "Hyperlink", "box": [10, 200, 60, 220]},
           {"name": "Save", "type": "Button", "box": [500, 300, 560, 330], "pri": True},     # bubble over page
           {"name": "captcha box", "type": "CheckBox", "box": [110, 610, 130, 630]}]         # in x-origin frame
    dom = [{"name": "(icon button: ⋮ more options)", "type": "Button", "box": [900, 300, 920, 320]}]
    out = perception._dom_merge(uia, dom, [doc], frames=[[100, 600, 400, 700]])
    names = [m["name"] for m in out]
    assert names == ["Back", "Page", "Save", "captcha box", "(icon button: ⋮ more options)"]


# ============================================================================ live (optional)
@pytest.mark.skipif(os.environ.get("BUDDY_LIVE") != "1", reason="set BUDDY_LIVE=1 with the buddy browser running")
def test_live_page_script_runs():
    tabs = browser_dom.targets()
    assert tabs, "buddy browser not running (python -m rbot.browser.browser_dom launch <url>)"
    raw = browser_dom.evaluate(tabs[0]["webSocketDebuggerUrl"], browser_dom.JS % {"maxn": 50, "tbudget": 300}, 2.0)
    assert raw["vw"] > 0 and isinstance(raw["marks"], list)
