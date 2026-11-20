"""What the agent sees: a screenshot + numbered marks on clickable UI elements (research prototype).

  python -m rbot.desktop.perception            run observe() once, print timing, save runs/observe_marked.png

Looks at the monitor that holds the foreground window. Mark boxes are PHYSICAL px relative to that monitor's
screenshot; add obs["origin"] to get absolute screen px for input.
"""
import ctypes
import os
import re
import time

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")   # see cursor_overlay.py

from rbot.desktop import grounding as gt          # sets per-monitor DPI awareness on import; gives capture/root_hwnd_at/draw_marks

MARKABLE = gt.CLICKABLE | {"DocumentControl"}
BIG_OK = {"EditControl", "DocumentControl"}          # text areas may be huge; other controls > half the screen are skipped


def foreground_title():
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, buf, 512)
    return buf.value


def capture_active_monitor():
    """Screenshot of the monitor containing the foreground window's centre (primary as fallback)."""
    import mss
    from ctypes import wintypes
    from PIL import Image
    u = ctypes.windll.user32
    r = wintypes.RECT()
    u.GetWindowRect(u.GetForegroundWindow(), ctypes.byref(r))
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    with mss.MSS() as s:
        mons = s.monitors[1:]
        mon = next((m for m in mons if m["left"] <= cx < m["left"] + m["width"]
                    and m["top"] <= cy < m["top"] + m["height"]), mons[0])
        for f in BEFORE_CAPTURE:                         # e.g. hide the buddy cursor from the agent's eyes
            f()
        try:
            shot = s.grab(mon)
        finally:
            for f in AFTER_CAPTURE:
                f()
    return Image.frombytes("RGB", shot.size, shot.rgb), mon


BEFORE_CAPTURE, AFTER_CAPTURE = [], []                # hooks run around every agent screenshot
USE_DOM = True     # buddy browser in front -> page elements come from the DOM (browser_dom.py), else UIA
_POOL = None


def _pool():
    global _POOL
    if _POOL is None:
        import concurrent.futures
        _POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dom")
    return _POOL
HIT_TEST = False   # per-element hit-test is too slow/unreliable in WebView apps (Teams); safety is checked at click time


def _sig(c):
    r = c.BoundingRectangle
    return c.ControlTypeName, c.Name or "", r.left, r.top, r.right, r.bottom


def _on_top(ctrl, cx, cy, up=6):
    """True if the element actually under (cx, cy) IS ctrl or one of its descendants (walk up from the
    hit, compare type+name+rect). False means something else in the same window covers ctrl."""
    import uiautomation as auto
    try:
        want = _sig(ctrl)
        hit = auto.ControlFromPoint(int(cx), int(cy))
        for _ in range(up):
            if hit is None:
                return False
            if _sig(hit) == want:
                return True
            hit = hit.GetParentControl()
    except Exception:
        return True                                      # can't tell -> keep it
    return False


# ---------------------------------------------------------------- browser clutter (Edge / Chrome)
# Browser chrome is identified by the UIA ClassName of the views (checked on this machine with Edge 157; the
# Chrome names are their upstream equivalents). DROP_CLS subtrees are skipped entirely: bookmarks bar, extension /
# hub / Copilot / profile / share / screenshot buttons, page-action icons, sidebar. Everything else in the window
# chrome is kept (Back/Forward/Refresh, address bar, Downloads, Settings-and-more, infobars, caption buttons...),
# so browser prompts/bubbles that live inside the window stay too. Inactive tabs go last, at most MAX_INACTIVE_TABS.
BROWSER_TITLE = re.compile(r"(Microsoft\s*Edge|Google Chrome|Chromium)\s*$")
DROP_CLS = {
    # Edge
    "BookmarkBarView", "EdgeExtensionsToolbarContainer", "PinnedToolbarActionsContainer", "PageActionContainerView",
    "EdgePageActionIconContainerView", "EdgeHubAppsTowerView", "HubToggleToolbarButton", "EdgeCopilotToolbarLabelButton",
    "EdgeAvatarToolbarButton", "ContextMenuButton", "ToolbarSplitWindowButton", "MediaToolbarButtonView",
    "SpaceworkButton", "EdgeTabActionsMenuButton", "ToggleToolbarButton", "StarView", "HomeButton",
    "OmniboxViewOverlayViews", "EdgeCoworkOmniboxIdentityView", "EdgeCopilotToolbarSplitButtonContainer",
    "EdgeCopilotDropdownMenuButton",
    # Chrome
    "ExtensionsToolbarContainer", "ExtensionsToolbarButton", "AvatarToolbarButton", "SidePanel", "TabSearchButton",
    "TabSearchContainer", "PageActionIconContainerView", "ToolbarAccountIconContainerView", "PinnedActionToolbarButton",
}
DROP_RE = re.compile(r"SidePane|SidePanel|Bookmark|Extension|Copilot")
TAB_CLS = {"EdgeTab", "Tab"}
TAB_CLOSE_CLS = {"EdgeTabCloseButton", "TabCloseButton"}
MAX_INACTIVE_TABS = 8


def is_browser(cls, title):
    return cls == "Chrome_WidgetWin_1" and bool(BROWSER_TITLE.search((title or "").replace("​", "")))


def _tab_name(name):
    """'Foo - Sleeping - Memory usage - 31.9 MB' -> 'Foo'."""
    return re.sub(r"( - (Sleeping|Memory usage - [\d.,]+\s*[KMG]B|Audio playing|Muted|Network error|Crashed))+\s*$",
                  "", name)


def _hwnd_title(h):
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(h, buf, 512)
    return buf.value


def _walk(win, mon, out, limit, deadline, max_depth=30, browser=False, docs=None, skip_docs=False,
          in_page=False, only=None):   # Chromium/Electron apps nest deep
    """Depth-first walk of one top-level window, appending visible clickable elements to out.
    browser=True: drop browser clutter (DROP_CLS), tag inactive tabs (m["low"]) and keep at most
    MAX_INACTIVE_TABS of them. docs: list that collects (ctrl, rect) of web page Documents (RootWebArea);
    skip_docs=True doesn't descend into them (DOM marks / a later page walk fill them in). in_page: start inside
    a page (no browser checks). only: list of boxes (monitor px); keep only marks whose centre is in one of them.
    Browser bubbles ("Save your password?", Allow/Block prompts...) are nested Chrome_WidgetWin_1 panes with their
    own (owned) HWND: their elements are checked against that HWND, tagged m["pri"], and the bubble's title
    gets a mark of type "Dialog" (the guard and hitl read it)."""
    top = win.NativeWindowHandle
    sw, sh = mon["width"], mon["height"]
    ox, oy = mon["left"], mon["top"]
    n_low = [0]

    def visit(ctrl, depth, zone, want, pri):
        if len(out) >= limit or time.time() > deadline:
            return
        try:
            kind = ctrl.ControlTypeName
            if ctrl.IsOffscreen:
                return                                   # skip the whole hidden subtree
            low = zone == "tab_inactive"
            if browser and zone not in ("page",):
                cls = ctrl.ClassName or ""
                if cls in DROP_CLS or DROP_RE.search(cls):
                    return
                if cls in TAB_CLS and kind == "TabItemControl":
                    try:
                        sel = ctrl.GetSelectionItemPattern().IsSelected
                    except Exception:
                        sel = False
                    zone = "tab_active" if sel else "tab_inactive"
                    low = not sel
                    if low and n_low[0] >= MAX_INACTIVE_TABS:
                        return
                elif cls in TAB_CLOSE_CLS and zone == "tab_inactive":
                    return
                if depth > 0 and cls == "Chrome_WidgetWin_1":           # a bubble / popup in its own window
                    want, pri = ctrl.NativeWindowHandle or want, True
                    r = ctrl.BoundingRectangle
                    title = " ".join((ctrl.Name or "").split())
                    cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
                    if title and 0 <= cx - ox < sw and 0 <= cy - oy < sh:
                        out.append({"name": title[:80], "type": "Dialog", "pri": True,
                                    "box": [max(r.left - ox, 0), max(r.top - oy, 0),
                                            min(r.right - ox, sw), min(r.bottom - oy, sh)]})
                if kind == "DocumentControl":
                    if cls == "WebView":                 # empty wrapper around the real page Document
                        kind = "_wrapper"
                    elif ctrl.AutomationId == "RootWebArea" or not cls:
                        zone = "page"
                        r = ctrl.BoundingRectangle
                        if docs is not None:
                            docs.append((ctrl, (r.left, r.top, r.right, r.bottom)))
            if kind in MARKABLE and not (in_page and depth == 0):     # (the page Document is already marked)
                r = ctrl.BoundingRectangle
                w, h = r.right - r.left, r.bottom - r.top
                cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
                if (w >= 8 and h >= 8 and (kind in BIG_OK or (w <= sw * 0.5 and h <= sh * 0.5))
                        and 0 <= cx - ox < sw and 0 <= cy - oy < sh and gt.root_hwnd_at(cx, cy) == want
                        and (only is None or any(b[0] <= cx - ox < b[2] and b[1] <= cy - oy < b[3] for b in only))):
                    name = (ctrl.Name or "").strip().replace("\n", " ")
                    if not name and kind in BIG_OK:
                        name = "(edit)" if kind == "EditControl" else "(document)"
                    if name and HIT_TEST and kind not in BIG_OK and not _on_top(ctrl, cx, cy):
                        name = ""                        # hidden behind something inside the same window
                    if name and kind == "TabItemControl" and browser:
                        name = _tab_name(name) + ("" if low else " (current tab)")
                    if name:
                        m = {"name": name[:80], "type": kind.replace("Control", ""),
                             "box": [max(r.left - ox, 0), max(r.top - oy, 0), min(r.right - ox, sw), min(r.bottom - oy, sh)]}
                        if low:
                            m["low"] = True
                            n_low[0] += kind == "TabItemControl"
                        if pri:
                            m["pri"] = True
                        out.append(m)
            if depth >= max_depth or (skip_docs and zone == "page" and kind == "DocumentControl"):
                return
            child = ctrl.GetFirstChildControl()
        except Exception:
            return
        while child is not None and len(out) < limit and time.time() < deadline:
            visit(child, depth + 1, zone, want, pri)
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                return

    visit(win, 0, "page" if in_page else None, top, False)


def _windows_below(h):
    """Top-level hwnds below h in z-order."""
    u = ctypes.windll.user32
    u.GetWindow.restype = ctypes.c_void_p
    out, cur = set(), h
    for _ in range(2000):
        cur = u.GetWindow(ctypes.c_void_p(cur), 2)          # GW_HWNDNEXT
        if not cur:
            break
        out.add(int(cur))
    return out


def _hwnd_rect(h):
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _dom_merge(fg_marks, dom, docs, frames):
    """Replace the UIA marks inside the page Document box(es) by the DOM marks (marks are monitor-relative).
    The page Document mark itself stays (hitl reads its name), so do browser bubbles floating over the page
    (m["pri"]) and UIA marks inside cross-origin iframes (frames: boxes the page script couldn't look into)."""
    def inside(m, b):
        cx, cy = (m["box"][0] + m["box"][2]) / 2, (m["box"][1] + m["box"][3]) / 2
        return b[0] <= cx < b[2] and b[1] <= cy < b[3]
    keep = [m for m in fg_marks
            if m["type"] == "Document" or m.get("pri") or not any(inside(m, d) for d in docs)
            or any(inside(m, f) for f in frames)]
    return keep + dom


def observe(max_marks=100, budget_s=3.0):
    import uiautomation as auto
    t0 = time.time()
    deadline = t0 + budget_s
    img, mon = capture_active_monitor()
    origin = (mon["left"], mon["top"])
    fg_hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    fg_title = foreground_title()
    page_source = "uia"
    with auto.UIAutomationInitializerInThread():          # COM must be initialised in whatever thread calls us
        fg = auto.GetForegroundControl()
        tray = auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd")
        tray = tray if tray.Exists(0, 0) else None

        fg_marks, tray_marks, other_marks = [], [], []
        browser = bool(fg) and is_browser(fg.ClassName, fg_title)
        dom_job = None
        if browser and USE_DOM:
            try:
                from rbot.browser import browser_dom
                if browser_dom.is_buddy_window(fg_hwnd):  # page script runs while we walk the browser chrome
                    dom_job = _pool().submit(browser_dom.fetch_raw, fg_hwnd, fg_title)
            except Exception:
                dom_job = None
        docs = []
        tm = {}
        if fg:
            # browser: chrome + bubbles first, the page Document afterwards (DOM marks, or a UIA walk of it)
            _walk(fg, mon, fg_marks, max_marks, deadline, browser=browser, docs=docs, skip_docs=browser)
        tm["fg"] = time.time() - t0
        if browser and dom_job is None:
            for c, _ in docs:
                _walk(c, mon, fg_marks, max_marks, deadline, in_page=True)
        if dom_job is not None:
            raw = None
            try:
                raw = dom_job.result(timeout=max(0.05, browser_dom.BUDGET_S + 0.1 - (time.time() - t0)))
            except Exception:
                raw = None
            doc = max(docs, key=lambda d: (d[1][2] - d[1][0]) * (d[1][3] - d[1][1]), default=None)
            dom, frames = browser_dom.to_marks(raw, doc[1] if doc else None, origin)
            if dom is not None:
                ox, oy = origin                          # covered by another window (e.g. a browser bubble)?
                dom = [m for m in dom if gt.root_hwnd_at((m["box"][0] + m["box"][2]) / 2 + ox,
                                                         (m["box"][1] + m["box"][3]) / 2 + oy) == fg_hwnd]
                dboxes = [[d[1][0] - ox, d[1][1] - oy, d[1][2] - ox, d[1][3] - oy] for d in docs]
                if frames:                               # cross-origin iframes: only UIA can see inside
                    for c, _ in docs:
                        _walk(c, mon, fg_marks, max_marks, deadline, in_page=True, only=frames)
                fg_marks = _dom_merge(fg_marks, dom, dboxes, frames)
                page_source = "dom"
            else:                                        # page script failed: walk the page with UIA after all
                for c, _ in docs:
                    _walk(c, mon, fg_marks, max_marks, deadline, in_page=True)
        tm["dom"] = time.time() - t0
        if tray:                                          # taskbar always gets a little time, even if fg was slow
            _walk(tray, mon, tray_marks, max_marks, max(deadline, time.time() + 0.6))
            # the notification area (Windows Update, wifi, volume, clock...) is never part of a task:
            # tag it so the guard can refuse clicks there. It starts at the "Show hidden icons" chevron.
            chev = next((m["box"][0] for m in tray_marks if "hidden icons" in m["name"].lower()), None)
            edge = chev if chev is not None else mon["width"] * 0.8
            for m in tray_marks:
                if m["box"][0] >= edge - 2:
                    m["area"] = "systray"
        # text boxes always get a number; inactive browser tabs only if there's room left
        fg_marks.sort(key=lambda m: (bool(m.get("low")), not m.get("pri"), m["type"] not in ("Edit", "Document")))
        tm["tray"] = time.time() - t0
        keep_fg = max(max_marks - len(tray_marks), max_marks * 2 // 3)
        marks = (fg_marks[:keep_fg] + tray_marks)[:max_marks]

        skip = {fg.NativeWindowHandle if fg else 0, tray.NativeWindowHandle if tray else 0}
        if len(marks) < max_marks and time.time() < deadline:
            below = _windows_below(fg_hwnd) if fg_hwnd else set()
            fr = _hwnd_rect(fg_hwnd) if fg_hwnd else None
            for win in auto.GetRootControl().GetChildren():           # top-level windows, front to back
                if len(marks) + len(other_marks) >= max_marks or time.time() > deadline:
                    break
                try:
                    h = win.NativeWindowHandle
                    if h in skip or win.IsOffscreen:
                        continue
                    if fr and h in below:                # fully hidden behind the foreground window? skip it
                        wr = _hwnd_rect(h)
                        vis = (max(wr[0], mon["left"]), max(wr[1], mon["top"]),
                               min(wr[2], mon["left"] + mon["width"]), min(wr[3], mon["top"] + mon["height"]))
                        if vis[2] <= vis[0] or vis[3] <= vis[1] or (
                                fr[0] <= vis[0] and fr[1] <= vis[1] and fr[2] >= vis[2] and fr[3] >= vis[3]):
                            continue
                    wb = is_browser(win.ClassName, _hwnd_title(h))
                except Exception:
                    continue
                _walk(win, mon, other_marks, max_marks - len(marks), min(deadline, time.time() + 0.8), browser=wb)
            other_marks.sort(key=lambda m: (bool(m.get("low")), not m.get("pri")))
        marks += other_marks[:max_marks - len(marks)]

    for i, m in enumerate(marks):
        m["id"] = i
    marks = [{"id": m["id"], "name": m["name"], "type": m["type"], "box": m["box"],
              **({"area": m["area"]} if m.get("area") else {})} for m in marks]
    return {"image": img, "marked": gt.draw_marks(img, marks), "marks": marks,
            "size": img.size, "origin": (mon["left"], mon["top"]), "window": foreground_title(),
            "page_source": page_source, "timing": {k: round(v, 3) for k, v in tm.items()}}


def diff_ratio(img_a, img_b, width=320, threshold=24):
    """Fraction of pixels that visibly changed between two screenshots (0.0 .. 1.0)."""
    from PIL import ImageChops
    h = max(1, round(img_a.height * width / img_a.width))
    a = img_a.convert("L").resize((width, h))
    b = img_b.convert("L").resize((width, h))
    hist = ImageChops.difference(a, b).histogram()
    return sum(hist[threshold:]) / (width * h)


if __name__ == "__main__":
    t = time.time()
    obs = observe()
    dt = time.time() - t
    print(f"observe(): {dt:.2f}s, {len(obs['marks'])} marks, screen {obs['size']}")
    gt.RUNS.mkdir(exist_ok=True)
    obs["marked"].save(gt.RUNS / "observe_marked.png")
    print("diff_ratio(same, same) =", diff_ratio(obs["image"], obs["image"]))
