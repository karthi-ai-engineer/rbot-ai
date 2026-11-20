"""Invisible clicks (phase 5): do an action through UI Automation patterns WITHOUT moving the real mouse.

  try_invisible(name, args, mark, obs, origin, cursor=None, timeout=1.5) -> outcome str | None
  capture_window(hwnd) -> PIL.Image | None       picture of a (covered, not minimized) window via PrintWindow
  patterns_at(x, y) -> [{"type","name","patterns",...}]   read-only probe of the element under a point (+parents)

None = "can't do it invisibly, use the normal (real mouse / keyboard) path". A string = it was done (or at
least sent); never return None after a pattern call went out, so the caller can't do the action twice.

How it works: the live element is re-found at action time (ControlFromPoint at the mark's box centre, absolute
= box centre + origin, then up to 15 parents until one IS the mark: same name+type, or same rect within 4 px,
like agent.mark_matcher). All UIA calls run in a worker thread with COM initialised and a hard timeout
(WebView2 apps like Teams can hang). If a buddy cursor is given it glides to the element and plays the click
(ghost click) while the real pointer stays where it is.

Pattern ladder:
  click         Invoke -> Toggle -> SelectionItem -> ExpandCollapse -> LegacyIAccessible.DoDefaultAction
                Items differ: list/tree/data items only SelectionItem (or Toggle) - Invoke / default action on
                them means "open" (a double click); tab items and radio buttons try SelectionItem first; items of
                a classic Win32 LISTBOX -> None (its Select doesn't notify the app). Check boxes try Toggle first
                so the new state can be verified; combo boxes try ExpandCollapse first.
  double_click  list/tree/data items with Invoke ("open") only
  right_click   never (context menus need the real mouse)
  type_text     ValuePattern.SetValue on a plain Edit only: not password, not read-only, field empty or
                args["replace"], single-line text, not a web/WebView2/Electron field (FrameworkId "Chrome":
                rich editors like the Teams compose box must get real keystrokes)
  press_key     "enter" on a Button/Hyperlink mark -> Invoke; anything else -> None
  scroll        ScrollPattern.Scroll on the element or its first scrollable parent

Focus: a pattern call on a background window often makes that app the foreground window (seen live with Qt
and WinForms). RESTORE_FOREGROUND gives the focus back to the previous window right after (not after an Expand,
whose drop-down would close again); the outcome text then says so.

Limits: the element is re-found by POINT, so the target must be visible on screen (a covered window -> None);
classic Win32 combo boxes and Qt lists expose no Expand/Scroll pattern -> None (real mouse).

Thread-safe: every cursor call goes through cursor.post(); UIA objects never leave the worker thread.
"""
import ctypes
import re
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes

# UIA pattern ids (UIAutomationClient.h) - kept here so this module imports without uiautomation
INVOKE, VALUE, SCROLL, EXPAND = 10000, 10002, 10004, 10005
SELECTION_ITEM, TOGGLE, SCROLL_ITEM, LEGACY = 10010, 10015, 10017, 10018
PATTERN_NAMES = {10000: "Invoke", 10001: "Selection", 10002: "Value", 10003: "RangeValue", 10004: "Scroll",
                 10005: "ExpandCollapse", 10010: "SelectionItem", 10014: "Text", 10015: "Toggle",
                 10017: "ScrollItem", 10018: "LegacyIAccessible"}
COLLAPSED, EXPANDED, PARTIAL, LEAF = 0, 1, 2, 3            # ExpandCollapseState
SMALL_DEC, NO_AMOUNT, SMALL_INC = 1, 2, 4                  # ScrollAmount
SCROLL_STEPS_PER_CLICK = 3                                 # one wheel notch ~ 3 lines ~ 3 SmallIncrements
MAX_SCROLL_CALLS = 30

UP = 15                                                    # parents walked from the hit
RECT_TOL = 4
VERIFY_S = 0.35                                            # how long to poll for a visible state change
GO_WAIT_S = 6.0                                            # worker waits this long for the ghost glide
RISKY = re.compile(r"\b(delete|remove|pay|payment|buy|purchase|send money|uninstall|format (?:the )?(?:disk|drive|partition|c:)|sign ?out|log ?out)\b",
                   re.I)
ITEMS = {"ListItem", "TreeItem", "TabItem", "DataItem", "RadioButton"}
OPENABLE = {"ListItem", "TreeItem", "DataItem"}
WHOLE = {"Document", "Pane", "Window", "Group", "Text", "Image", "Custom"}   # never "clicked" by pattern
SUPPORTED = {"click", "double_click", "right_click", "type_text", "press_key", "scroll"}


# ---------------------------------------------------------------- UIA access (tests replace these two)
@contextmanager
def _com():
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        yield


def _control_from_point(x, y):
    import uiautomation as auto
    return auto.ControlFromPoint(int(x), int(y))


# ---------------------------------------------------------------- small helpers
def _kind(c):
    return (c.ControlTypeName or "").replace("Control", "")


def _name(c):
    return (c.Name or "").strip().replace("\n", " ")


def _rect(c):
    r = c.BoundingRectangle
    return [r.left, r.top, r.right, r.bottom]


def _pat(c, pid):
    try:
        return c.GetPattern(pid)
    except Exception:  # noqa: BLE001
        return None


def _prop(obj, attr, default=None):
    try:
        return getattr(obj, attr)
    except Exception:  # noqa: BLE001
        return default


def _clamp_rect(rect, origin, size):
    (ox, oy), (w, h) = origin, size
    l, t, r, b = rect
    return [min(max(l - ox, 0), w), min(max(t - oy, 0), h), min(max(r - ox, 0), w), min(max(b - oy, 0), h)]


def _match(c, mark, origin, size):
    """'name' if the live element is the mark by name+type, 'rect' if by box, else None (agent.mark_matcher)."""
    mname, mtype = str(mark.get("name", "")).strip(), str(mark.get("type", ""))
    kind, name = _kind(c), _name(c)[:80]
    if kind == mtype and (name == mname or (not name and mname in ("(edit)", "(document)"))):
        return "name"
    try:
        if all(abs(p - q) <= RECT_TOL for p, q in zip(_clamp_rect(_rect(c), origin, size), mark["box"])):
            return "rect"
    except Exception:  # noqa: BLE001
        pass
    return None


def _points(box, origin):
    """Box centre first, then four inner points (the centre may sit on a child or on the buddy overlay)."""
    x1, y1, x2, y2 = box
    ox, oy = origin
    out = [(0.5, 0.5), (0.3, 0.5), (0.7, 0.5), (0.5, 0.3), (0.5, 0.7)]
    return [(round(x1 + (x2 - x1) * fx + ox), round(y1 + (y2 - y1) * fy + oy)) for fx, fy in out]


def _find(mark, origin, size, cancel):
    """Live element that IS the mark, or None. Prefers a name+type match over a same-rect match."""
    for x, y in _points(mark["box"], origin):
        if cancel.is_set():
            return None
        try:
            c = _control_from_point(x, y)
        except Exception:  # noqa: BLE001
            c = None
        by_rect = None
        for _ in range(UP):
            if c is None or cancel.is_set():
                break
            how = _match(c, mark, origin, size)
            if how == "name":
                return c
            if how == "rect" and by_rect is None:
                by_rect = c
            try:
                c = c.GetParentControl()
            except Exception:  # noqa: BLE001
                break
        if by_rect is not None:
            return by_rect
    return None


def _poll(fn, want=True, secs=VERIFY_S):
    """Poll fn() until it returns something truthy-equal to want (or time runs out); returns the last value."""
    end = time.time() + secs
    while True:
        try:
            v = fn()
        except Exception:  # noqa: BLE001
            v = None
        if v == want or time.time() >= end:
            return v
        time.sleep(0.03)


def _label(c):
    n = _name(c)
    return f"'{n[:50]}' ({_kind(c)})" if n else f"an unnamed {_kind(c)}"


# ---------------------------------------------------------------- plans: (ghost kind, fn() -> outcome str)
def _invoke_plan(c, verb="pressed"):
    p = _pat(c, INVOKE)
    if p is None:
        return None

    def run():
        p.Invoke(waitTime=0)
        return f"ok - {verb} {_label(c)} without the mouse (UIA Invoke)"
    return run


def _toggle_plan(c):
    p = _pat(c, TOGGLE)
    if p is None:
        return None

    def run():
        before = _prop(p, "ToggleState")
        p.Toggle(waitTime=0)
        after = _poll(lambda: p.ToggleState != before)
        state = {0: "off", 1: "on", 2: "mixed"}.get(_prop(p, "ToggleState"), "?")
        if after:
            return f"ok - toggled {_label(c)} without the mouse, now {state} (UIA Toggle)"
        return f"ok? - sent Toggle to {_label(c)} but it still reads {state} - check the screen"
    return run


def _select_plan(c):
    p = _pat(c, SELECTION_ITEM)
    if p is None:
        return None

    def run():
        if _prop(p, "IsSelected"):
            return f"ok - {_label(c)} was already selected (no mouse used)"
        p.Select(waitTime=0)
        if _poll(lambda: bool(p.IsSelected)):
            return f"ok - selected {_label(c)} without the mouse (UIA SelectionItem)"
        return f"ok? - sent Select to {_label(c)} but it does not read as selected - check the screen"
    return run


def _expand_plan(c):
    p = _pat(c, EXPAND)
    state = _prop(p, "ExpandCollapseState") if p is not None else None
    if p is None or state not in (COLLAPSED, EXPANDED, PARTIAL):
        return None

    def run():
        if state == EXPANDED:
            p.Collapse(waitTime=0)
            ok = _poll(lambda: p.ExpandCollapseState == COLLAPSED)
            what = "collapsed"
        else:
            p.Expand(waitTime=0)
            ok = _poll(lambda: p.ExpandCollapseState == EXPANDED)
            what = "expanded"
        if ok:
            return f"ok - {what} {_label(c)} without the mouse (UIA ExpandCollapse)"
        return f"ok? - asked {_label(c)} to be {what} but its state did not change - check the screen"
    run.keep_focus = state != EXPANDED                   # an open drop-down/menu closes when its app loses focus
    return run


def _legacy_plan(c):
    p = _pat(c, LEGACY)
    action = str(_prop(p, "DefaultAction", "") or "").strip() if p is not None else ""
    if not action:
        return None

    def run():
        p.DoDefaultAction(waitTime=0)
        return f"ok - did '{action}' on {_label(c)} without the mouse (MSAA default action)"
    return run


def _win32_listbox_item(c):
    """Item of a classic Win32 LISTBOX (also WinForms ListBox / combo drop-down list)? Its UIA proxy selects with
    LB_SETCURSEL, which sends no LBN_SELCHANGE: the row looks selected but the app never hears about it."""
    try:
        cls = str(_prop(c.GetParentControl(), "ClassName", "") or "").upper()
    except Exception:  # noqa: BLE001
        return False
    return "LISTBOX" in cls or cls == "COMBOLBOX"


def _plan(name, args, c):
    """-> (ghost, run) or None. ghost: 'click' | 'double' | 'type' | 'move'."""
    kind = _kind(c)
    if RISKY.search(_name(c)):
        return None
    if not _prop(c, "IsEnabled", True):
        return None
    if name == "click":
        if kind in WHOLE:
            return None
        if kind in ITEMS:                                # Invoke/default action on an item = "open" (double click)
            if kind in OPENABLE and _win32_listbox_item(c):
                return None                              # Select there doesn't notify the app (seen live: WinForms)
            order = ((_select_plan, _toggle_plan) if kind in OPENABLE else
                     (_select_plan, _toggle_plan, _invoke_plan, _expand_plan))
        elif kind == "CheckBox":                         # Toggle can be verified; Qt also offers Invoke
            order = (_toggle_plan, _invoke_plan, _select_plan, _expand_plan, _legacy_plan)
        elif kind == "ComboBox":                         # a click opens/closes the drop-down
            order = (_expand_plan, _invoke_plan, _toggle_plan, _select_plan, _legacy_plan)
        else:
            order = (_invoke_plan, _toggle_plan, _select_plan, _expand_plan, _legacy_plan)
        for mk in order:
            run = mk(c)
            if run:
                return "click", run
        return None
    if name == "double_click":
        run = _invoke_plan(c, "opened") if kind in OPENABLE else None
        return ("double", run) if run else None
    if name == "press_key":
        if str(args.get("key", "")).strip().lower() in ("enter", "return") and kind in ("Button", "Hyperlink"):
            run = _invoke_plan(c)
            return ("click", run) if run else None
        return None
    if name == "type_text":
        return _type_plan(args, c)
    if name == "scroll":
        return _scroll_plan(args, c)
    return None


def _type_plan(args, c):
    text = str(args.get("text", ""))
    if _kind(c) != "Edit" or not text or any(ch in text for ch in "\r\n\t"):
        return None
    if _prop(c, "IsPassword", True):                     # can't tell -> treat it as a password field
        return None
    if str(_prop(c, "FrameworkId", "") or "") == "Chrome":   # web page / WebView2 / Electron: real keystrokes
        return None
    p = _pat(c, VALUE)
    if p is None or _prop(p, "IsReadOnly", True):
        return None
    current = _prop(p, "Value", None)
    if current is None or (current != "" and not args.get("replace")):
        return None                                      # appending: only the real keyboard does that right

    def run():
        p.SetValue(text, waitTime=0)
        got = _poll(lambda: (p.Value or "").replace("\r\n", "\n"), want=text)
        if got == text:
            return f"ok - typed {text[:40]!r} into {_label(c)} without the keyboard (UIA SetValue)"
        return f"ok? - set {_label(c)} to {text[:40]!r} but it now reads {str(got)[:40]!r} - check the screen"
    return "type", run


def _scroll_plan(args, c):
    try:
        clicks = int(args.get("clicks") or -3)
    except (TypeError, ValueError):
        return None
    if clicks == 0:
        return None
    box, p = c, None
    for _ in range(UP):                                  # the element itself or its first scrollable parent
        if box is None:
            break
        q = _pat(box, SCROLL)
        if q is not None and _prop(q, "VerticallyScrollable", False):
            p = q
            break
        try:
            box = box.GetParentControl()
        except Exception:  # noqa: BLE001
            break
    if p is None:
        return None
    n = min(abs(clicks) * SCROLL_STEPS_PER_CLICK, MAX_SCROLL_CALLS)
    amount, way = (SMALL_INC, "down") if clicks < 0 else (SMALL_DEC, "up")

    def run():
        before = _prop(p, "VerticalScrollPercent")
        for _ in range(n):
            p.Scroll(NO_AMOUNT, amount, waitTime=0)
        after = _poll(lambda: p.VerticalScrollPercent != before)
        pct = _prop(p, "VerticalScrollPercent")
        where = f"{pct:.0f}%" if isinstance(pct, (int, float)) and pct >= 0 else "?"
        if after:
            return f"ok - scrolled {_label(box)} {way} without the mouse, now at {where} (UIA Scroll)"
        end = "bottom" if clicks < 0 else "top"
        return f"ok - {_label(box)} did not move: probably already at the {end} ({where})"
    return "move", run


# ---------------------------------------------------------------- ghost click
def _ghost(cursor, x, y, kind, text=""):
    """Buddy glides to (x, y) and plays the action. The real mouse is never touched."""
    if cursor is None:
        return
    try:
        cursor.post("move_to_px", x, y)
        time.sleep(0.06)                                 # let the Qt thread pick up the new target
        end = time.time() + 1.2
        while not getattr(cursor, "arrived", True) and time.time() < end:
            time.sleep(0.02)
        if kind == "type":
            cursor.post("say", f"typing {text[:24]!r}" if text else "typing...", False)
        elif kind == "click":
            cursor.post("click")
        elif kind == "double":
            cursor.post("click")
            time.sleep(0.12)
            cursor.post("click")
    except Exception:  # noqa: BLE001  (an animation problem never stops the action)
        pass


# ---------------------------------------------------------------- the worker
class _Job:
    def __init__(self, name, args, mark, origin, size):
        self.name, self.args, self.mark, self.origin, self.size = name, args, mark, origin, size
        self.planned, self.go, self.done, self.cancel = (threading.Event() for _ in range(4))
        self.lock = threading.Lock()
        self.plan = None                                 # (ghost kind, label) once found
        self.started = False
        self.result = None
        self.error = None

    def run(self):
        try:
            with _com():
                c = _find(self.mark, self.origin, self.size, self.cancel)
                plan = _plan(self.name, self.args, c) if c is not None else None
                if plan is None:
                    return
                self.plan = plan
                self.planned.set()
                if not self.go.wait(GO_WAIT_S):
                    return
                with self.lock:
                    if self.cancel.is_set():
                        return
                    self.started = True
                self.result = plan[1]()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"[:200]
        finally:
            self.planned.set()
            self.done.set()


def _in_taskbar(mark, obs):
    """Box sits in the bottom 6 % of the screenshot (the taskbar row)."""
    try:
        return mark["box"][1] >= (obs or {}).get("size", (0, 1 << 20))[1] * 0.94
    except (TypeError, IndexError, KeyError):
        return False


def try_invisible(name, args, mark, obs, origin, cursor=None, timeout=1.5, restore=None):
    """Do the action through UIA patterns without the real mouse/keyboard. -> outcome text, or None if it
    can't be done that way (caller then uses the normal path). Never raises for "can't do"."""
    try:
        args = args or {}
        if name not in SUPPORTED or name == "right_click" or not isinstance(mark, dict) or not mark.get("box"):
            return None
        if RISKY.search(str(mark.get("name", ""))):
            return None
        mname = str(mark.get("name", "")).lower()
        if mark.get("area") or "running window" in mname or mname.startswith(("start", "search")) \
                and mark.get("type") == "Button" and _in_taskbar(mark, obs):
            return None                          # taskbar / tray: the point is to bring a window to the front ->
                                                 # the real click (an Invoke + focus give-back undoes exactly that)
        if name == "type_text" and mark.get("type") != "Edit":
            return None
        origin = tuple(origin if origin is not None else (obs or {}).get("origin", (0, 0)))
        size = tuple((obs or {}).get("size") or (1 << 20, 1 << 20))
        x1, y1, x2, y2 = mark["box"]
        cx, cy = round((x1 + x2) / 2 + origin[0]), round((y1 + y2) / 2 + origin[1])

        job = _Job(name, args, mark, origin, size)
        threading.Thread(target=job.run, daemon=True, name="uia_exec").start()
        if not job.planned.wait(timeout) or job.plan is None:
            job.cancel.set()                             # too slow (or nothing to do): the worker gives up
            return None
        _ghost(cursor, cx, cy, job.plan[0], str(args.get("text", "")))
        prev = _foreground()
        job.go.set()
        if job.done.wait(timeout):
            out = job.result                             # None if the pattern call raised before anything went out
        else:
            with job.lock:
                if not job.started:
                    job.cancel.set()
                    return None
            out = (f"ok? - sent the {name} to '{str(mark.get('name', ''))[:40]}' through UI Automation, but the "
                   f"app did not answer within {timeout:g}s - check the screen before repeating it")
        if out and (RESTORE_FOREGROUND if restore is None else restore) and not getattr(job.plan[1], "keep_focus", False) and _give_back(prev):
            out += " (the app grabbed the focus; it was given back to the window you were using)"
        return out
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- foreground give-back
# Seen live (Qt 6 and WinForms test windows): a pattern call on a BACKGROUND window often makes that app the
# foreground window (Invoke, Toggle, SetValue, Select, Scroll). The real mouse doesn't move, but keyboard focus
# would leave the user's app -> give it back right after the action (no input is injected for that).
# Not after an Expand: the drop-down / menu would close again as soon as its app loses the focus.
RESTORE_FOREGROUND = True


def _foreground():
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _give_back(prev, settle=0.25):
    """If the foreground moved away from prev during the action, make prev the foreground again.
    -> True if it had moved (and a give-back was attempted)."""
    if not prev:
        return False
    u = ctypes.windll.user32
    end = time.time() + settle                           # activation can arrive a little after the call returns
    now = _foreground()
    while now == prev and time.time() < end:
        time.sleep(0.03)
        now = _foreground()
    if not now or now == prev or not u.IsWindow(wintypes.HWND(prev)) or u.IsIconic(wintypes.HWND(prev)):
        return False
    try:
        k = ctypes.windll.kernel32
        fg_thread = u.GetWindowThreadProcessId(wintypes.HWND(now), None)
        me = k.GetCurrentThreadId()
        attached = bool(u.AttachThreadInput(me, fg_thread, True))
        try:
            u.SetForegroundWindow(wintypes.HWND(prev))
        finally:
            if attached:
                u.AttachThreadInput(me, fg_thread, False)
    except Exception:  # noqa: BLE001
        pass
    return True


# ---------------------------------------------------------------- read-only probe
def patterns_at(x, y, up=6, timeout=3.0):
    """Element under an absolute point and its parents, with the patterns each supports (nothing is invoked)."""
    out, fin = [], threading.Event()

    def work():
        try:
            with _com():
                c = _control_from_point(x, y)
                for _ in range(up):
                    if c is None:
                        break
                    pats = [n for pid, n in PATTERN_NAMES.items() if _pat(c, pid) is not None]
                    el = {"type": _kind(c), "name": _name(c)[:60], "rect": _rect(c), "patterns": pats,
                          "framework": _prop(c, "FrameworkId", ""), "class": _prop(c, "ClassName", "")}
                    v = _pat(c, VALUE)
                    if v is not None:
                        el["readonly"] = _prop(v, "IsReadOnly")
                    lg = _pat(c, LEGACY)
                    if lg is not None:
                        el["default_action"] = _prop(lg, "DefaultAction", "")
                    out.append(el)
                    c = c.GetParentControl()
        except Exception as e:  # noqa: BLE001
            out.append({"error": str(e)[:200]})
        finally:
            fin.set()

    threading.Thread(target=work, daemon=True).start()
    fin.wait(timeout)
    return list(out)


# ---------------------------------------------------------------- window picture (background work)
class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


PW_RENDERFULLCONTENT = 2


def capture_window(hwnd):
    """Picture of one window even if other windows cover it (PrintWindow + PW_RENDERFULLCONTENT).
    None if it is minimized, gone, or the capture failed / came back all black. Pixel size follows the
    process DPI awareness (perception/grounding set per-monitor v2 -> physical px)."""
    from PIL import Image
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    u.GetWindowDC.restype = wintypes.HDC
    u.GetWindowDC.argtypes = [wintypes.HWND]
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteDC.argtypes = [wintypes.HDC]
    try:
        hwnd = wintypes.HWND(int(hwnd))
        if not u.IsWindow(hwnd) or u.IsIconic(hwnd):
            return None
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        wdc = u.GetWindowDC(hwnd)
        mdc = g.CreateCompatibleDC(wdc)
        bmp = g.CreateCompatibleBitmap(wdc, w, h)
        old = g.SelectObject(mdc, bmp)
        try:
            if not u.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
                return None
            bih = _BIH(ctypes.sizeof(_BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)   # top-down BGRA
            buf = ctypes.create_string_buffer(w * h * 4)
            if not g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0):
                return None
            img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1).copy()
        finally:
            g.SelectObject(mdc, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mdc)
            u.ReleaseDC(hwnd, wdc)
        if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
            return None                                  # GPU surface that PrintWindow could not read
        return img
    except Exception:  # noqa: BLE001
        return None
