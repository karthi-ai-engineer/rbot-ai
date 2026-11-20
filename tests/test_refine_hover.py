"""Offline tests for the zoom-in refine (coordinate pointing) and the hover check (what is under the pointer).

  python -m pytest -q tests/test_refine_hover.py

Fake model, fake UI Automation (ControlFromPoint), fake window lookup, real actions.Executor with its mouse
controller and input listeners replaced by stubs. Nothing here moves the real mouse or presses keys.
"""
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from PIL import Image  # noqa: E402

from rbot.desktop import actions  # noqa: E402
from rbot.core import agent  # noqa: E402
from rbot.core import agent_graph  # noqa: E402

W, H = 1920, 1080


# ============================================================================ engines
ENGINES = {"legacy": agent.run_task, "graph": agent_graph.run_task}   # run_task tests run on both loops
ENGINE = "legacy"


@pytest.fixture(params=sorted(ENGINES))
def engine(request):
    global ENGINE
    ENGINE = request.param
    yield request.param
    ENGINE = "legacy"


# ============================================================================ fakes
def msg(content="", calls=None):
    tc = [NS(function=NS(name=n, arguments=json.dumps(a))) for n, a in (calls or [])]
    return NS(content=content, tool_calls=tc or None)


def point_llm(*replies, delay=0.0, extra_kw=False):
    """Fake model for the refine call: returns the replies in order (str -> content, Exception -> raise)."""
    seen = []

    def llm(model, messages, thinking, **kw):
        seen.append({"model": model, "thinking": thinking, "kw": kw, "text": messages[0]["content"][0]["text"],
                     "image": messages[0]["content"][1]["image_url"]["url"]})
        if delay:
            time.sleep(delay)
        r = replies[min(len(seen) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return (r if isinstance(r, NS) else msg(r)), NS(prompt_tokens=5, completion_tokens=3)
    llm.seen = seen
    if extra_kw:
        llm.extra_kw = True
    return llm


def obs_with(marks=(), size=(W, H), origin=(0, 0)):
    im = Image.new("RGB", size, (240, 240, 240))
    ms = [{"id": i, "name": n, "type": t, "box": list(b)} for i, (n, t, b) in enumerate(marks)]
    return {"image": im, "marked": im, "size": size, "origin": origin, "window": "Fake", "marks": ms}


def marks_of(o):
    return {m["id"]: m for m in o["marks"]}


def px_of(nx, ny, size=(W, H)):
    """0-1000 -> px exactly like agent.resolve."""
    return agent.resolve({"x": nx, "y": ny}, {}, size)


class Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class Ctl:
    """Fake uiautomation control."""
    def __init__(self, kind, name, rect, parent=None, delay=0.0):
        self.ControlTypeName, self.Name, self.BoundingRectangle = kind, name, Rect(*rect)
        self.parent, self.delay = parent, delay

    def GetParentControl(self):
        if self.delay:
            time.sleep(self.delay)
        return self.parent


@pytest.fixture
def fake_uia(monkeypatch):
    """state["hit"] = control returned by ControlFromPoint, state["root"] = window handle at the point now,
    state["roots_then"] = handle at observe time, state["delay"] = seconds ControlFromPoint takes."""
    import uiautomation as auto
    state = {"hit": None, "root": 100, "roots_then": 100, "delay": 0.0, "calls": 0}

    def cfp(x, y):
        state["calls"] += 1
        if state["delay"]:
            time.sleep(state["delay"])
        return state["hit"]
    monkeypatch.setattr(auto, "ControlFromPoint", cfp)
    monkeypatch.setattr(agent, "root_at", lambda x, y: state["root"])
    monkeypatch.setattr(agent, "own_window", lambda h: h == 999)
    monkeypatch.setattr(agent, "window_title", lambda h: {200: "Save As", 100: "Fake App"}.get(h, "?"))
    return state


class FakeMouse:
    """Stands in for pynput's mouse.Controller: records instead of moving / clicking."""
    def __init__(self, on_press=None):
        self._pos, self.moves, self.presses, self.on_press = (0, 0), [], [], on_press

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, p):
        self._pos = p
        self.moves.append(p)

    def press(self, b):
        self.presses.append((b, self._pos))
        if self.on_press:
            self.on_press(self._pos)

    def release(self, b):
        pass

    def scroll(self, dx, dy):
        pass


class Dummy:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def make_executor(monkeypatch, on_press=None):
    monkeypatch.setattr(actions.mouse, "Listener", Dummy)
    monkeypatch.setattr(actions.keyboard, "GlobalHotKeys", Dummy)
    monkeypatch.setattr(actions, "STEP", 0.0)
    ex = actions.Executor()
    ex.mouse = FakeMouse(on_press)
    ex._last = (0, 0)
    return ex


# ============================================================================ target param
def test_target_param_in_tools_and_parsing():
    props = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in agent.TOOLS}
    for n in ("click", "double_click", "right_click", "move", "drag"):
        assert props[n]["target"]["type"] == "string"
    assert '"target"' in agent.SYSTEM
    m = msg(calls=[("click", {"x": 412, "y": 300, "target": "three-dot menu on the report card", "reason": "menu"})])
    name, args = agent.parse_reply(m)
    assert name == "click" and args["target"] == "three-dot menu on the report card"
    # leaked JSON in content (no tool_calls) keeps the target too
    name, args = agent.parse_reply(msg('{"name": "click", "arguments": {"x": 5, "y": 6, "target": "OK button"}}'))
    assert args["target"] == "OK button"
    # target shows in the history text, but does not make "the same action" look different
    assert "'three-dot menu on the report card'" in agent.describe("click", {"x": 412, "y": 300,
                                                                            "target": "three-dot menu on the report card"}, {})
    assert agent.action_key("click", {"x": 1, "y": 2, "target": "a"}) == agent.action_key("click", {"x": 1, "y": 2, "target": "b"})


def test_refine_uses_target_then_reason():
    o = obs_with()
    llm = point_llm('{"x": 500, "y": 500}')
    agent.refine_args("click", {"x": 500, "y": 500, "target": "Download report button", "reason": "r"}, o, {}, llm, "m")
    agent.refine_args("click", {"x": 500, "y": 500, "reason": "open the menu"}, o, {}, llm, "m")
    assert "Download report button" in llm.seen[0]["text"]
    assert "open the menu" in llm.seen[1]["text"]
    assert all(s["thinking"] is False for s in llm.seen)
    assert llm.seen[0]["image"].startswith("data:image/png;base64,")


def test_refine_extra_kw_path():
    """The real llm (make_llm) takes tools/max_tokens/timeout: refine asks without tools and short."""
    llm = point_llm('{"x": 500, "y": 500}', extra_kw=True)
    agent.refine_args("click", {"x": 500, "y": 500, "target": "t"}, obs_with(), {}, llm, "m")
    kw = llm.seen[0]["kw"]
    assert kw["tools"] is None and kw["max_tokens"] == agent.REFINE_MAX_TOKENS and kw["timeout"] == agent.REFINE_TIMEOUT


# ============================================================================ crop / mapping
def test_zoom_crop_clamped():
    assert agent.zoom_crop((W, H), 960, 540) == (780, 420, 1140, 660)
    assert agent.zoom_crop((W, H), 3, 2) == (0, 0, 360, 240)                    # top-left corner
    assert agent.zoom_crop((W, H), W - 1, H - 1) == (W - 360, H - 240, W, H)   # bottom-right corner
    assert agent.zoom_crop((200, 100), 50, 50) == (0, 0, 200, 100)             # image smaller than the crop
    z = agent.make_zoom(Image.new("RGB", (W, H)), (0, 0, 360, 240), (3, 2))
    assert z.size == (1080, 720)


@pytest.mark.parametrize("guess,reply", [((2, 3), (100, 900)), ((998, 997), (950, 40)), ((500, 500), (500, 500)),
                                         ((0, 1000), (0, 1000)), ((1000, 0), (1000, 0))])
def test_refine_maps_back(guess, reply):
    o = obs_with()
    llm = point_llm(json.dumps({"x": reply[0], "y": reply[1]}))
    saved = []
    new, info, note = agent.refine_args("click", {"x": guess[0], "y": guess[1], "target": "t"}, o, {}, llm, "m",
                                        save=lambda suf, im: saved.append((suf, im.size)) or "z.jpg")
    px, py = px_of(*guess)
    l, t, r, b = agent.zoom_crop((W, H), px, py)
    ex_x = l + reply[0] * (r - l) / 1000
    ex_y = t + reply[1] * (b - t) / 1000
    rx, ry = agent.resolve(new, {}, (W, H))
    assert abs(rx - ex_x) <= 1.0 and abs(ry - ex_y) <= 1.0, (rx, ry, ex_x, ex_y)
    assert 0 <= new["x"] <= 1000 and 0 <= new["y"] <= 1000
    assert info[0]["status"] == "ok" and info[0]["orig"] == [float(guess[0]), float(guess[1])]
    assert info[0]["refined"] == [new["x"], new["y"]] and info[0]["zoom"] == "z.jpg"
    assert saved == [("", (1080, 720))] and "latency_s" in info[0] and "zoom check moved" in note


def test_refine_corrects_offset_example():
    """Target really at px (1000, 600); the model guessed 30 units left. Zoom answer -> lands on the target."""
    o = obs_with()
    gx, gy = 1000 * 1000 / (W - 1) - 30, 600 * 1000 / (H - 1)
    px, py = px_of(gx, gy)
    l, t, r, b = agent.zoom_crop((W, H), px, py)
    ans = {"x": (1000 - l) * 1000 / (r - l), "y": (600 - t) * 1000 / (b - t)}
    new, info, _ = agent.refine_args("click", {"x": gx, "y": gy, "target": "t"}, o, {}, point_llm(json.dumps(ans)), "m")
    rx, ry = agent.resolve(new, {}, (W, H))
    assert abs(rx - 1000) <= 1 and abs(ry - 600) <= 1


@pytest.mark.parametrize("reply,status", [
    ("I am not sure where that is.", "garbage"),
    ('{"x": 1500, "y": 20}', "garbage"),                       # outside 0-1000
    ('{"none": true}', "none"),
    ("<think>hmm</think>```json\n{\"none\": true}\n```", "none"),
    (RuntimeError("gateway 502"), "error"),
])
def test_refine_fallback(reply, status):
    args = {"x": 400, "y": 300, "target": "t", "reason": "r"}
    new, info, note = agent.refine_args("click", args, obs_with(), {}, point_llm(reply), "m")
    assert new["x"] == 400 and new["y"] == 300                 # kept the guess
    assert info[0]["status"] == status and "refined" not in info[0]
    assert "kept your guess" in note


def test_refine_timeout(monkeypatch):
    monkeypatch.setattr(agent, "REFINE_TIMEOUT", 0.3)
    t0 = time.time()
    new, info, note = agent.refine_args("click", {"x": 400, "y": 300, "target": "t"}, obs_with(), {},
                                        point_llm('{"x": 1, "y": 1}', delay=2.0), "m")
    assert time.time() - t0 < 1.0
    assert info[0]["status"] == "timeout" and new["x"] == 400 and "timed out" in note


def test_parse_point_formats():
    assert agent.parse_point(msg('{"x": 12, "y": 34}')) == (12.0, 34.0)
    assert agent.parse_point(msg('[{"bbox_2d": [100, 200, 300, 400], "label": "menu"}]')) == (200.0, 300.0)
    assert agent.parse_point(msg('{"point_2d": [7, 8]}')) == (7.0, 8.0)
    assert agent.parse_point(msg(calls=[("click", {"x": 5, "y": 6})])) == (5.0, 6.0)   # answered with a tool call
    assert agent.parse_point(msg('The element is not visible here.')) == "none"
    assert agent.parse_point(msg("")) is None


# ============================================================================ snap / skip
def test_snap_inside_small_box():
    o = obs_with([("More options", "Button", (1000, 600, 1040, 640)),      # 40x40 -> snap
                  ("Report card", "Group", (800, 500, 1400, 900))])        # big container around it
    llm = point_llm('{"x": 1, "y": 1}')
    gx, gy = agent._to_norm(1003, 604, (W, H))
    new, info, note = agent.refine_args("click", {"x": gx, "y": gy}, o, marks_of(o), llm, "m")
    assert agent.resolve(new, {}, (W, H)) == (1020, 620)
    assert info[0]["status"] == "snap" and info[0]["box_id"] == 0 and not llm.seen and "snapped" in note


def test_inside_button_sized_box_no_refine():
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])   # 200x40: already on an element
    llm = point_llm('{"x": 1, "y": 1}')
    gx, gy = agent._to_norm(1010, 610, (W, H))
    new, info, note = agent.refine_args("click", {"x": gx, "y": gy}, o, marks_of(o), llm, "m")
    assert new == {"x": gx, "y": gy} and info[0]["status"] == "in_box" and not llm.seen and note == ""


def test_inside_big_box_still_refined():
    o = obs_with([("page", "Document", (0, 100, 1900, 1000))])
    llm = point_llm('{"x": 500, "y": 500}')
    _, info, _ = agent.refine_args("click", {"x": 300, "y": 300}, o, marks_of(o), llm, "m")
    assert info[0]["status"] == "ok" and len(llm.seen) == 1


def test_id_targets_and_other_tools_untouched():
    llm = point_llm('{"x": 1, "y": 1}')
    o = obs_with([("OK", "Button", (10, 10, 50, 30))])
    assert agent.refine_args("click", {"id": 0}, o, marks_of(o), llm, "m") == ({"id": 0}, [], "")
    assert agent.refine_args("scroll", {"x": 5, "y": 5}, o, marks_of(o), llm, "m") == ({"x": 5, "y": 5}, [], "")
    assert agent.refine_args("click", {"x": "abc", "y": 5}, o, marks_of(o), llm, "m")[1] == []   # execute reports it
    assert not llm.seen


def test_drag_refines_both_ends():
    llm = point_llm('{"x": 500, "y": 500}', '{"none": true}')
    saved = []
    new, info, note = agent.refine_args("drag", {"from_x": 300, "from_y": 300, "to_x": 700, "to_y": 700,
                                                 "target": "file to the folder"}, obs_with(), {}, llm, "m",
                                        save=lambda s, im: saved.append(s) or f"z{s}.jpg")
    assert [i["status"] for i in info] == ["ok", "none"] and saved == ["_from", "_to"]
    assert new["to_x"] == 700 and "start point" in llm.seen[0]["text"] and "end point" in llm.seen[1]["text"]


# ============================================================================ hover check
def test_hover_id_ok_parent_matches(fake_uia, monkeypatch):
    mark = ("Download report", "Button", (1000, 600, 1200, 640))
    o = obs_with([mark])
    btn = Ctl("ButtonControl", "Download report", (1000, 600, 1200, 640))
    fake_uia["hit"] = Ctl("TextControl", "", (1010, 610, 1100, 630), parent=btn)
    ex = make_executor(monkeypatch)
    info = {}
    out = agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 100}, info=info)
    assert out == "ok" and info["matched"] and len(ex.mouse.presses) == 1
    assert ex.mouse.moves == [(1100, 620)]                     # exactly one move: no double-move for the check
    assert ex.before_press is None and ex._busy is False


def test_hover_id_ok_rect_match_with_origin(fake_uia, monkeypatch):
    """Second monitor: marks are monitor-relative, UIA rects absolute. Names differ (e.g. DOM marks)."""
    o = obs_with([("report", "link", (100, 100, 300, 140))], origin=(-1920, 0))
    fake_uia["hit"] = Ctl("HyperlinkControl", "Download report", (-1820, 100, -1620, 140))
    ex = make_executor(monkeypatch)
    info = {}
    agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], o["origin"], roots={0: 100}, info=info)
    assert info["matched"] and ex.mouse.presses and ex.mouse.moves == [(-1720, 120)]


def test_hover_blocked_by_other_window(fake_uia, monkeypatch):
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])
    fake_uia["hit"] = Ctl("ButtonControl", "Cancel", (1050, 590, 1150, 650),
                          parent=Ctl("WindowControl", "Save As", (800, 300, 1400, 800)))
    fake_uia["root"] = 200                                      # a dialog popped up on top of it
    ex = make_executor(monkeypatch)
    with pytest.raises(agent.Covered) as e:
        agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 100})
    assert isinstance(e.value, agent.BadAction)
    assert "something else is on top: 'Cancel' (Button) in 'Save As'" in str(e.value)
    assert ex.mouse.presses == [] and ex._busy is False and ex.before_press is None


def test_hover_same_window_mismatch_still_clicks(fake_uia, monkeypatch):
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])
    fake_uia["hit"] = Ctl("TextControl", "something", (900, 500, 1300, 700))
    ex = make_executor(monkeypatch)
    info = {}
    agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 100}, info=info)
    assert ex.mouse.presses and not info["matched"] and not info.get("blocked")


def test_hover_own_window_never_blocks(fake_uia, monkeypatch):
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])
    fake_uia["hit"] = Ctl("PaneControl", "buddy", (1000, 600, 1100, 700))
    fake_uia["root"] = 999                                      # our own overlay / chat window
    ex = make_executor(monkeypatch)
    agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 100})
    assert ex.mouse.presses


def test_hover_timeout_proceeds(fake_uia, monkeypatch):
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])
    fake_uia["hit"] = Ctl("ButtonControl", "Cancel", (0, 0, 10, 10))
    fake_uia["root"] = 200                                      # would block - but UIA is too slow to tell
    fake_uia["delay"] = 1.5
    ex = make_executor(monkeypatch)
    info = {}
    t0 = time.time()
    agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 200}, info=info)
    dt = time.time() - t0                                       # same window, UIA too slow -> just click
    assert ex.mouse.presses and info["timed_out"] and dt < 0.6, dt


def test_hover_timeout_still_blocks_other_window(fake_uia, monkeypatch):
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])
    fake_uia["hit"] = Ctl("ButtonControl", "Cancel", (0, 0, 10, 10))
    fake_uia["root"] = 200                                      # a different window is there now
    fake_uia["delay"] = 1.5                                     # and UIA is slow: the cheap window check still runs
    ex = make_executor(monkeypatch)
    with pytest.raises(agent.Covered):
        agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 100}, info={})
    assert not ex.mouse.presses


def test_hover_slow_parent_walk_times_out(fake_uia, monkeypatch):
    o = obs_with([("Download report", "Button", (1000, 600, 1200, 640))])
    chain = None
    for i in range(15):
        chain = Ctl("GroupControl", f"g{i}", (0, 0, 5, 5), parent=chain, delay=0.1)
    fake_uia["hit"], fake_uia["root"] = chain, 200
    ex = make_executor(monkeypatch)
    info = {}
    t0 = time.time()
    agent.execute("click", {"id": 0}, ex, marks_of(o), o["size"], (0, 0), roots={0: 200}, info=info)
    assert ex.mouse.presses and info["timed_out"] and time.time() - t0 < 0.6


def test_hover_coordinate_click_reports_hit(fake_uia, monkeypatch):
    o = obs_with()
    fake_uia["hit"] = Ctl("TextControl", "", (0, 0, 5, 5),
                          parent=Ctl("ButtonControl", "Download report", (0, 0, 50, 20)))
    fake_uia["root"] = 200                                      # coordinate clicks are never blocked
    ex = make_executor(monkeypatch)
    out = agent.execute("click", {"x": 500, "y": 500}, ex, {}, o["size"], (0, 0), roots={})
    assert out == "ok - clicked on 'Download report' (Button)" and ex.mouse.presses
    out = agent.execute("right_click", {"x": 500, "y": 500}, ex, {}, o["size"], (0, 0), roots={})
    assert out == "ok - right-clicked on 'Download report' (Button)"


def test_hover_broken_check_still_clicks(fake_uia, monkeypatch):
    monkeypatch.setattr(agent, "hover_check", lambda *a: 1 / 0)
    ex = make_executor(monkeypatch)
    info = {}
    assert agent.execute("click", {"x": 5, "y": 5}, ex, {}, (W, H), (0, 0), info=info) == "ok"
    assert ex.mouse.presses and "division" in info["error"]


def test_hover_switch_off(fake_uia, monkeypatch):
    monkeypatch.setattr(agent, "HOVER_CHECK", False)
    ex = make_executor(monkeypatch)
    agent.execute("click", {"x": 5, "y": 5}, ex, {}, (W, H), (0, 0))
    assert ex.mouse.presses and fake_uia["calls"] == 0


def test_hover_takeover_and_killswitch(fake_uia, monkeypatch):
    """While the pointer waits for the check: our own (injected) moves and the busy window are not a takeover;
    a kill switch pressed during the check cancels the click."""
    ex = make_executor(monkeypatch)
    calls = []
    ex.takeover_handler = lambda: calls.append(1)
    ex._grace_until = 0.0

    def during(x, y):
        ex._on_move(x, y, True)                                 # the move we injected
        ex._on_click(x, y, None, True, True)                    # injected click event
        assert ex._busy                                         # real input in this window is ignored too
    ex.before_press = during
    ex.click(10, 10)
    assert not ex.takeover_pending and not calls and ex.mouse.presses
    ex.before_press = lambda x, y: setattr(ex, "killed", True)
    with pytest.raises(actions.KillSwitch):
        ex.click(20, 20)
    assert len(ex.mouse.presses) == 1 and ex._busy is False


def test_mark_roots(monkeypatch):
    monkeypatch.setattr(agent, "root_at", lambda x, y: int(x) * 10000 + int(y))
    o = obs_with([("a", "Button", (10, 20, 30, 40))], origin=(100, 0))
    assert agent.mark_roots(o) == {0: 120 * 10000 + 30}


# ============================================================================ run_task integration
class Screen:
    def __init__(self, marks=()):
        self.state, self.n_obs, self.marks = 0, 0, list(marks)

    def observe(self):
        self.n_obs += 1
        o = obs_with(self.marks)
        o["image"] = Image.new("RGB", (W, H), (self.state * 40 % 255, 0, 0))
        o["marked"] = o["image"]
        return o

    @staticmethod
    def diff(a, b):
        return 0.05 if a.tobytes() != b.tobytes() else 0.0


def script_llm(script):
    """Agent model + refine model in one fake: tool-call steps from script, refine questions answered by refine."""
    seen = []

    def llm(model, messages, thinking, **kw):
        text = messages[-1]["content"][0]["text"]
        seen.append((model, thinking, text))
        r = script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, NS(prompt_tokens=1, completion_tokens=1)
    llm.seen = seen
    return llm


def C(tool, **a):
    return msg(calls=[(tool, {"reason": f"r-{tool}", **a})])


def run(script, screen, ex, levels=(("m", False), ("m", True)), max_steps=5):
    d = Path(tempfile.mkdtemp())
    llm = script_llm(list(script))
    res = ENGINES[ENGINE]("test task", ex, None, max_steps, observe=screen.observe, diff_ratio=screen.diff, llm=llm,
                          levels=list(levels), run_dir=d, settle=False, keep_images=True)
    nodes = [json.loads(x) for x in (d / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
    return res, nodes, llm.seen, d


def test_run_task_refine_logged(fake_uia, monkeypatch, engine):
    s = Screen()
    fake_uia["hit"] = Ctl("ButtonControl", "Download report", (0, 0, 10, 10))
    ex = make_executor(monkeypatch, on_press=lambda p: setattr(s, "state", s.state + 1))
    gx, gy = 470, 500                                          # target really at 0-1000 (500, 500)
    px, py = px_of(gx, gy)
    l, t, r, b = agent.zoom_crop((W, H), px, py)
    tx, ty = px_of(500, 500)
    ans = json.dumps({"x": (tx - l) * 1000 / (r - l), "y": (ty - t) * 1000 / (b - t)})
    res, nodes, seen, d = run([C("click", x=gx, y=gy, target="Download report button"), msg(ans),
                               C("done", summary="ok")], s, ex)
    assert res == ("done", "ok")
    n = nodes[0]
    assert n["ok"] and n["args"]["x"] == gx                    # the log keeps what the model said ...
    rf = n["refine"][0]
    assert rf["status"] == "ok" and rf["orig"] == [470.0, 500.0] and abs(rf["refined"][0] - 500) < 0.6
    assert rf["zoom"] == "step_01_zoom.jpg" and (d / "step_01_zoom.jpg").exists() and rf["latency_s"] >= 0
    assert abs(ex.mouse.presses[0][1][0] - tx) <= 1            # ... and the click went to the refined spot
    assert n["hover"]["note"] == "clicked on 'Download report' (Button)"
    hist = seen[-1][2]                                         # the done step's prompt shows the history
    assert "zoom check moved the point to (500" in hist and "ok - clicked on 'Download report' (Button)" in hist
    assert seen[1][1] is False                                 # refine call: thinking off


def test_run_task_refine_fail_kept_guess(fake_uia, monkeypatch, engine):
    s = Screen()
    fake_uia["hit"] = None
    ex = make_executor(monkeypatch, on_press=lambda p: setattr(s, "state", s.state + 1))
    res, nodes, seen, d = run([C("click", x=300, y=300, target="menu"), msg("no idea"), C("done", summary="ok")],
                              s, ex)
    assert res[0] == "done" and nodes[0]["refine"][0]["status"] == "garbage"
    assert ex.mouse.presses[0][1] == px_of(300, 300)
    assert "zoom check failed (garbage) - kept your guess" in seen[-1][2]


def test_run_task_covered_retries_with_fresh_screenshot(fake_uia, monkeypatch, engine):
    s = Screen(marks=[("Download report", "Button", (1000, 600, 1200, 640))])
    fake_uia["hit"] = Ctl("ButtonControl", "Cancel", (1050, 590, 1150, 650))
    fake_uia["root"] = 200                                     # a 'Save As' dialog covers the button
    monkeypatch.setattr(agent, "mark_roots", lambda o: {0: 100})
    ex = make_executor(monkeypatch, on_press=lambda p: setattr(s, "state", s.state + 1))
    orig_observe = s.observe

    def observe():                                             # the next screenshot: dialog gone
        if s.n_obs >= 1:
            fake_uia["root"] = 100
            fake_uia["hit"] = Ctl("ButtonControl", "Download report", (1000, 600, 1200, 640))
        return orig_observe()
    s.observe = observe
    res, nodes, seen, d = run([C("click", id=0), C("click", id=0), C("done", summary="ok")], s, ex)
    assert nodes[0]["fail_reason"].startswith("something else is on top: 'Cancel' (Button) in 'Save As'")
    assert nodes[0]["hover"]["blocked"] and not nodes[0]["ok"]
    assert nodes[1]["level"] == 2 and nodes[1]["ok"] and "something else is on top" in seen[1][2]
    assert s.n_obs >= 2 and (d / "step_01_a2.jpg").exists()   # L2 got a fresh screenshot
    assert len(ex.mouse.presses) == 1 and res == ("done", "ok")
