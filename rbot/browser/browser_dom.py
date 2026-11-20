"""The "buddy browser": an Edge instance with its own profile and the Chrome DevTools Protocol switched on, so the
agent can read clickable elements straight from the web page (things Windows UI Automation can't see, e.g. a
<div> with a click handler).

  python -m rbot.browser.browser_dom launch [url]     start the buddy browser (or open url in a new tab of it) and bring it to front
  python -m rbot.browser.browser_dom marks            print the marks of the buddy-browser page that is in front
  python -m rbot.browser.browser_dom observe [id]     background observation of a tab (observe_tab), saves runs/observe_tab.png
  python -m rbot.browser.browser_dom close            close the buddy browser (CDP Browser.close)

Edge/Chrome 136+ refuse --remote-debugging-port on the default profile, hence the separate profile in
<project>/.buddy_browser (sign in there once, it persists).

perception.observe() calls fetch_raw() (in a thread, while it walks UIA) and to_marks() afterwards;
page_marks() does both in one go.

Background mode (PLAN phase 3, used by browser_exec.py): observe_tab(target_id) reads one tab through CDP only -
screenshot (Page.captureScreenshot) + DOM marks in image px, no UIA, the window may be covered. For that the
browser is launched with BG_FLAGS (keep rendering while occluded); an instance started before them must be
restarted once (close + launch). new_window(url) opens the agent's own window behind the user's windows.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from ctypes import wintypes

from rbot import ROOT

PORT = 9222
HOST = f"http://127.0.0.1:{PORT}"
PROFILE = ROOT / ".buddy_browser"
EDGE_PATHS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
BUDGET_S = 0.4                       # whole CDP round trip; on timeout perception falls back to UIA
MAX_MARKS = 150

_u32 = ctypes.WinDLL("user32")       # own function objects: don't change restypes other modules rely on
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_buddy_pid_cache = {}                # pid -> bool  (is this process the buddy browser?)


# ---------------------------------------------------------------- small helpers
def find_edge():
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def _http(method, path, timeout=0.3):
    import requests
    r = requests.request(method, HOST + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def alive(timeout=0.3):
    try:
        return bool(_http("GET", "/json/version", timeout))
    except Exception:  # noqa: BLE001
        return False


def targets(timeout=0.3):
    """Open page tabs of the buddy browser ([] if it isn't running)."""
    try:
        return [t for t in _http("GET", "/json/list", timeout) if t.get("type") == "page"]
    except Exception:  # noqa: BLE001
        return []


def _pid_of(hwnd):
    pid = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def is_buddy_window(hwnd):
    """Does this top-level window belong to the buddy browser (msedge started with our --user-data-dir)?"""
    if not hwnd:
        return False
    pid = _pid_of(hwnd)
    if pid in _buddy_pid_cache:
        return _buddy_pid_cache[pid]
    ok = False
    try:
        import psutil
        p = psutil.Process(pid)
        ok = p.name().lower() in ("msedge.exe", "chrome.exe") and \
            any(PROFILE.name.lower() in a.lower() for a in p.cmdline())
    except Exception:  # noqa: BLE001
        ok = False
    _buddy_pid_cache[pid] = ok
    return ok


def _buddy_windows():
    """Visible titled top-level windows of the buddy browser, front to back."""
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if _u32.IsWindowVisible(h) and _u32.GetWindowTextLengthW(h) > 0:
            buf = ctypes.create_unicode_buffer(32)
            _u32.GetClassNameW(h, buf, 32)
            if buf.value == "Chrome_WidgetWin_1" and is_buddy_window(h):
                out.append(int(h))
        return True
    _u32.EnumWindows(EnumProc(cb), 0)
    return out


def focus(hwnd):
    """Bring hwnd to the front without touching mouse/keyboard (thread-input trick, then the shell switcher)."""
    k = ctypes.windll.kernel32
    if not hwnd:
        return False
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, 9)                                   # SW_RESTORE
    for attempt in range(2):
        if attempt == 0:
            fg_thread = _u32.GetWindowThreadProcessId(_u32.GetForegroundWindow(), None)
            me = k.GetCurrentThreadId()
            attached = bool(fg_thread and fg_thread != me and _u32.AttachThreadInput(me, fg_thread, True))
            _u32.BringWindowToTop(hwnd)
            _u32.SetForegroundWindow(hwnd)
            if attached:
                _u32.AttachThreadInput(me, fg_thread, False)
        else:
            _u32.SwitchToThisWindow(hwnd, True)
        for _ in range(10):
            if int(_u32.GetForegroundWindow() or 0) == int(hwnd):
                return True
            time.sleep(0.05)
    return False


def bring_to_front(target_id=None):
    """Activate the tab (CDP) and raise the buddy browser window."""
    if target_id:
        try:
            t = next((t for t in targets() if t["id"] == target_id), None)
            if t:
                _cdp(t["webSocketDebuggerUrl"], "Page.bringToFront", {}, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
    wins = _buddy_windows()
    return focus(wins[0]) if wins else False


# ---------------------------------------------------------------- launch / open
# Keep rendering (screenshots, rAF, timers) while the window is covered / behind other windows: the background
# agent (browser_exec.py) works in the buddy browser while the user uses another app. A buddy browser that was
# started BEFORE these flags existed must be closed and restarted once (python -m rbot.browser.browser_dom close; launch).
# Minimised windows still stop rendering: never minimise it (send_window_to_back restores without activating).
BG_FLAGS = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling"]
LAST_LAUNCH_PID = None               # pid of the msedge process launch() started (None: it was already running)


def quiet_profile():
    """Buddy profile prefs: no autofill dropdowns, no "Save your password?" bubble, no card saving - they pop up
    over fields the agent types into and waste steps. Only while the buddy browser is closed (Edge rewrites
    Preferences on exit)."""
    import json
    f = PROFILE / "Default" / "Preferences"
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return False
    want = {("autofill", "profile_enabled"): False, ("autofill", "credit_card_enabled"): False,
            ("autofill", "enabled"): False, ("credentials_enable_service",): False,
            ("credentials_enable_autosignin",): False, ("profile", "password_manager_enabled"): False}
    changed = False
    for path, val in want.items():
        d = data
        for k in path[:-1]:
            d = d.setdefault(k, {}) if isinstance(d.get(k, {}), dict) else d
        if d.get(path[-1]) != val:
            d[path[-1]], changed = val, True
    if changed:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            return False
    return True


def launch_args(exe, url=None):
    return [exe, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "--no-first-run",
            "--no-default-browser-check", "--start-maximized", *BG_FLAGS, url or "about:blank"]


def has_bg_flags():
    """Was the running buddy browser started with BG_FLAGS? (None = can't tell / not running)"""
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            cl = p.info.get("cmdline") or []
            if (p.info.get("name") or "").lower() == "msedge.exe" and \
                    any(PROFILE.name.lower() in a.lower() for a in cl) and not any(a.startswith("--type=") for a in cl):
                return all(f in cl for f in BG_FLAGS)
    except Exception:  # noqa: BLE001
        pass
    return None


def launch(url=None, wait_s=10.0, front=True):
    """Start the buddy browser on url (or open url in a new tab if it already runs). front=True brings it to the
    front (live mode); front=False (background mode) leaves the window that was in front where it was."""
    global LAST_LAUNCH_PID
    prev_fg = int(_u32.GetForegroundWindow() or 0)
    if alive():
        tid = None
        if url:
            try:
                tid = _http("PUT", "/json/new?" + urllib.parse.quote(url, safe=""), timeout=2.0).get("id")
            except Exception:  # noqa: BLE001
                return False
        if front:
            bring_to_front(tid)
        else:
            _settle_behind(prev_fg)
        return True
    exe = find_edge()
    if not exe:
        return False
    PROFILE.mkdir(exist_ok=True)
    quiet_profile()
    try:
        p = subprocess.Popen(launch_args(exe, url), close_fds=True,
                             creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        LAST_LAUNCH_PID = p.pid
    except OSError:
        return False
    end = time.time() + wait_s
    while time.time() < end:
        if alive() and _buddy_windows():
            break
        time.sleep(0.2)
    else:
        return False
    time.sleep(0.3)
    if front:
        bring_to_front()
    else:
        _settle_behind(prev_fg)
    return True


def _settle_behind(prev_fg, wait_s=1.5):
    """Background mode: a new buddy window/tab may have grabbed the foreground -> give it back to prev_fg and push
    the buddy windows to the bottom of the z-order (NOT minimised: minimised windows stop rendering)."""
    end = time.time() + wait_s
    while time.time() < end:                      # the new window shows up a moment after the tab is created
        fg = int(_u32.GetForegroundWindow() or 0)
        if fg and fg != prev_fg and is_buddy_window(fg):
            break
        time.sleep(0.05)
    for h in _buddy_windows():
        send_window_to_back(h)
    if prev_fg and not is_buddy_window(prev_fg) and int(_u32.GetForegroundWindow() or 0) != prev_fg:
        focus(prev_fg)


def send_window_to_back(hwnd):
    """Restore a minimised window without activating it, then put it at the bottom of the z-order."""
    if not hwnd:
        return False
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, 4)                                  # SW_SHOWNOACTIVATE
    # HWND_BOTTOM=1; SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE = 0x13
    return bool(_u32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(1), 0, 0, 0, 0, 0x13))


def open_url(url):
    """What the agent's open_url tool calls: open url in the buddy browser. False if that's not possible."""
    if not re.match(r"^(https?://|about:)", str(url or "")):
        return False
    try:
        return launch(url)
    except Exception:  # noqa: BLE001
        return False


def close():
    """Close the buddy browser (all its windows) via CDP."""
    try:
        ws = _http("GET", "/json/version")["webSocketDebuggerUrl"]
        _cdp(ws, "Browser.close", {}, timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- CDP
def _cdp(ws_url, method, params, timeout=BUDGET_S):
    import websocket
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        end = time.time() + timeout
        while True:
            ws.settimeout(max(0.01, end - time.time()))
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


class CDPSession:
    """A persistent CDP websocket (page or browser target) with a reader thread: call() waits for its own reply,
    events go to handlers registered with on(method, fn). Thread-safe. Used by observe_tab and browser_exec."""

    def __init__(self, ws_url, name="cdp"):
        import threading
        import websocket
        self.ws_url = ws_url
        self.ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        self.ws.settimeout(None)
        self._lock = threading.Lock()
        self._next = 0
        self._wait = {}                              # id -> [Event, reply]
        self._handlers = {}                          # method -> [fn]
        self.closed = False
        self.state = {}                              # free-form per-target state (dialog, last image, ...)
        self._t = threading.Thread(target=self._reader, name=name, daemon=True)
        self._t.start()

    def _reader(self):
        while not self.closed:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001  (socket closed / tab gone)
                break
            if "id" in msg:
                w = self._wait.pop(msg["id"], None)
                if w:
                    w[1] = msg
                    w[0].set()
            elif "method" in msg:
                for fn in list(self._handlers.get(msg["method"], ())):
                    try:
                        fn(msg.get("params", {}))
                    except Exception:  # noqa: BLE001
                        pass
        self.closed = True
        for w in list(self._wait.values()):          # wake everybody waiting
            w[0].set()
        self._wait.clear()

    def on(self, method, fn):
        self._handlers.setdefault(method, []).append(fn)

    def off(self, method, fn):
        try:
            self._handlers.get(method, []).remove(fn)
        except ValueError:
            pass

    def send(self, method, params=None):
        """Fire and forget. Returns the message id."""
        import threading
        with self._lock:
            self._next += 1
            mid = self._next
            self._wait[mid] = [threading.Event(), None]
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        return mid

    def call(self, method, params=None, timeout=2.0):
        if self.closed:
            raise ConnectionError("CDP session closed")
        mid = self.send(method, params)
        w = self._wait.get(mid)
        if w is None:                                 # reply already arrived (fast path race)
            return {}
        if not w[0].wait(timeout):
            self._wait.pop(mid, None)
            raise TimeoutError(f"CDP {method} timed out after {timeout:.1f}s")
        msg = w[1]
        if msg is None:
            raise ConnectionError("CDP session closed")
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def eval(self, expression, timeout=2.0, await_promise=False):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": await_promise, "timeout": int(timeout * 1000)},
                        timeout=timeout + 0.2)
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text", "js error"))
        return res.get("result", {}).get("value")

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


_sessions = {}                                       # target id -> CDPSession (shared by observe_tab + executor)


def _track_dialogs(s):
    """JS dialogs block the page (Runtime.evaluate hangs): remember them so callers can answer them."""
    s.call("Page.enable", {}, timeout=2.0)

    def opening(p):
        s.state["dialog"] = {"type": p.get("type", "alert"), "message": p.get("message", ""),
                             "default": p.get("defaultPrompt", ""), "url": p.get("url", ""), "t": time.time()}

    def closed(_p):
        s.state.pop("dialog", None)
    s.on("Page.javascriptDialogOpening", opening)
    s.on("Page.javascriptDialogClosed", closed)


def get_session(target_id, timeout=0.5):
    """Live CDPSession for a page target (cached, reconnects if the old one died)."""
    s = _sessions.get(target_id)
    if s is not None and not s.closed:
        return s
    t = next((t for t in targets(timeout=timeout) if t["id"] == target_id), None)
    if not t or not t.get("webSocketDebuggerUrl"):
        raise LookupError(f"no buddy-browser tab {target_id}")
    s = CDPSession(t["webSocketDebuggerUrl"], name=f"cdp-{target_id[:6]}")
    _track_dialogs(s)
    _sessions[target_id] = s
    return s


def close_sessions():
    for s in list(_sessions.values()):
        s.close()
    _sessions.clear()


def choose_target(tabs, target_id=None):
    """Which page target to work in. tabs = /json/list order (Chromium lists the most recently activated first).
    Explicit id -> that tab (or None if it's gone); else the most recent page, skipping DevTools windows."""
    pages = [t for t in tabs if t.get("type", "page") == "page"]
    if target_id:
        return next((t for t in pages if t.get("id") == target_id), None)
    return next((t for t in pages if not str(t.get("url", "")).startswith("devtools://")), None)


def new_window(url="about:blank", timeout=3.0):
    """Open url in a NEW buddy-browser window, created in the background (checked: doesn't take the foreground).
    -> target id. The background agent wants its tab to be the only (= active) tab of its window: a tab that is
    switched to with Target.activateTarget while its window is in the background renders frames very slowly
    (input acks / screenshots take ~1 s or time out, measured on Edge 157), a tab alone in its window is fine."""
    ws = _http("GET", "/json/version", timeout=1.0)["webSocketDebuggerUrl"]
    return _cdp(ws, "Target.createTarget", {"url": url, "newWindow": True, "background": True},
                timeout=timeout)["targetId"]


def buddy_hwnd_for(title):
    """The buddy-browser window whose active tab has this title (front-most first), else the front-most one."""
    wins = _buddy_windows()
    for h in wins:
        buf = ctypes.create_unicode_buffer(512)
        _u32.GetWindowTextW(h, buf, 512)
        if title and match_target(buf.value, [{"title": title}]):
            return h
    return wins[0] if wins else 0


# Hidden during the screenshot, so the model never sees the agent's own ghost cursor (browser_exec injects it).
_GHOST_HIDE = "(()=>{const g=window.__buddyGhost; if(g&&g.capture) g.capture(true);})()"
_GHOST_SHOW = "(()=>{const g=window.__buddyGhost; if(g&&g.capture) g.capture(false);})()"
# Blocker hints read from the DOM (password / card fields, captcha frames, cookie banners) for detect_blocker_tab.
FLAGS_JS = r"""
(() => {
  const VW = innerWidth, VH = innerHeight;
  const vis = el => { const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > VH || r.left > VW) return false;
    return !el.checkVisibility || el.checkVisibility({opacityProperty: true, visibilityProperty: true}); };
  const lab = el => ((el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) ||
                      el.placeholder || el.title || el.name || el.id || '') + '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const pw = [...document.querySelectorAll('input[type=password]')].filter(vis).map(lab).slice(0, 4);
  const CARD = /(cc-number|cc-csc|cc-exp|card.?number|cardnumber|cvv|cvc|security.?code)/i;
  const card = [...document.querySelectorAll('input,iframe')].filter(e => vis(e) && CARD.test(
      [e.getAttribute('autocomplete'), e.name, e.id, e.getAttribute('aria-label'), e.placeholder, e.title,
       e.tagName === 'IFRAME' ? e.src : ''].join(' '))).map(lab).slice(0, 4);
  const CAP = /(recaptcha\/api2\/(anchor|bframe)|recaptcha\/enterprise\/(anchor|bframe)|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs|funcaptcha)/i;
  const captcha = [...document.querySelectorAll('iframe')].filter(f => vis(f) && CAP.test(f.src || '') &&
      !/size=invisible/.test(f.src || '')).map(f => (f.title || f.src).slice(0, 60)).slice(0, 3);
  const cookie = [];
  for (const el of document.querySelectorAll('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[aria-label*=cookie i],[id*=gdpr i],[class*=gdpr i]')) {
    if (cookie.length >= 2) break;
    if (!vis(el)) continue;
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (!(cs.position === 'fixed' || cs.position === 'sticky' || el.getAttribute('role') === 'dialog' ||
          el.getAttribute('aria-modal') === 'true')) continue;
    if (r.width * r.height < 0.03 * VW * VH) continue;
    cookie.push((el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80));
  }
  return {password: pw, card: card, captcha: captcha, cookie: cookie};
})()
"""


def _tab_js(max_marks, budget_ms):
    """Collector + blocker flags in one evaluate; hides the ghost and waits a frame so the capture is clean."""
    return ("(async () => {" + _GHOST_HIDE + ";"
            "const r = " + (JS % {"maxn": max_marks, "tbudget": budget_ms}).strip() + ";"
            "try { r.flags = " + FLAGS_JS.strip() + "; } catch (e) { r.flags = null; }"
            "await new Promise(res => { requestAnimationFrame(() => res()); setTimeout(res, 50); });"
            "return r; })()")


def _decode(b64):
    import base64
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64)))
    im.load()
    return im.convert("RGB")


def raw_to_image_marks(raw, img_size):
    """Collector output (CSS px) -> marks with boxes in IMAGE px (scale = image width / innerWidth), clipped."""
    W, H = img_size
    vw = float(raw.get("vw") or 0) or W
    s = W / vw
    marks = []
    for x1, y1, x2, y2, typ, name in raw.get("marks", []):
        b = [max(0, round(x1 * s)), max(0, round(y1 * s)), min(W, round(x2 * s)), min(H, round(y2 * s))]
        if b[2] - b[0] >= 2 and b[3] - b[1] >= 2 and name:
            marks.append({"name": str(name)[:80], "type": typ, "box": b})
    return marks, s


def observe_tab(target_id=None, max_marks=100, fmt="jpeg", quality=88, timeout=1.5):
    """Background observation of a buddy-browser tab (no OS window needs to be in front, no UIA).
    Same schema as perception.observe(): image (viewport, device px), marked, marks (boxes in image px), size,
    origin (0, 0), window (page title), page_source "dom", plus background=True, url, target_id, scale (image px
    per CSS px), flags (DOM blocker hints) and dialog (a pending JS alert/confirm/prompt or None).
    While a JS dialog is open the page can't run scripts: the last image is reused with the dialog drawn on top
    and its buttons as marks (type "DialogButton"; browser_exec.click on them answers the dialog)."""
    from rbot.desktop import grounding as gt
    t0 = time.time()
    tabs = targets(timeout=0.5)
    t = choose_target(tabs, target_id)
    if t is None:
        raise LookupError("no buddy-browser tab" + (f" {target_id}" if target_id else ""))
    s = get_session(t["id"])
    tm = {}
    dialog = s.state.get("dialog")
    raw = None
    if not dialog:
        try:
            raw = s.eval(_tab_js(max_marks, int(BUDGET_S * 1000 * 0.6)), timeout=timeout, await_promise=True)
        except TimeoutError:
            dialog = s.state.get("dialog")           # a dialog popped up meanwhile
            if not dialog:
                raise
    tm["dom"] = time.time() - t0
    img = None
    if not dialog:
        params = {"format": fmt, "captureBeyondViewport": False, "optimizeForSpeed": True}
        if fmt == "jpeg":
            params["quality"] = quality
        try:
            img = _decode(s.call("Page.captureScreenshot", params, timeout=timeout)["data"])
        finally:
            try:
                s.send("Runtime.evaluate", {"expression": _GHOST_SHOW})
            except Exception:  # noqa: BLE001
                pass
        s.state["last_image"] = img
    tm["shot"] = time.time() - t0
    if dialog:
        img, marks = _draw_dialog(s.state.get("last_image"), dialog)
        s.state["dialog_marks"] = marks
        scale, url, title, flags = s.state.get("scale", 1.0), t.get("url", ""), t.get("title", ""), None
    else:
        raw = raw or {}
        marks, scale = raw_to_image_marks(raw, img.size)
        s.state["scale"] = scale
        s.state.pop("dialog_marks", None)
        url, title, flags = raw.get("url", t.get("url", "")), raw.get("title", t.get("title", "")), raw.get("flags")
    marks = marks[:max_marks]
    for i, m in enumerate(marks):
        m["id"] = i
    tm["total"] = time.time() - t0
    return {"image": img, "marked": gt.draw_marks(img, marks), "marks": marks, "size": img.size, "origin": (0, 0),
            "window": title, "page_source": "dom", "background": True, "url": url, "target_id": t["id"],
            "scale": scale, "flags": flags, "dialog": dialog, "hwnd": buddy_hwnd_for(title),
            "vis": (raw or {}).get("vis"), "timing": {k: round(v, 3) for k, v in tm.items()}}


def _draw_dialog(base, dialog, size=(1280, 720)):
    """Paint a JS dialog over the last screenshot -> (image, marks). OK/Cancel boxes answer it in browser_exec."""
    from PIL import Image, ImageDraw, ImageFont
    img = base.copy() if base is not None else Image.new("RGB", size, (245, 246, 250))
    W, H = img.size
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, W, H], fill=(0, 0, 0, 90))
    try:
        f, fb = ImageFont.truetype("segoeui.ttf", 18), ImageFont.truetype("segoeuib.ttf", 18)
    except OSError:
        f = fb = ImageFont.load_default()
    w, h = min(560, W - 40), 200 if dialog["type"] == "prompt" else 170
    x, y = (W - w) // 2, max(20, H // 6)
    d.rounded_rectangle([x, y, x + w, y + h], 10, fill=(255, 255, 255, 255), outline=(180, 180, 190, 255))
    host = re.sub(r"^https?://", "", dialog.get("url", "")).split("/")[0] or "This page"
    d.text((x + 20, y + 16), f"{host} says", font=fb, fill=(20, 20, 30))
    msg = (dialog.get("message") or "").replace("\n", " ")
    d.text((x + 20, y + 48), msg[:70] + ("…" if len(msg) > 70 else ""), font=f, fill=(40, 40, 50))
    marks = []
    if dialog["type"] == "prompt":
        box = [x + 20, y + 84, x + w - 20, y + 118]
        d.rectangle(box, outline=(120, 120, 140, 255), width=1)
        d.text((box[0] + 8, box[1] + 6), dialog.get("default", "")[:60], font=f, fill=(40, 40, 50))
        marks.append({"name": "Dialog text box", "type": "Edit", "box": box})
    by = y + h - 52
    ok = [x + w - 130, by, x + w - 20, by + 36]
    d.rounded_rectangle(ok, 6, fill=(0, 95, 184, 255))
    d.text((ok[0] + 40, ok[1] + 6), "OK", font=fb, fill="white")
    marks.append({"name": f"OK ({dialog['type']}: {msg[:50]})", "type": "DialogButton", "box": ok, "answer": True})
    if dialog["type"] != "alert":
        ca = [x + w - 250, by, x + w - 140, by + 36]
        d.rounded_rectangle(ca, 6, fill=(240, 240, 244, 255), outline=(150, 150, 160, 255))
        d.text((ca[0] + 22, ca[1] + 6), "Cancel", font=f, fill=(20, 20, 30))
        marks.append({"name": "Cancel", "type": "DialogButton", "box": ca, "answer": False})
    return img, marks


_TITLE_REST = re.compile(r"^( and \d+ more (pages?|tabs?))?( - .*)?$")


def match_target(window_title, tabs):
    """Tabs whose title is the active-tab part of a browser window title, e.g.
    'Sales reports – Shop and 2 more pages - Profile 1 - Microsoft\u200b Edge'. Longest title first."""
    wt = (window_title or "").replace("\u200b", "").strip()
    hits = []
    for t in tabs:
        tt = (t.get("title") or "").replace("\u200b", "").strip()
        if tt and wt.startswith(tt) and _TITLE_REST.match(wt[len(tt):]):
            hits.append(t)
    return sorted(hits, key=lambda t: -len(t.get("title") or ""))


# The page-side collector. Returns CSS-px rects in the top window's viewport.
JS = r"""
(() => {
  const T0 = performance.now(), MAXN = %(maxn)d, TBUDGET = %(tbudget)d;
  const VW = window.innerWidth, VH = window.innerHeight;
  const NATIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[onclick],[contenteditable=""],' +
    '[contenteditable=true],[contenteditable=plaintext-only],[role=button],[role=link],[role=menuitem],' +
    '[role=menuitemcheckbox],[role=menuitemradio],[role=tab],[role=checkbox],[role=radio],[role=switch],' +
    '[role=option],[role=combobox],[role=textbox],[role=searchbox],[role=slider],[role=treeitem],[role=spinbutton]';
  const TEXT_INPUT = /^(text|email|password|search|tel|url|number|date|datetime-local|month|week|time|)$/;
  const out = [], frames = [], kept = new Map();  // el -> index in out
  let scanned = 0, truncated = false;
  const curCache = new Map();
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const parentOf = el => el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
  function cursorOf(el) {
    if (!el || el.nodeType !== 1) return '';
    let c = curCache.get(el);
    if (c === undefined) { c = getComputedStyle(el).cursor; curCache.set(el, c); }
    return c;
  }
  function typeOf(el, tag) {
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'CheckBox';
      if (t === 'radio') return 'RadioButton';
      if (t === 'range') return 'Slider';
      if (/^(button|submit|reset|image|file|color)$/.test(t)) return 'Button';
      return 'Edit';
    }
    if (tag === 'textarea' || el.isContentEditable || role === 'textbox' || role === 'searchbox') return 'Edit';
    if (tag === 'select' || role === 'combobox' || role === 'listbox') return 'ComboBox';
    if (role === 'checkbox' || role === 'switch') return 'CheckBox';
    if (role === 'radio') return 'RadioButton';
    if (role.startsWith('menuitem')) return 'MenuItem';
    if (role === 'tab') return 'TabItem';
    if (role === 'option') return 'ListItem';
    if (role === 'treeitem') return 'TreeItem';
    if (role === 'slider') return 'Slider';
    if (role === 'spinbutton') return 'Spinner';
    if (role === 'link' || (tag === 'a' && el.hasAttribute('href'))) return 'Hyperlink';
    return 'Button';
  }
  const ICONS = [
    [/(kebab|more|dots|ellipsis|overflow|options|three dots|meatball|⋮|⋯|…|︙)/, '⋮ more options'],
    [/(close|dismiss|cancel|×|✕|✖|╳)/, '× close'], [/(hamburger|menu|nav toggle|☰|≡)/, '☰ menu'],
    [/(search|magnif|🔍)/, 'search'], [/(setting|gear|cog|⚙)/, 'settings'], [/(share)/, 'share'],
    [/(download)/, 'download'], [/(upload)/, 'upload'], [/(edit|pencil|✎|✏)/, 'edit'],
    [/(delete|trash|bin|remove)/, 'delete'], [/(copy|clipboard)/, 'copy'],
    [/(chevron left|arrow left|prev|back|‹|«|←)/, '‹ back/previous'], [/(chevron right|arrow right|next|forward|›|»|→)/, '› next'],
    [/(chevron down|arrow down|caret|dropdown|expand|▾|▼|⌄)/, '▾ expand'], [/(chevron up|arrow up|collapse|▴|▲)/, '▴ collapse'],
    [/(play|▶)/, '▶ play'], [/(pause)/, 'pause'], [/(heart|like)/, 'like'], [/(star|favou?rite|☆|★)/, 'favorite'],
    [/(bell|notif|🔔)/, 'notifications'], [/(avatar|profile|account|user)/, 'account'], [/(cart|basket|bag)/, 'cart'],
    [/(home)/, 'home'], [/(info|ℹ)/, 'info'], [/(help|question|\?)/, 'help'], [/(plus|add|new|＋|\+)/, '+ add'],
    [/(filter)/, 'filter'], [/(sort)/, 'sort'], [/(refresh|reload|sync|↻)/, 'refresh'], [/(calendar|date)/, 'calendar'],
    [/(mail|envelope|inbox)/, 'mail'], [/(attach|paperclip|📎)/, 'attach'], [/(emoji|smile)/, 'emoji'],
    [/(send|paper plane)/, 'send'], [/(mic|microphone)/, 'microphone'], [/(check|tick|✓|✔)/, '✓ check'],
    [/(external|open in new)/, 'open externally'], [/(print)/, 'print'], [/(lock)/, 'lock'], [/(eye|show password|visibility)/, 'show/hide'],
  ];
  function words(s) {
    return ' ' + String(s || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[-_.:#\/]+/g, ' ').toLowerCase() + ' ';
  }
  function iconName(el, tag, type) {
    const glyph = txt(el.textContent);
    let hint = [el.id, (typeof el.className === 'string') ? el.className : (el.className && el.className.baseVal),
                el.getAttribute('data-testid'), el.getAttribute('data-icon'), el.getAttribute('name'),
                el.getAttribute('aria-describedby') ? '' : ''].join(' ');
    let i = 0;
    for (const d of el.querySelectorAll('*')) {           // a few descendants: icon fonts, svg <use>/<title>
      if (++i > 12) break;
      const cn = (typeof d.className === 'string') ? d.className : (d.className && d.className.baseVal);
      hint += ' ' + (cn || '') + ' ' + (d.getAttribute('data-icon') || '') + ' ' + (d.getAttribute('href') || d.getAttribute('xlink:href') || '');
      if (d.tagName.toLowerCase() === 'title') hint += ' ' + d.textContent;
    }
    const svg = tag === 'svg' ? el : el.querySelector('svg');
    if (svg) {
      const c = svg.querySelectorAll('circle');
      if (c.length === 3) {
        const ys = [...c].map(x => +x.getAttribute('cy') || 0), xs = [...c].map(x => +x.getAttribute('cx') || 0);
        hint += (Math.max(...ys) - Math.min(...ys) > Math.max(...xs) - Math.min(...xs)) ? ' ⋮ ' : ' ⋯ ';
      }
    }
    const w = words(hint) + (glyph.length <= 3 ? ' ' + glyph + ' ' : '');
    let what = '';
    for (const [re, label] of ICONS) {
      const src = re.source.slice(1, -1);
      if (new RegExp('[^a-z](' + src + ')').test(w)) { what = label; break; }
    }
    const g = (glyph && glyph.length <= 3 && !/[a-z0-9]/i.test(glyph)) ? glyph : '';
    const kind = tag === 'canvas' ? 'canvas, drawn content' :
      (type === 'Hyperlink' ? 'icon link' : (type === 'Button' ? 'icon button' : type.toLowerCase()));
    const desc = [g && !what.includes(g) ? g : '', what].filter(Boolean).join(' ');
    return desc ? '(' + kind + ': ' + desc + ')' : '(' + kind + ')';
  }
  function nameOf(el, tag, type) {
    let n = txt(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (!n && lb) n = txt(lb.split(/\s+/).map(id => { const e = document.getElementById(id); return e ? e.innerText || e.textContent : ''; }).join(' '));
    if (!n && el.labels && el.labels.length) n = txt(el.labels[0].innerText || el.labels[0].textContent);
    const isField = tag === 'input' || tag === 'select' || tag === 'textarea';
    if (!n && !isField) n = txt(el.innerText !== undefined ? el.innerText : el.textContent);
    if (!n) n = txt(el.getAttribute('title'));
    if (!n) { const img = tag === 'img' ? el : el.querySelector('img[alt]'); if (img) n = txt(img.getAttribute('alt')); }
    if (!n) n = txt(el.getAttribute('placeholder'));
    if (!n && tag === 'input' && /^(button|submit|reset)$/.test(el.type)) n = txt(el.value) || el.type;
    if (n.length > 80) n = n.slice(0, 79) + '…';
    if (!n || (n.length <= 2 && !/[a-z0-9]/i.test(n))) n = iconName(el, tag, type);
    if (tag === 'input' && el.type === 'password' && !/password/i.test(n)) n += ' (password)';
    if (tag === 'select' && el.selectedIndex >= 0 && el.options[el.selectedIndex]) n += ': ' + txt(el.options[el.selectedIndex].text);
    if ((type === 'CheckBox' || type === 'RadioButton')) {
      const on = el.checked !== undefined && tag === 'input' ? el.checked : el.getAttribute('aria-checked') === 'true';
      n += on ? ' (checked)' : ' (not checked)';
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') n += ' (disabled)';
    return n;
  }
  function hits(el, root, x, y) {
    const h = root.elementFromPoint(x, y);
    return !!h && (h === el || el.contains(h));
  }
  function consider(el, root, fx, fy, clip) {
    scanned++;
    if (el.hasAttribute('data-buddy-ghost')) return;               // the agent's own in-page ghost cursor
    const tag = el.tagName.toLowerCase();
    if (tag === 'html' || tag === 'body' || tag === 'option' || tag === 'optgroup') return;
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return;
    const x1 = Math.max(r.left + fx, clip[0]), y1 = Math.max(r.top + fy, clip[1]);
    const x2 = Math.min(r.right + fx, clip[2]), y2 = Math.min(r.bottom + fy, clip[3]);
    if (x2 - x1 < 3 || y2 - y1 < 3) return;                       // off screen
    let native = el.matches(NATIVE);
    if (!native && el.hasAttribute('tabindex') && el.tabIndex >= 0) native = true;
    let pointer = false;
    if (!native) {
      if (cursorOf(el) !== 'pointer') return;
      const p = parentOf(el);
      if (p && cursorOf(p) === 'pointer') {                       // only the outermost pointer element...
        const pr = p.getBoundingClientRect();
        if (pr.width * pr.height < 0.5 * VW * VH) return;          // ...unless the parent is a page-sized blob
      }
      if ((x2 - x1) * (y2 - y1) > 0.5 * VW * VH) return;
      pointer = true;
    }
    if (el.checkVisibility && !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return;
    // on top? centre first, then a few other spots (box shrinks to the spot so the centre stays clickable)
    const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
    const rn = el.getRootNode();
    let bx = [x1, y1, x2, y2];
    if (!hits(el, rn, cx - fx, cy - fy)) {
      let ok = false;
      for (const [ax, ay] of [[.25, .5], [.75, .5], [.5, .25], [.5, .75], [.2, .2], [.8, .2], [.2, .8], [.8, .8]]) {
        const px = x1 + (x2 - x1) * ax, py = y1 + (y2 - y1) * ay;
        if (hits(el, rn, px - fx, py - fy)) {
          const hw = Math.max(3, Math.min(x2 - x1, y2 - y1) / 4);
          bx = [px - hw, py - hw, px + hw, py + hw]; ok = true; break;
        }
      }
      if (!ok) return;
    }
    const type = typeOf(el, tag);
    // same box as an interactive ancestor we already kept (e.g. <a><button>)? keep one
    let p = parentOf(el);
    for (let k = 0; p && k < 4; k++, p = parentOf(p)) {
      if (kept.has(p)) {
        const o = out[kept.get(p)];
        if (Math.abs(o[0] - bx[0]) <= 2 && Math.abs(o[1] - bx[1]) <= 2 && Math.abs(o[2] - bx[2]) <= 2 && Math.abs(o[3] - bx[3]) <= 2) {
          if (type === 'Edit' || type === 'CheckBox' || type === 'ComboBox' || type === 'RadioButton') {
            out[kept.get(p)] = [bx[0], bx[1], bx[2], bx[3], type, nameOf(el, tag, type)]; kept.set(el, kept.get(p));
          }
          return;
        }
        break;
      }
    }
    kept.set(el, out.length);
    out.push([bx[0], bx[1], bx[2], bx[3], type, nameOf(el, tag, type)]);
  }
  function scan(root, fx, fy, clip) {
    for (const el of root.querySelectorAll('*')) {
      if (out.length >= MAXN || performance.now() - T0 > TBUDGET) { truncated = true; return; }
      consider(el, root, fx, fy, clip);
      if (el.shadowRoot && !el.hasAttribute('data-buddy-ghost')) scan(el.shadowRoot, fx, fy, clip);
      const tag = el.tagName;
      if (tag === 'IFRAME' || tag === 'FRAME') {
        const r = el.getBoundingClientRect();
        const f = [Math.max(r.left + fx, clip[0]), Math.max(r.top + fy, clip[1]), Math.min(r.right + fx, clip[2]), Math.min(r.bottom + fy, clip[3])];
        if (f[2] - f[0] < 8 || f[3] - f[1] < 8) continue;
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc && doc.documentElement) {
          const ox = r.left + fx + el.clientLeft, oy = r.top + fy + el.clientTop;
          scan(doc, ox, oy, f);
        } else {
          frames.push(f);                                           // cross-origin: UIA must cover it
        }
      }
    }
  }
  scan(document, 0, 0, [0, 0, VW, VH]);
  return {vw: VW, vh: VH, dpr: window.devicePixelRatio, title: document.title, url: location.href,
          vis: document.visibilityState, marks: out, frames: frames, scanned: scanned, truncated: truncated,
          ms: Math.round(performance.now() - T0)};
})()
"""


def evaluate(ws_url, expression, timeout=BUDGET_S):
    res = _cdp(ws_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "timeout": int(timeout * 1000)}, timeout=timeout)
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"].get("text", "js error"))
    return res.get("result", {}).get("value")


def fetch_raw(fg_hwnd, fg_title, budget_s=BUDGET_S, max_marks=MAX_MARKS):
    """JS scan of the active page when fg_hwnd is the buddy browser -> dict in CSS px, or None."""
    t0 = time.time()
    try:
        if not is_buddy_window(fg_hwnd):
            return None
        cands = match_target(fg_title, targets(timeout=budget_s / 2))
        if not cands:
            return None
        js = JS % {"maxn": max_marks, "tbudget": int(budget_s * 1000 * 0.6)}
        for t in cands[:3]:                          # same title in several tabs: the visible one wins
            left = budget_s - (time.time() - t0)
            if left <= 0.05:
                return None
            raw = evaluate(t["webSocketDebuggerUrl"], js, timeout=left)
            if raw and raw.get("vis") == "visible":
                raw["target"] = t["id"]
                return raw
    except Exception:  # noqa: BLE001
        return None
    return None


def css_to_screen(rect, doc_box, vw):
    """CSS-px viewport rect -> screen px, given the web content's screen box and window.innerWidth."""
    scale = (doc_box[2] - doc_box[0]) / float(vw)
    return [round(doc_box[0] + rect[0] * scale), round(doc_box[1] + rect[1] * scale),
            round(doc_box[0] + rect[2] * scale), round(doc_box[1] + rect[3] * scale)]


def to_marks(raw, doc_box_screen, origin=(0, 0)):
    """raw (fetch_raw) -> (marks, frames): boxes in px relative to origin (the monitor's top-left), clipped to
    the content box. frames = boxes of cross-origin iframes whose content only UIA can see."""
    if not raw or not raw.get("vw") or not doc_box_screen:
        return None, []
    ox, oy = origin
    l, t, r, b = doc_box_screen

    def conv(rect):
        x1, y1, x2, y2 = css_to_screen(rect, doc_box_screen, raw["vw"])
        return [max(x1, l) - ox, max(y1, t) - oy, min(x2, r) - ox, min(y2, b) - oy]

    marks = []
    for x1, y1, x2, y2, typ, name in raw.get("marks", []):
        box = conv((x1, y1, x2, y2))
        if box[2] - box[0] >= 2 and box[3] - box[1] >= 2 and name:
            marks.append({"name": str(name)[:80], "type": typ, "box": box})
    return marks, [conv(f) for f in raw.get("frames", [])]


def page_marks(fg_hwnd, fg_title, doc_box_screen, origin=(0, 0), budget_s=BUDGET_S):
    """DOM marks of the buddy-browser page in front (boxes relative to origin), or None -> use UIA."""
    marks, _ = to_marks(fetch_raw(fg_hwnd, fg_title, budget_s), doc_box_screen, origin)
    return marks


# ---------------------------------------------------------------- CLI
def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "launch":
        print("launched" if launch(sys.argv[2] if len(sys.argv) > 2 else None) else "FAILED")
        if has_bg_flags() is False:
            print("note: this buddy browser was started without the background flags - close and launch it again "
                  "so it keeps rendering while covered (python -m rbot.browser.browser_dom close)")
    elif cmd == "observe":
        from rbot.desktop import grounding as gt
        o = observe_tab(sys.argv[2] if len(sys.argv) > 2 else None)
        gt.RUNS.mkdir(exist_ok=True)
        o["marked"].save(gt.RUNS / "observe_tab.png")
        print(f"observe_tab: {o['timing']}  {o['size']}  scale={o['scale']}  vis={o['vis']}  {o['window']!r}")
        for m in o["marks"]:
            print(f"[{m['id']}] {m['type']}: {m['name'][:70]}  {m['box']}")
    elif cmd == "close":
        print("closed" if close() else "not running")
    elif cmd == "marks":
        from rbot.desktop import perception                          # front buddy window: use perception's own doc-box lookup
        wins = _buddy_windows()
        if not wins:
            print("buddy browser not running")
            return
        fg = int(_u32.GetForegroundWindow() or 0)
        if fg not in wins:
            print("note: buddy browser is not in front; bringing it to front")
            focus(wins[0])
            time.sleep(0.3)
        t = time.time()
        obs = perception.observe()
        print(f"observe(): {time.time() - t:.2f}s  source={obs.get('page_source')}  window={obs['window']!r}")
        for m in obs["marks"]:
            print(f"[{m['id']}] {m['type']}: {m['name'][:70]}  {m['box']}{'  ' + m['area'] if m.get('area') else ''}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
