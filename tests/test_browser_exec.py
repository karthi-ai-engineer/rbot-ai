"""Tests for browser_exec.py (background actions in the buddy browser) + browser_dom.observe_tab.

  python -m pytest tests/test_browser_exec.py -q                 unit tests only (no browser, no screen)
  BUDDY_LIVE=1 python -m pytest tests/test_browser_exec.py -q -s -k live
      live: serves tests/live/hitl_pages (127.0.0.1:8765), starts the buddy browser if it isn't running (and closes it
      again afterwards), covers it with a test window so it is NOT in front, then clicks / holds / types in it via
      CDP only. Checks the page through DOM reads, that the real cursor never moved and that no injected OS mouse
      event happened. Saves ghost-cursor frames to runs/bg_ghost_*.png.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from rbot.browser import browser_dom as bd  # noqa: E402
from rbot.browser import browser_exec as be  # noqa: E402
from rbot.human import hitl  # noqa: E402


# ============================================================================ coordinates / glide
def test_to_css_divides_by_device_pixel_ratio():
    assert be.to_css(1860, 873, 1.25) == (1488.0, 698.4)
    assert be.to_css(100, 50, 1.0) == (100.0, 50.0)
    assert be.to_css(300, 300, 2) == (150.0, 150.0)
    assert be.to_css(10, 10, None) == (10.0, 10.0)            # unknown scale -> 1


def test_glide_duration_is_between_250_and_400_ms():
    assert be.glide_ms(None, None, 500, 500) == 250
    assert be.glide_ms(0, 0, 1, 1) == 0                        # already there
    assert be.glide_ms(0, 0, 100, 0) == 265
    assert be.glide_ms(0, 0, 3000, 0) == 400


def test_raw_to_image_marks_scales_and_clips():
    raw = {"vw": 1488, "marks": [[100, 40, 120, 60, "Button", "OK"], [1480, 690, 1600, 800, "Edit", "Far"],
                                 [5, 5, 5.5, 5.5, "Button", "tiny"], [10, 10, 50, 30, "Button", ""]]}
    marks, s = bd.raw_to_image_marks(raw, (1860, 873))
    assert s == 1.25
    assert marks[0] == {"name": "OK", "type": "Button", "box": [125, 50, 150, 75]}
    assert marks[1]["box"] == [1850, 862, 1860, 873]           # clipped to the image
    assert [m["name"] for m in marks] == ["OK", "Far"]          # too small / unnamed dropped


# ============================================================================ target choice
def _tab(i, url="http://x/", typ="page"):
    return {"id": i, "type": typ, "url": url, "title": i, "webSocketDebuggerUrl": "ws://" + i}


def test_choose_target_most_recent_page_skipping_devtools_and_workers():
    tabs = [_tab("sw", typ="service_worker"), _tab("dt", "devtools://devtools/inspector.html"), _tab("a"), _tab("b")]
    assert bd.choose_target(tabs)["id"] == "a"
    assert bd.choose_target(tabs, "b")["id"] == "b"
    assert bd.choose_target(tabs, "gone") is None
    assert bd.choose_target([]) is None


# ============================================================================ keys
def test_norm_key_aliases():
    assert be.norm_key("Esc") == "escape"
    assert be.norm_key("Return") == "enter"
    assert be.norm_key("PageDown") == "page_down"
    assert be.norm_key("page down") == "page_down"
    assert be.norm_key("ArrowLeft") == "left"
    assert be.norm_key("Control") == "ctrl"
    assert be.norm_key("Win") == "meta"
    assert be.norm_key("A") == "a"
    assert be.norm_key("/") == "/"


@pytest.mark.parametrize("name,key,code,vk", [
    ("enter", "Enter", "Enter", 13), ("tab", "Tab", "Tab", 9), ("escape", "Escape", "Escape", 27),
    ("backspace", "Backspace", "Backspace", 8), ("delete", "Delete", "Delete", 46),
    ("up", "ArrowUp", "ArrowUp", 38), ("down", "ArrowDown", "ArrowDown", 40), ("left", "ArrowLeft", "ArrowLeft", 37),
    ("right", "ArrowRight", "ArrowRight", 39), ("home", "Home", "Home", 36), ("end", "End", "End", 35),
    ("pgdn", "PageDown", "PageDown", 34), ("space", " ", "Space", 32), ("F5", "F5", "F5", 116),
    ("a", "a", "KeyA", 65), ("7", "7", "Digit7", 55), (".", ".", "Period", 190), ("/", "/", "Slash", 191)])
def test_key_info(name, key, code, vk):
    assert be.key_info(name)[:3] == (key, code, vk)


def test_key_info_unknown():
    with pytest.raises(KeyError):
        be.key_info("hyperspace")


def test_enter_types_carriage_return_and_tab_has_no_text():
    down, up = be.combo_events("enter")
    assert down["type"] == "keyDown" and down["text"] == "\r" and down["windowsVirtualKeyCode"] == 13
    assert up["type"] == "keyUp" and "text" not in up
    down, _ = be.combo_events("tab")
    assert down["type"] == "rawKeyDown" and "text" not in down


def test_ctrl_a_combo_events():
    ev = be.combo_events(["ctrl", "a"])
    assert [(e["type"], e["key"], e["modifiers"]) for e in ev] == [
        ("rawKeyDown", "Control", 2), ("rawKeyDown", "a", 2), ("keyUp", "a", 2), ("keyUp", "Control", 0)]
    assert ev[1]["code"] == "KeyA" and ev[1]["windowsVirtualKeyCode"] == 65
    assert all("text" not in e for e in ev)                    # ctrl+a selects, it doesn't type an 'a'
    assert ev[0]["location"] == 1 and ev[0]["code"] == "ControlLeft"


def test_ctrl_shift_combo_and_shift_tab():
    ev = be.combo_events(["Ctrl", "Shift", "Tab"])
    assert [e["modifiers"] for e in ev] == [2, 10, 10, 10, 2, 0]
    ev = be.combo_events(["shift", "tab"])
    assert ev[1]["key"] == "Tab" and ev[1]["modifiers"] == 8


def test_char_events_letters_shift_and_symbols():
    ev = be.char_events("a")
    assert [e["type"] for e in ev] == ["keyDown", "keyUp"] and ev[0]["text"] == "a"
    ev = be.char_events("A")
    assert [e["key"] for e in ev] == ["Shift", "A", "A", "Shift"]
    assert ev[1]["text"] == "A" and ev[1]["modifiers"] == 8 and ev[1]["code"] == "KeyA"
    ev = be.char_events("@")
    assert ev[1]["key"] == "@" and ev[1]["code"] == "Digit2" and ev[1]["text"] == "@"
    ev = be.char_events("?")
    assert ev[1]["code"] == "Slash" and ev[1]["windowsVirtualKeyCode"] == 191
    assert be.char_events("\n")[0]["key"] == "Enter"
    assert be.char_events("é") is None and be.char_events("✓") is None      # -> Input.insertText


def test_browser_shortcuts():
    assert be.browser_shortcut(["ctrl", "l"]) == "address"
    assert be.browser_shortcut(["Control", "L"]) == "address"
    assert be.browser_shortcut(["alt", "d"]) == "address"
    assert be.browser_shortcut(["ctrl", "t"]) == "address_new"
    assert be.browser_shortcut("f5") == "reload"
    assert be.browser_shortcut(["alt", "left"]) == "back"
    assert be.browser_shortcut(["ctrl", "w"]) == "refuse"
    assert be.browser_shortcut(["win", "r"]) == "desktop"
    assert be.browser_shortcut(["alt", "tab"]) == "desktop"
    assert be.browser_shortcut(["ctrl", "a"]) is None
    assert be.browser_shortcut(["ctrl", "c"]) is None
    assert be.browser_shortcut("enter") is None


def test_nav_url():
    assert be.nav_url("example.com") == "https://example.com"
    assert be.nav_url("http://127.0.0.1:8765/x.html") == "http://127.0.0.1:8765/x.html"
    assert be.nav_url("localhost:8765/a") == "http://localhost:8765/a"
    assert be.nav_url("grape buddy") == "https://www.bing.com/search?q=grape+buddy"
    assert be.nav_url("  ") is None


# ============================================================================ dialogs
def test_dialog_drawn_with_ok_cancel_marks_and_click_mapping():
    from PIL import Image
    img, marks = bd._draw_dialog(Image.new("RGB", (1000, 600), "white"),
                                 {"type": "confirm", "message": "Delete it?", "url": "http://127.0.0.1:8765/a"})
    assert img.size == (1000, 600)
    assert [m["name"].split(" ")[0] for m in marks] == ["OK", "Cancel"]
    ok, cancel = marks
    cx = lambda b: ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)  # noqa: E731
    assert be.dialog_answer_at(marks, *cx(ok["box"])) is True
    assert be.dialog_answer_at(marks, *cx(cancel["box"])) is False
    assert be.dialog_answer_at(marks, 1, 1) is None
    _, pm = bd._draw_dialog(None, {"type": "prompt", "message": "Name?", "default": "x"})
    assert be.dialog_answer_at(pm, *cx(pm[0]["box"])) == "edit"
    _, am = bd._draw_dialog(None, {"type": "alert", "message": "Hi"})
    assert len(am) == 1                                         # alert: OK only


# ============================================================================ blockers on a background tab
def _obs(marks, window="Page", flags=None):
    return {"window": window, "marks": [{"id": i, "name": n, "type": t, "box": [0, 0, 10, 10]}
                                        for i, (n, t) in enumerate(marks)], "flags": flags or {}, "hwnd": 42}


def test_blocker_tab_login_from_password_field():
    b = be.detect_blocker_tab(_obs([("Email address", "Edit"), ("Password (password)", "Edit"),
                                    ("Sign in", "Button")], "Sign in – Test Shop",
                                   {"password": ["Password"], "card": [], "captcha": [], "cookie": []}))
    assert isinstance(b, hitl.Blocker) and b.kind == "login" and b.auto_resume and b.hwnd == 42
    assert "password field" in b.evidence
    b = be.detect_blocker_tab(_obs([("Email", "Edit"), ("Password (password)", "Edit")], "Shop"))   # no flags
    assert b is not None and b.kind == "login"


def test_blocker_tab_payment_captcha_cookie_and_none():
    b = be.detect_blocker_tab(_obs([("Card number", "Edit"), ("Pay now", "Button")], "Checkout",
                                   {"card": ["Card number"]}))
    assert b.kind == "payment" and not b.auto_resume
    b = be.detect_blocker_tab(_obs([("Continue", "Button")], "Just a moment",
                                   {"captcha": ["Widget containing a Cloudflare security challenge"]}))
    assert b.kind == "captcha"
    b = be.detect_blocker_tab(_obs([("I'm not a robot (not checked)", "CheckBox")], "Security check"))
    assert b.kind == "captcha"
    b = be.detect_blocker_tab(_obs([("Settings", "Button")], "News", {"cookie": ["We use cookies to improve"]}))
    assert b.kind == "consent"
    b = be.detect_blocker_tab(_obs([("Accept all cookies", "Button"), ("Reject all", "Button")], "Blog"))
    assert b.kind == "consent"
    assert be.detect_blocker_tab(_obs([("Home", "Hyperlink"), ("Reports", "Hyperlink")], "Sales reports")) is None


# ============================================================================ executor logic on a fake tab
class FakeSession:
    def __init__(self):
        self.calls, self.sent, self.state, self.closed = [], [], {"scale": 1.25}, False

    def call(self, method, params=None, timeout=2.0):
        self.calls.append((method, params or {}))
        if method == "Page.navigate":
            return {"frameId": "f"}
        return {}

    def send(self, method, params=None):
        self.sent.append((method, params or {}))
        return 1

    def eval(self, expression, timeout=2.0, await_promise=False):
        return "http://127.0.0.1:8765/stuck.html" if expression == "location.href" else None

    def on(self, *a):
        pass

    def off(self, *a):
        pass


@pytest.fixture
def fake_ex(monkeypatch):
    monkeypatch.setattr(bd, "alive", lambda timeout=0.3: False)
    ex = be.BrowserExecutor(target_id="T", kill_hotkey=False)
    fs = FakeSession()
    monkeypatch.setattr(ex, "_session", lambda: fs)
    monkeypatch.setattr(be.time, "sleep", lambda s: None)
    ex.navigate = lambda url, wait_s=10.0: fs.calls.append(("NAV", {"url": url}))
    return ex, fs


def _keys(fs):
    return [(p["type"], p["key"], p["modifiers"]) for m, p in fs.calls if m == "Input.dispatchKeyEvent"]


def test_fake_click_converts_image_px_to_css_and_presses(fake_ex):
    ex, fs = fake_ex
    ex.ghost = False
    ex.click(250, 125)
    mouse = [(p["type"], p["x"], p["y"], p["button"], p["clickCount"])
             for m, p in fs.calls if m == "Input.dispatchMouseEvent"]
    assert mouse == [("mouseMoved", 200.0, 100.0, "none", 0), ("mousePressed", 200.0, 100.0, "left", 1),
                     ("mouseReleased", 200.0, 100.0, "left", 1)]


def test_fake_double_and_right_click(fake_ex):
    ex, fs = fake_ex
    ex.ghost = False
    ex.double_click(10, 10)
    ex.right_click(10, 10)
    ev = [(p["type"], p["button"], p["clickCount"]) for m, p in fs.calls if m == "Input.dispatchMouseEvent"
          and p["type"] != "mouseMoved"]
    assert ev == [("mousePressed", "left", 1), ("mouseReleased", "left", 1), ("mousePressed", "left", 2),
                  ("mouseReleased", "left", 2), ("mousePressed", "right", 1), ("mouseReleased", "right", 1)]


def test_fake_hold_releases_even_when_killed(fake_ex, monkeypatch):
    ex, fs = fake_ex
    ex.ghost = False
    real_sleep = be.BrowserExecutor._sleep

    def sleep_and_kill(self, s):
        self.killed = True
        real_sleep(self, s)
    monkeypatch.setattr(be.BrowserExecutor, "_sleep", sleep_and_kill)
    with pytest.raises(be.KillSwitch):
        ex.hold(50, 50, 1.2)
    ev = [p["type"] for m, p in fs.calls if m == "Input.dispatchMouseEvent"]
    assert ev == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_fake_scroll_down_is_positive_delta(fake_ex):
    ex, fs = fake_ex
    ex.ghost = False
    ex.scroll(125, 125, -3)
    wheel = [p for m, p in fs.calls if m == "Input.dispatchMouseEvent" and p["type"] == "mouseWheel"]
    assert wheel[0]["deltaY"] == 3 * be.WHEEL_PX and wheel[0]["x"] == 100.0


def test_fake_press_key_with_plus_is_a_hotkey(fake_ex):
    ex, fs = fake_ex
    ex.press_key("ctrl+a")
    assert _keys(fs) == [("rawKeyDown", "Control", 2), ("rawKeyDown", "a", 2), ("keyUp", "a", 2),
                         ("keyUp", "Control", 0)]


def test_fake_type_text_keys_and_insert_text(fake_ex):
    ex, fs = fake_ex
    ex.type_text("Hi é\n")
    assert [k for k in _keys(fs) if k[0] != "keyUp"] == [
        ("rawKeyDown", "Shift", 8), ("keyDown", "H", 8), ("keyDown", "i", 0), ("keyDown", " ", 0),
        ("keyDown", "Enter", 0)]
    assert ("Input.insertText", {"text": "é"}) in fs.calls


def test_fake_address_bar_emulation(fake_ex):
    ex, fs = fake_ex
    ex.hotkey(["ctrl", "l"])
    assert ex._addr == {"text": "http://127.0.0.1:8765/stuck.html", "sel": True}
    ex.type_text("127.0.0.1:8765/index.html")                  # replaces the selected URL
    ex.press_key("backspace")
    ex.type_text("l")
    assert ex._addr["text"] == "127.0.0.1:8765/index.html"
    assert not _keys(fs)                                        # nothing went to the page
    ex.press_key("enter")
    assert ex._addr is None and ("NAV", {"url": "http://127.0.0.1:8765/index.html"}) in fs.calls
    ex.hotkey(["ctrl", "t"])
    assert ex._addr == {"text": "", "sel": False}
    ex.press_key("escape")
    assert ex._addr is None


def test_fake_refused_and_emulated_browser_keys(fake_ex):
    ex, fs = fake_ex
    with pytest.raises(RuntimeError, match="desktop shortcut"):
        ex.hotkey(["win", "r"])
    with pytest.raises(RuntimeError, match="browser-window shortcut"):
        ex.hotkey(["ctrl", "w"])
    ex.press_key("f5")
    assert ("Page.reload", {"ignoreCache": False}) in fs.calls
    with pytest.raises(RuntimeError):
        ex.open_app("notepad")


def test_fake_dialog_answered_by_enter_click_and_prompt_typing(fake_ex):
    ex, fs = fake_ex
    fs.state["dialog"] = {"type": "prompt", "message": "Name?", "default": "", "url": ""}
    ex.type_text("Grape")
    assert ex._prompt_text == "Grape" and not _keys(fs)
    ex.press_key("enter")
    assert ("Page.handleJavaScriptDialog", {"accept": True, "promptText": "Grape"}) in fs.calls
    fs.state["dialog"] = {"type": "confirm", "message": "Sure?", "default": "", "url": ""}
    fs.state["dialog_marks"] = [{"name": "OK", "type": "DialogButton", "box": [0, 0, 10, 10], "answer": True},
                                {"name": "Cancel", "type": "DialogButton", "box": [20, 0, 30, 10], "answer": False}]
    ex.click(25, 5)
    assert fs.calls[-1] == ("Page.handleJavaScriptDialog", {"accept": False})
    fs.state["dialog"] = {"type": "alert", "message": "Hi", "default": "", "url": ""}
    with pytest.raises(RuntimeError, match="dialog is open"):
        ex.click(500, 500)


def test_fake_stop_event_and_kill(fake_ex):
    ex, fs = fake_ex
    ex.stop_event = threading.Event()
    ex.stop_event.set()
    with pytest.raises(KeyboardInterrupt):
        ex.click(1, 1)
    ex.stop_event = None
    ex.killed = True
    with pytest.raises(be.KillSwitch):
        ex.type_text("x")
    assert not [c for c in fs.calls if c[0].startswith("Input.")]


def test_executor_surface_matches_actions_executor():
    from rbot.desktop import actions
    for name in ("click", "double_click", "right_click", "move", "drag", "scroll", "type_text", "press_key",
                 "hotkey", "wait", "stop", "open_app", "_check", "takeover_handler", "human_turn",
                 "takeover_pending"):
        assert hasattr(actions.Executor, name) and hasattr(be.BrowserExecutor, name), name
    assert be.BrowserExecutor.kind == "browser"
    assert be.KillSwitch is actions.KillSwitch and be.HumanTakeover is actions.HumanTakeover


# ============================================================================ LIVE (BUDDY_LIVE=1)
LIVE = os.environ.get("BUDDY_LIVE") == "1"
from rbot import RUNS  # noqa: E402
_u32 = ctypes.windll.user32


def _cursor():
    p = wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _fg():
    return int(_u32.GetForegroundWindow() or 0)


COVER = r"""
import sys, tkinter as tk
l, t, r, b = map(int, sys.argv[1:5])
w = tk.Tk(); w.title("Buddy test cover - you can keep working")
w.geometry(f"{r - l}x{b - t}+{l}+{t}"); w.configure(bg="#1e1b2e")
tk.Label(w, text="Buddy browser test is running BEHIND this window.\nThe agent works in the background; your mouse is yours.",
         fg="white", bg="#1e1b2e", font=("Segoe UI", 18)).pack(expand=True)
w.after(120000, w.destroy); w.mainloop()
"""


@pytest.mark.skipif(not LIVE, reason="set BUDDY_LIVE=1 for the live buddy-browser test")
def test_live_background_tab():
    from tests.live import hitl_server
    from pynput import mouse

    base = hitl_server.start()
    started = not bd.alive()
    cursor0 = _cursor()
    inj_events = []                                              # OS mouse events injected by anyone (should be 0)

    real_moves = [0]                                             # the user's own mouse movement (not ours)

    def on_move(x, y, injected=False):
        if injected:
            inj_events.append(("move", x, y))
        else:
            real_moves[0] += 1

    def on_click(x, y, button, pressed, injected=False):
        if injected:
            inj_events.append(("click", x, y))
    lst = mouse.Listener(on_move=on_move, on_click=on_click)
    lst.start()
    cover = ex = None
    tid = None
    timings = []
    report = []
    try:
        assert bd.launch(base + "index.html", front=False) if started else True
        launched_pid = bd.LAST_LAUNCH_PID if started else None
        report.append(f"buddy browser {'started (pid %s)' % launched_pid if started else 'already running'}; "
                      f"bg flags: {bd.has_bg_flags()}")
        # the agent's own window, created behind everything (the started browser's first window stays as is)
        ex = be.BrowserExecutor.in_new_window(base + "stuck.html?reset=1", kill_hotkey=True,
                                              download_dir=RUNS / "bg_downloads")
        tid = ex.target_id
        time.sleep(1.0)
        report.append(f"agent window opened in the background; foreground is buddy: {bd.is_buddy_window(_fg())}")
        # cover the agent's window with a normal window that has the foreground
        hw = bd.buddy_hwnd_for("Sales reports – Test Shop Admin")
        r = wintypes.RECT()
        _u32.GetWindowRect(hw, ctypes.byref(r))
        cover = subprocess.Popen([sys.executable, "-c", COVER, str(r.left), str(r.top), str(r.right), str(r.bottom)])
        time.sleep(1.2)
        ch = _u32.FindWindowW(None, "Buddy test cover - you can keep working")
        bd.focus(ch)
        time.sleep(0.3)
        fg0 = _fg()
        report.append(f"foreground during test: hwnd {fg0} (cover={fg0 == ch}, buddy={bd.is_buddy_window(fg0)})")
        assert not bd.is_buddy_window(fg0)

        s = ex._session()
        dom = lambda js: s.eval(js, timeout=2.0)  # noqa: E731

        def observe():
            t0 = time.time()
            o = ex.observe()
            timings.append(time.time() - t0)
            assert not bd.is_buddy_window(_fg()), "the buddy browser came to the front"
            return o

        def shot(name, crop_around=None):
            im = bd._decode(s.call("Page.captureScreenshot", {"format": "png"}, timeout=3)["data"])
            if crop_around:
                x, y = crop_around
                im = im.crop((max(0, int(x) - 260), max(0, int(y) - 190), int(x) + 260, int(y) + 190))
            im.save(RUNS / f"bg_ghost_{name}.png")

        def center(m):
            b = m["box"]
            return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2

        def find(o, pred):
            return next(m for m in o["marks"] if pred(m["name"].lower()))

        # ---- 1. stuck.html: kebab by box, press-and-hold "Download report" 1.2 s
        o = observe()
        assert o["background"] and o["page_source"] == "dom" and o["origin"] == (0, 0)
        assert o["vis"] == "visible", "occluded tab stopped rendering (restart the buddy browser with BG_FLAGS)"
        assert dom("document.getElementById('downloads').textContent") == "Downloads this session: 0"
        o["marked"].save(RUNS / "bg_observe_stuck.png")
        home = find(o, lambda n: n == "home")
        ex.move(*center(home))                                      # park far away -> long flight to the dots
        kebab = find(o, lambda n: "more options" in n)
        kx, ky = center(kebab)
        th = threading.Thread(target=ex.click, args=(kx, ky))
        th.start()
        time.sleep(0.07)
        shot("glide_mid")
        th.join()
        time.sleep(0.05)
        shot("click_ripple", (kx, ky))
        time.sleep(0.4)
        o = observe()
        assert all("ghost" not in m["name"].lower() for m in o["marks"])
        menu = next(m for m in o["marks"] if m["type"] == "Button" and "canvas" in m["name"])
        b = menu["box"]
        hx, hy = b[0] + 60 * o["scale"], b[1] + (b[3] - b[1]) * 2.5 / 3     # third row: "Download report"
        th = threading.Thread(target=ex.hold, args=(hx, hy, 1.2))
        th.start()
        time.sleep(0.25 + 0.6)
        shot("hold_ring", (hx, hy))
        th.join()
        time.sleep(0.3)
        got = dom("document.getElementById('downloads').textContent")
        report.append(f"stuck.html after hold 1.2 s: {got!r}")
        assert got == "Downloads this session: 1"

        # ---- 2. login.html: fill (test page, fake account) + blocker detection without the window in front
        ex.navigate(base + "login.html")
        o = observe()
        blk = be.detect_blocker_tab(o)
        report.append(f"login.html blocker: {blk.kind if blk else None} ({blk.evidence if blk else ''})")
        assert blk is not None and blk.kind == "login"
        ex.click(*center(find(o, lambda n: n.startswith("email"))))
        ex.type_text("grape.buddy@example.com")
        ex.click(*center(find(o, lambda n: "password" in n and "forgot" not in n)))
        th = threading.Thread(target=ex.type_text, args=("not-a-real-pw-123",))
        th.start()
        time.sleep(0.12)
        shot("typing_caret", center(find(o, lambda n: "password" in n and "forgot" not in n)))
        th.join()
        vals = dom("[document.getElementById('email').value, document.getElementById('password').value.length]")
        report.append(f"login.html email={vals[0]!r} password length={vals[1]}")
        assert vals == ["grape.buddy@example.com", len("not-a-real-pw-123")]
        ex.press_key("enter")                                        # submit -> welcome.html after 1 s
        deadline = time.time() + 5
        while time.time() < deadline and "welcome" not in (dom("location.href") or ""):
            time.sleep(0.2)
        assert "welcome.html" in dom("location.href")

        # ---- 3. contact.html: type, native <select> via the in-page picker, checkbox, send
        ex.navigate(base + "contact.html")
        o = observe()
        ex.click(*center(find(o, lambda n: n == "your name")))
        ex.type_text("Grape Buddy")
        ex.click(*center(find(o, lambda n: n == "email address")))
        ex.type_text("grape.buddy@example.com")
        ex.click(*center(find(o, lambda n: n.startswith("subject"))))
        time.sleep(0.2)
        o = observe()
        o["marked"].save(RUNS / "bg_observe_picker.png")
        opt = find(o, lambda n: n.startswith("returns"))
        assert opt["type"] == "ListItem"
        ex.click(*center(opt))
        assert dom("document.getElementById('subject').value") == "Returns"
        ex.scroll(o["size"][0] / 2, o["size"][1] / 2, -5)           # the message box is below the fold
        time.sleep(0.4)
        assert dom("scrollY") > 0
        o = observe()
        ex.click(*center(find(o, lambda n: n == "message")))
        ex.type_text("Hello from the background!\nCafé order #42 ✓ (A&B?)")
        ex.click(*center(find(o, lambda n: n.startswith("send me a copy"))))
        vals = dom("['name','email','subject','message'].map(i => document.getElementById(i).value)"
                   ".concat([document.getElementById('copy').checked])")
        report.append(f"contact.html values: {vals}")
        assert vals == ["Grape Buddy", "grape.buddy@example.com", "Returns",
                        "Hello from the background!\nCafé order #42 ✓ (A&B?)", True]
        ex.click(*center(find(o, lambda n: n == "message")))
        ex.press_key("ctrl+a")                                      # select all in the textarea, then retype
        ex.type_text("Short message")
        assert dom("document.getElementById('message').value") == "Short message"
        o = observe()
        ex.click(*center(find(o, lambda n: n == "send message")))
        time.sleep(0.3)
        sent = dom("document.getElementById('sent').textContent")
        report.append(f"contact.html sent: {sent!r}")
        assert "Subject: Returns" in sent and "Message: Short message" in sent

        # ---- 4. JS dialog handled without an OS window
        dom("setTimeout(() => { window.__ans = confirm('Proceed with the test?'); }, 50); 1")
        time.sleep(0.4)
        d = ex.pending_dialog()
        assert d and d["type"] == "confirm"
        o = observe()
        o["marked"].save(RUNS / "bg_observe_dialog.png")
        ex.click(*center(find(o, lambda n: n.startswith("ok"))))
        assert ex.pending_dialog() is None and dom("window.__ans") is True
        report.append("confirm() dialog: seen via CDP, answered OK by clicking its painted button")

        # ---- 5. ctrl+l address bar emulation + a real download into the download dir
        ex.hotkey(["ctrl", "l"])
        ex.type_text(base.replace("http://", "") + "stuck.html")
        shot("address_bar")
        ex.press_key("enter")
        assert dom("location.pathname") == "/stuck.html"
        assert dom("document.getElementById('downloads').textContent") == "Downloads this session: 1"
        dom("(() => { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['buddy,test\\n1,2'],"
            " {type: 'text/csv'})); a.download = 'bg_test.csv'; a.id = 'dl'; a.textContent = 'Download CSV';"
            " a.style.cssText = 'position:fixed;left:40px;top:120px;font-size:20px;background:#fff;padding:6px';"
            " document.body.appendChild(a); return 1; })()")
        o = observe()
        ex.click(*center(find(o, lambda n: n == "download csv")))
        deadline = time.time() + 5
        while time.time() < deadline and not any(x["state"] == "completed" for x in ex.last_downloads()):
            time.sleep(0.2)
        dls = ex.last_downloads()
        report.append(f"downloads: {[(x['filename'], x['state']) for x in dls]}")
        assert dls and dls[0]["state"] == "completed" and Path(dls[0]["path"]).exists()
        Path(dls[0]["path"]).unlink()

        # ---- 6. more blockers seen in the background (no UIA, window covered)
        for page, kind in (("cookies.html", "consent"), ("captcha.html", "captcha"), ("checkout.html", None)):
            ex.navigate(base + page)
            time.sleep(0.9)                                         # the cookie banner appears after 600 ms
            blk = be.detect_blocker_tab(observe())
            report.append(f"{page} blocker: {blk.kind if blk else None} ({blk.evidence if blk else ''})")
            assert (blk.kind if blk else None) == kind

        # ---- proof: the real pointer never moved, no injected OS mouse event, browser never came to front
        assert not bd.is_buddy_window(_fg())
        report.append(f"observe_tab timings (s): {[round(t, 3) for t in timings]}")
        timings.sort()
        assert timings[len(timings) // 2] < 0.4
    finally:
        lst.stop()
        cursor1 = _cursor()
        report.append(f"GetCursorPos before={cursor0} after={cursor1}; injected OS mouse events: {len(inj_events)}; "
                      f"real (user) mouse moves seen: {real_moves[0]}")
        if ex is not None:
            ex.stop()
            ex.target_id = tid
            ex.close_window()
        if cover is not None:
            cover.kill()
        if started:
            bd.close()
        hitl_server.stop()
        print("\n".join(["", "LIVE REPORT"] + report))
    assert inj_events == [], "something injected OS mouse input"
    if real_moves[0] == 0:                                       # nobody touched the mouse: it must not have moved
        assert cursor1 == cursor0, "the real mouse pointer moved during the test"


@pytest.mark.skipif(os.environ.get("BUDDY_LIVE_FRONT") != "1",
                    reason="set BUDDY_LIVE_FRONT=1: brings the buddy window to the front for ~1 s (handover)")
def test_live_handover_front_and_back():
    from tests.live import hitl_server
    base = hitl_server.start()
    started = not bd.alive()
    ex = None
    try:
        ex = be.BrowserExecutor.in_new_window(base + "login.html", kill_hotkey=False)
        time.sleep(1.0)
        user_fg = _fg()
        assert not bd.is_buddy_window(user_fg)
        assert ex.bring_to_front()
        time.sleep(0.8)
        assert bd.is_buddy_window(_fg())
        ex.send_to_back()
        time.sleep(0.5)
        back = _fg()
        print(f"\nhandover: user window {user_fg} -> buddy in front -> back to {back}")
        assert back == user_fg
        o = ex.observe()                                            # still rendering behind the user's window
        assert o["vis"] == "visible" and o["timing"]["total"] < 1.0
        assert be.detect_blocker_tab(o).kind == "login"
    finally:
        if ex is not None:
            tid = ex.target_id
            ex.stop()
            ex.target_id = tid
            ex.close_window()
        if started:
            bd.close()
        hitl_server.stop()
