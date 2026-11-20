"""Offline tests for uia_exec (invisible clicks through UI Automation patterns).

  python -m pytest -q tests/test_uia_exec.py

Fake UIA elements/patterns, fake ControlFromPoint, fake buddy cursor. Nothing here touches real UI Automation,
moves the real mouse or presses keys (pynput's mouse controller and SetCursorPos/SendInput/mouse_event are
replaced by spies that fail the test; GetCursorPos isn't compared because a live user may move the mouse).
"""
import ctypes
import time
from contextlib import nullcontext
from types import SimpleNamespace as NS

import pytest

from rbot.desktop import uia_exec as ux  # noqa: E402

ORIGIN = (1920, 0)                       # second monitor: mark boxes are monitor-relative
OBS = {"size": (1920, 1080), "origin": ORIGIN}


# ============================================================================ fakes
class Invoke:
    def __init__(self, delay=0.0, boom=False):
        self.calls, self.delay, self.boom = 0, delay, boom

    def Invoke(self, waitTime=0.5):
        if self.boom:
            raise RuntimeError("COM error")
        time.sleep(self.delay)
        self.calls += 1
        return True


class Toggle:
    def __init__(self, state=0, stuck=False):
        self.ToggleState, self.stuck, self.calls = state, stuck, 0

    def Toggle(self, waitTime=0.5):
        self.calls += 1
        if not self.stuck:
            self.ToggleState = 1 - self.ToggleState
        return True


class SelItem:
    def __init__(self, selected=False):
        self.IsSelected, self.calls = selected, 0

    def Select(self, waitTime=0.5):
        self.calls += 1
        self.IsSelected = True
        return True


class Expand:
    def __init__(self, state=ux.COLLAPSED):
        self.ExpandCollapseState, self.log = state, []

    def Expand(self, waitTime=0.5):
        self.log.append("expand")
        self.ExpandCollapseState = ux.EXPANDED

    def Collapse(self, waitTime=0.5):
        self.log.append("collapse")
        self.ExpandCollapseState = ux.COLLAPSED


class Legacy:
    def __init__(self, action="Press"):
        self.DefaultAction, self.calls = action, 0

    def DoDefaultAction(self, waitTime=0.5):
        self.calls += 1


class Value:
    def __init__(self, value="", readonly=False, mangle=None):
        self.Value, self.IsReadOnly, self.mangle, self.calls = value, readonly, mangle, 0

    def SetValue(self, v, waitTime=0.5):
        self.calls += 1
        self.Value = self.mangle(v) if self.mangle else v


class Scroll:
    def __init__(self, pct=0.0, scrollable=True):
        self.VerticalScrollPercent, self.VerticallyScrollable, self.calls = pct, scrollable, []

    def Scroll(self, h, v, waitTime=0.5):
        self.calls.append((h, v))
        step = 1.0 if v == ux.SMALL_INC else -1.0 if v == ux.SMALL_DEC else 0.0
        self.VerticalScrollPercent = min(100.0, max(0.0, self.VerticalScrollPercent + step))


class El:
    """Fake uiautomation Control. rect is ABSOLUTE screen px (like BoundingRectangle)."""
    def __init__(self, name, kind, rect, patterns=None, parent=None, password=False, enabled=True,
                 framework="Win32"):
        self.Name, self.ControlTypeName = name, kind + "Control"
        self.BoundingRectangle = NS(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
        self.patterns, self.parent = patterns or {}, parent
        self.IsPassword, self.IsEnabled, self.FrameworkId = password, enabled, framework

    def GetParentControl(self):
        return self.parent

    def GetPattern(self, pid):
        return self.patterns.get(pid)


class Cursor:
    def __init__(self):
        self.posts, self.arrived = [], True

    def post(self, name, *args):
        self.posts.append((name, args))


def absr(box):
    return [box[0] + ORIGIN[0], box[1] + ORIGIN[1], box[2] + ORIGIN[0], box[3] + ORIGIN[1]]


def mark(name, kind, box=(100, 100, 200, 140)):
    return {"id": 3, "name": name, "type": kind, "box": list(box)}


@pytest.fixture
def uia(monkeypatch):
    """state["hit"] = element ControlFromPoint returns (or fn(x, y)); state["points"] = points asked."""
    state = {"hit": None, "points": [], "delay": 0.0}

    def cfp(x, y):
        state["points"].append((x, y))
        time.sleep(state["delay"])
        h = state["hit"]
        return h(x, y) if callable(h) else h
    monkeypatch.setattr(ux, "_com", nullcontext)
    monkeypatch.setattr(ux, "_control_from_point", cfp)
    monkeypatch.setattr(ux, "VERIFY_S", 0.05)
    monkeypatch.setattr(ux, "_foreground", lambda: 0)   # never touch the real foreground window in tests

    class NoMouse:                                       # any real-mouse use fails the test
        def __init__(self, *a, **k):
            raise AssertionError("real mouse controller used")
    import pynput.mouse
    monkeypatch.setattr(pynput.mouse, "Controller", NoMouse)
    for fn in ("SetCursorPos", "SendInput", "mouse_event"):          # nor the raw Win32 input calls
        monkeypatch.setattr(ctypes.windll.user32, fn, NoMouse, raising=False)
    return state


def target(name, kind, patterns, box=(100, 100, 200, 140), **kw):
    """A window > element (with patterns) > a text child that ControlFromPoint hits."""
    win = El("Test window", "Window", absr((0, 0, 800, 600)))
    el = El(name, kind, absr(box), patterns, parent=win, **kw)
    child = El(name, "Text", absr((box[0] + 5, box[1] + 5, box[2] - 5, box[3] - 5)), parent=el)
    return el, child


# ============================================================================ click ladder
def test_click_invoke_uses_box_centre_plus_origin(uia):
    inv = Invoke()
    el, uia["hit"] = target("Save", "Button", {ux.INVOKE: inv})
    out = ux.try_invisible("click", {"id": 3}, mark("Save", "Button"), OBS, ORIGIN)
    assert out.startswith("ok - pressed 'Save' (Button) without the mouse")
    assert inv.calls == 1
    assert uia["points"][0] == (150 + 1920, 120)


def test_click_toggle_verified(uia):
    tg = Toggle(0)
    _, uia["hit"] = target("Remember me", "CheckBox", {ux.TOGGLE: tg})
    out = ux.try_invisible("click", {}, mark("Remember me", "CheckBox"), OBS, ORIGIN)
    assert tg.ToggleState == 1 and "now on" in out and out.startswith("ok - toggled")


def test_click_toggle_unverified_still_not_none(uia):
    tg = Toggle(0, stuck=True)
    _, uia["hit"] = target("Remember me", "CheckBox", {ux.TOGGLE: tg})
    out = ux.try_invisible("click", {}, mark("Remember me", "CheckBox"), OBS, ORIGIN)
    assert out.startswith("ok?") and tg.calls == 1     # sent once -> must not return None (no double action)


def test_checkbox_prefers_toggle_over_invoke(uia):
    tg, inv = Toggle(0), Invoke()
    _, uia["hit"] = target("Enable", "CheckBox", {ux.TOGGLE: tg, ux.INVOKE: inv})
    out = ux.try_invisible("click", {}, mark("Enable", "CheckBox"), OBS, ORIGIN)
    assert tg.ToggleState == 1 and inv.calls == 0 and "now on" in out


def test_combo_prefers_expand_over_invoke(uia):
    ex, inv = Expand(), Invoke()
    _, uia["hit"] = target("Color", "ComboBox", {ux.EXPAND: ex, ux.INVOKE: inv})
    assert "expanded" in ux.try_invisible("click", {}, mark("Color", "ComboBox"), OBS, ORIGIN)
    assert inv.calls == 0


def test_click_list_item_selects_not_invokes(uia):
    sel, inv = SelItem(), Invoke()
    _, uia["hit"] = target("report.pdf", "ListItem", {ux.SELECTION_ITEM: sel, ux.INVOKE: inv})
    out = ux.try_invisible("click", {}, mark("report.pdf", "ListItem"), OBS, ORIGIN)
    assert sel.IsSelected and inv.calls == 0 and "selected 'report.pdf'" in out


def test_click_list_item_without_selection_is_none(uia):
    inv, lg = Invoke(), Legacy("Double Click")
    _, uia["hit"] = target("report.pdf", "ListItem", {ux.INVOKE: inv, ux.LEGACY: lg})
    assert ux.try_invisible("click", {}, mark("report.pdf", "ListItem"), OBS, ORIGIN) is None
    assert inv.calls == 0 and lg.calls == 0             # Invoke/default action would OPEN it


def test_click_win32_listbox_item_is_none(uia):
    sel = SelItem()
    el, uia["hit"] = target("Fruit 3", "ListItem", {ux.SELECTION_ITEM: sel})
    el.parent.ClassName = "WindowsForms10.LISTBOX.app.0.2651b56_r8_ad1"
    assert ux.try_invisible("click", {}, mark("Fruit 3", "ListItem"), OBS, ORIGIN) is None and sel.calls == 0


def test_click_tab_item_falls_back_to_invoke(uia):
    inv = Invoke()
    _, uia["hit"] = target("Settings", "TabItem", {ux.INVOKE: inv})
    assert ux.try_invisible("click", {}, mark("Settings", "TabItem"), OBS, ORIGIN) and inv.calls == 1


def test_click_already_selected_tab(uia):
    sel = SelItem(selected=True)
    _, uia["hit"] = target("General", "TabItem", {ux.SELECTION_ITEM: sel})
    out = ux.try_invisible("click", {}, mark("General", "TabItem"), OBS, ORIGIN)
    assert "already selected" in out and sel.calls == 0


def test_click_combo_expands_then_collapses(uia):
    ex = Expand()
    _, uia["hit"] = target("Country", "ComboBox", {ux.EXPAND: ex})
    assert "expanded" in ux.try_invisible("click", {}, mark("Country", "ComboBox"), OBS, ORIGIN)
    assert "collapsed" in ux.try_invisible("click", {}, mark("Country", "ComboBox"), OBS, ORIGIN)
    assert ex.log == ["expand", "collapse"]


def test_click_leaf_node_is_not_expandable(uia):
    _, uia["hit"] = target("Leaf", "MenuItem", {ux.EXPAND: Expand(ux.LEAF)})
    assert ux.try_invisible("click", {}, mark("Leaf", "MenuItem"), OBS, ORIGIN) is None


def test_click_legacy_default_action(uia):
    lg = Legacy("Press")
    _, uia["hit"] = target("Old button", "Button", {ux.LEGACY: lg})
    out = ux.try_invisible("click", {}, mark("Old button", "Button"), OBS, ORIGIN)
    assert lg.calls == 1 and "MSAA default action" in out


def test_click_legacy_without_default_action_is_none(uia):
    _, uia["hit"] = target("Old button", "Button", {ux.LEGACY: Legacy("")})
    assert ux.try_invisible("click", {}, mark("Old button", "Button"), OBS, ORIGIN) is None


def test_click_no_pattern_is_none(uia):
    _, uia["hit"] = target("Mystery", "Button", {})
    assert ux.try_invisible("click", {}, mark("Mystery", "Button"), OBS, ORIGIN) is None


def test_click_whole_window_types_refused(uia):
    _, uia["hit"] = target("Page", "Document", {ux.INVOKE: Invoke()})
    assert ux.try_invisible("click", {}, mark("Page", "Document"), OBS, ORIGIN) is None


def test_click_disabled_is_none(uia):
    inv = Invoke()
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: inv}, enabled=False)
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN) is None and inv.calls == 0


def test_invoke_exception_is_none(uia):
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: Invoke(boom=True)})
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN) is None


# ============================================================================ matching
def test_match_by_rect_when_name_differs(uia):
    inv = Invoke()
    _, uia["hit"] = target("Edge tab title", "TabItem", {ux.INVOKE: inv})
    out = ux.try_invisible("click", {}, mark("Edge tab title (current tab)", "TabItem"), OBS, ORIGIN)
    assert out and inv.calls == 1


def test_not_found_is_none(uia):
    inv = Invoke()
    el, _ = target("Cancel", "Button", {ux.INVOKE: inv}, box=(400, 400, 480, 430))
    uia["hit"] = el                                      # something else is under the box now
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN) is None and inv.calls == 0
    assert len(uia["points"]) == 5                       # centre + 4 inner points tried


def test_found_at_second_point_when_centre_is_covered(uia):
    inv = Invoke()
    el, child = target("Save", "Button", {ux.INVOKE: inv})
    overlay = El("buddy", "Pane", [0, 0, 5000, 5000])
    uia["hit"] = lambda x, y: overlay if (x, y) == (150 + 1920, 120) else child
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN) and inv.calls == 1


def test_walk_limited_to_15_parents(uia):
    inv = Invoke()
    el = El("Save", "Button", absr((100, 100, 200, 140)), {ux.INVOKE: inv})
    leaf = El("x", "Text", [0, 0, 1, 1])
    node = leaf
    for i in range(15):                                  # 15 unrelated levels before the button
        nxt = El(f"lvl{i}", "Group", [0, 0, 1, 1])
        node.parent = nxt
        node = nxt
    node.parent = el
    uia["hit"] = leaf
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN) is None


def test_coordinate_click_without_mark_is_none(uia):
    assert ux.try_invisible("click", {"x": 500, "y": 500}, None, OBS, ORIGIN) is None
    assert uia["points"] == []


# ============================================================================ safety
@pytest.mark.parametrize("label", ["Delete", "Remove item", "Pay now", "Buy", "Purchase", "Send money",
                                   "Uninstall", "Format disk", "Sign out", "Signout"])
def test_risky_names_refused(uia, label):
    inv = Invoke()
    _, uia["hit"] = target(label, "Button", {ux.INVOKE: inv})
    assert ux.try_invisible("click", {}, mark(label, "Button"), OBS, ORIGIN) is None and inv.calls == 0


def test_risky_live_name_refused_even_if_mark_is_harmless(uia):
    inv = Invoke()
    _, uia["hit"] = target("Delete", "Button", {ux.INVOKE: inv})
    m = mark("Delete", "Button")
    m["name"] = "Bin"                                    # matched by rect, live element is "Delete"
    assert ux.try_invisible("click", {}, m, OBS, ORIGIN) is None and inv.calls == 0


def test_right_click_never(uia):
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: Invoke()})
    assert ux.try_invisible("right_click", {}, mark("Save", "Button"), OBS, ORIGIN) is None


# ============================================================================ double click / keys
def test_double_click_list_item_invokes(uia):
    inv = Invoke()
    _, uia["hit"] = target("report.pdf", "ListItem", {ux.INVOKE: inv, ux.SELECTION_ITEM: SelItem()})
    out = ux.try_invisible("double_click", {}, mark("report.pdf", "ListItem"), OBS, ORIGIN)
    assert inv.calls == 1 and out.startswith("ok - opened")


def test_double_click_button_is_none(uia):
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: Invoke()})
    assert ux.try_invisible("double_click", {}, mark("Save", "Button"), OBS, ORIGIN) is None


def test_press_enter_on_button_invokes_other_keys_none(uia):
    inv = Invoke()
    _, uia["hit"] = target("OK", "Button", {ux.INVOKE: inv})
    assert ux.try_invisible("press_key", {"key": "enter"}, mark("OK", "Button"), OBS, ORIGIN)
    assert ux.try_invisible("press_key", {"key": "tab"}, mark("OK", "Button"), OBS, ORIGIN) is None
    assert inv.calls == 1


def test_press_enter_in_edit_is_none(uia):
    _, uia["hit"] = target("(edit)", "Edit", {ux.VALUE: Value("x")})
    assert ux.try_invisible("press_key", {"key": "enter"}, mark("(edit)", "Edit"), OBS, ORIGIN) is None


# ============================================================================ type_text (SetValue rules)
def edit(value="", **kw):
    v = Value(value, **{k: kw.pop(k) for k in ("readonly", "mangle") if k in kw})
    el, child = target("", "Edit", {ux.VALUE: v}, **kw)
    return v, el


def test_type_into_empty_edit(uia):
    v, uia["hit"] = edit("")
    out = ux.try_invisible("type_text", {"text": "hello"}, mark("(edit)", "Edit"), OBS, ORIGIN)
    assert v.Value == "hello" and out.startswith("ok - typed 'hello'")


def test_type_into_non_empty_edit_refused_unless_replace(uia):
    v, uia["hit"] = edit("old")
    assert ux.try_invisible("type_text", {"text": "new"}, mark("(edit)", "Edit"), OBS, ORIGIN) is None
    assert v.Value == "old"
    out = ux.try_invisible("type_text", {"text": "new", "replace": True}, mark("(edit)", "Edit"), OBS, ORIGIN)
    assert v.Value == "new" and out.startswith("ok")


def test_type_password_refused(uia):
    v, uia["hit"] = edit("", password=True)
    assert ux.try_invisible("type_text", {"text": "hunter2"}, mark("(edit)", "Edit"), OBS, ORIGIN) is None
    assert v.calls == 0


def test_type_readonly_refused(uia):
    v, uia["hit"] = edit("", readonly=True)
    assert ux.try_invisible("type_text", {"text": "x"}, mark("(edit)", "Edit"), OBS, ORIGIN) is None


def test_type_web_field_refused(uia):
    v, uia["hit"] = edit("", framework="Chrome")       # Teams compose / web inputs -> real keyboard
    assert ux.try_invisible("type_text", {"text": "x"}, mark("(edit)", "Edit"), OBS, ORIGIN) is None


def test_type_document_or_multiline_refused(uia):
    _, uia["hit"] = target("(document)", "Document", {ux.VALUE: Value("")})
    assert ux.try_invisible("type_text", {"text": "x"}, mark("(document)", "Document"), OBS, ORIGIN) is None
    v, uia["hit"] = edit("")
    assert ux.try_invisible("type_text", {"text": "a\nb"}, mark("(edit)", "Edit"), OBS, ORIGIN) is None


def test_type_value_mismatch_reported(uia):
    v, uia["hit"] = edit("", mangle=lambda s: s.upper())
    out = ux.try_invisible("type_text", {"text": "abc"}, mark("(edit)", "Edit"), OBS, ORIGIN)
    assert out.startswith("ok?") and "'ABC'" in out


# ============================================================================ scroll
def test_scroll_uses_scrollable_parent(uia):
    sc = Scroll(10.0)
    win = El("List", "List", absr((0, 0, 400, 400)), {ux.SCROLL: sc})
    item = El("row 3", "ListItem", absr((100, 100, 200, 140)), parent=win)
    uia["hit"] = item
    out = ux.try_invisible("scroll", {"id": 3, "clicks": -2}, mark("row 3", "ListItem"), OBS, ORIGIN)
    assert out.startswith("ok - scrolled 'List' (List) down") and len(sc.calls) == 6
    assert all(c == (ux.NO_AMOUNT, ux.SMALL_INC) for c in sc.calls)


def test_scroll_at_bottom_says_so(uia):
    sc = Scroll(100.0)
    _, uia["hit"] = target("List", "List", {ux.SCROLL: sc})
    out = ux.try_invisible("scroll", {"clicks": -3}, mark("List", "List"), OBS, ORIGIN)
    assert "already at the bottom" in out


def test_scroll_without_pattern_is_none(uia):
    _, uia["hit"] = target("Label", "Button", {ux.SCROLL: Scroll(0, scrollable=False)})
    assert ux.try_invisible("scroll", {"clicks": 3}, mark("Label", "Button"), OBS, ORIGIN) is None


# ============================================================================ timeouts
def test_find_timeout_is_none(uia):
    inv = Invoke()
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: inv})
    uia["delay"] = 0.6
    t = time.time()
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN, timeout=0.15) is None
    assert time.time() - t < 0.5
    time.sleep(0.7)                                      # the worker wakes up, sees the cancel, does nothing
    assert inv.calls == 0


def test_act_timeout_is_not_none(uia):
    inv = Invoke(delay=0.5)                              # e.g. a button that opens a modal dialog
    _, uia["hit"] = target("Open", "Button", {ux.INVOKE: inv})
    out = ux.try_invisible("click", {}, mark("Open", "Button"), OBS, ORIGIN, timeout=0.15)
    assert out.startswith("ok?") and "did not answer" in out


# ============================================================================ ghost click
def test_ghost_click_moves_buddy_not_mouse(uia):
    cur = Cursor()
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: Invoke()})
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN, cursor=cur)
    assert cur.posts == [("move_to_px", (150 + 1920, 120)), ("click", ())]   # (real input calls are spied)


def test_ghost_double_and_type(uia):
    cur = Cursor()
    _, uia["hit"] = target("a.txt", "ListItem", {ux.INVOKE: Invoke()})
    ux.try_invisible("double_click", {}, mark("a.txt", "ListItem"), OBS, ORIGIN, cursor=cur)
    assert [p[0] for p in cur.posts] == ["move_to_px", "click", "click"]
    cur = Cursor()
    v, uia["hit"] = edit("")
    ux.try_invisible("type_text", {"text": "hi"}, mark("(edit)", "Edit"), OBS, ORIGIN, cursor=cur)
    assert cur.posts[0][0] == "move_to_px" and cur.posts[1][0] == "say" and "typing" in cur.posts[1][1][0]


def test_no_ghost_when_refused(uia):
    cur = Cursor()
    _, uia["hit"] = target("Mystery", "Button", {})
    assert ux.try_invisible("click", {}, mark("Mystery", "Button"), OBS, ORIGIN, cursor=cur) is None
    assert cur.posts == []


def test_ghost_waits_for_arrival(uia):
    class Slow(Cursor):
        def __init__(self):
            self.posts, self.t0 = [], None

        @property
        def arrived(self):
            self.t0 = self.t0 or time.time()
            return time.time() - self.t0 > 0.15
    cur, inv = Slow(), Invoke()
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: inv})
    t = time.time()
    assert ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN, cursor=cur)
    assert time.time() - t >= 0.15 and inv.calls == 1


def test_focus_give_back_noted(uia, monkeypatch):
    calls = []
    monkeypatch.setattr(ux, "_foreground", lambda: 111)
    monkeypatch.setattr(ux, "_give_back", lambda prev: calls.append(prev) or True)
    _, uia["hit"] = target("Save", "Button", {ux.INVOKE: Invoke()})
    out = ux.try_invisible("click", {}, mark("Save", "Button"), OBS, ORIGIN)
    assert calls == [111] and "given back" in out


def test_no_give_back_after_expand(uia, monkeypatch):
    calls = []
    monkeypatch.setattr(ux, "_foreground", lambda: 111)
    monkeypatch.setattr(ux, "_give_back", lambda prev: calls.append(prev) or True)
    _, uia["hit"] = target("Color", "ComboBox", {ux.EXPAND: Expand()})
    assert "expanded" in ux.try_invisible("click", {}, mark("Color", "ComboBox"), OBS, ORIGIN) and calls == []
    assert "collapsed" in ux.try_invisible("click", {}, mark("Color", "ComboBox"), OBS, ORIGIN) and calls == [111]


def test_no_give_back_when_refused(uia, monkeypatch):
    calls = []
    monkeypatch.setattr(ux, "_give_back", lambda prev: calls.append(prev) or True)
    _, uia["hit"] = target("Mystery", "Button", {})
    assert ux.try_invisible("click", {}, mark("Mystery", "Button"), OBS, ORIGIN) is None and calls == []


def test_give_back_skips_when_foreground_unchanged(monkeypatch):
    monkeypatch.setattr(ux, "_foreground", lambda: 5)
    assert ux._give_back(5, settle=0.05) is False and ux._give_back(0) is False


# ============================================================================ capture_window
def test_capture_window_bad_handles():
    assert ux.capture_window(0) is None
    assert ux.capture_window(0x7FFFFFF0) is None
