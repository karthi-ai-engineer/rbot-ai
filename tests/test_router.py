"""Offline tests for router.py (method ladder, PLAN §3) and its integration in BOTH agent loops.

  python -m pytest -q tests/test_router.py

Fakes only: a fake try_invisible (uia_exec), a fake BrowserExecutor + observe_tab (background mode), FakeEx raising
mouse_gate.MouseDenied. Nothing moves the real mouse, presses keys, opens windows or calls UI Automation.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

from rbot.core import agent  # noqa: E402
from rbot.core import agent_graph  # noqa: E402
from rbot.core import router  # noqa: E402
from rbot.core.agent import BadAction  # noqa: E402
from test_agent_graph import ENGINES, both, check_events, clean, of  # noqa: E402
from test_human_loop import C, FakeEx, Screen, model, setup  # noqa: E402

try:
    from rbot.desktop.mouse_gate import MouseDenied  # noqa: E402
except Exception:  # noqa: BLE001
    class MouseDenied(Exception):
        pass


# ============================================================================ fakes
class FakeUIA:
    """try_invisible stand-in. results: list of return values in order (last one repeats); screen: bumped on ok."""
    def __init__(self, screen=None, results=("ok - pressed it (UIA Invoke)",), change=True):
        self.s, self.results, self.change, self.calls = screen, list(results), change, []

    def __call__(self, name, args, mark, obs, origin, cursor=None):
        self.calls.append((name, (mark or {}).get("name"), tuple(origin)))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        if r and self.change and self.s is not None:
            self.s.state += 1
        return r


class Tab(Screen):
    """The buddy browser tab as browser_dom.observe_tab sees it (background obs)."""
    def observe(self):
        o = super().observe()
        o.update(origin=(0, 0), page_source="dom", background=True, window="Shop - tab")
        return o


class FakeBrowser:
    """browser_exec.BrowserExecutor stand-in: records calls, every action changes the tab."""
    kind = "browser"

    def __init__(self, tab, fail=()):
        self.tab, self.fail, self.calls = tab, set(fail), []

    def _do(self, name, *a):
        self.calls.append((name,) + a)
        if name in self.fail:
            raise RuntimeError("CDP timeout")
        self.tab.state += 1

    def click(self, x, y): self._do("click", x, y)
    def double_click(self, x, y): self._do("double_click", x, y)
    def right_click(self, x, y): self._do("right_click", x, y)
    def move(self, x, y): self._do("move", x, y)
    def drag(self, *a): self._do("drag", *a)
    def scroll(self, x, y, clicks): self._do("scroll", x, y, clicks)
    def type_text(self, t): self._do("type_text", t)
    def press_key(self, k): self._do("press_key", k)
    def hotkey(self, keys): self._do("hotkey", keys)
    def wait(self, s): self.calls.append(("wait", s))
    def hold(self, x, y, s): self._do("hold", x, y, s)
    def open_url(self, url): self._do("open_url", url)
    def bring_to_front(self): self.calls.append(("front",))
    def send_to_back(self): self.calls.append(("back",))
    def stop(self): self.calls.append(("stop",))
    def _check(self): pass


class DenyEx(FakeEx):
    """FakeEx whose n-th call of a method raises MouseDenied (the user said Not now and the gate timed out)."""
    def __init__(self, screen, deny_on):
        super().__init__(screen)
        self.deny_on = dict(deny_on)

    def _do(self, name, *a):
        k = self.n.get(name, 0) + 1
        if k in self.deny_on.get(name, ()):
            self.n[name] = k
            raise MouseDenied("the user said Not now")
        super()._do(name, *a)


def go(engine, script, screen, ex=None, loop=None, max_steps=10, task="test task", **kw):
    """Like test_agent_graph.go, with a task text (background mode routes web tasks to the tab)."""
    d = Path(tempfile.mkdtemp())
    llm = model(list(script))
    ex = ex or FakeEx(screen)
    events = []
    if engine == "graph":
        kw.setdefault("on_event", events.append)
    out = {"d": d, "events": events, "exc": None, "seen": llm.seen, "ex": ex}
    try:
        out["res"] = ENGINES[engine](task, ex, None, max_steps, observe=kw.pop("observe", screen.observe),
                                     diff_ratio=screen.diff, llm=llm, levels=[("m", False), ("m", True)], run_dir=d,
                                     settle=False, hitl=loop, keep_images=True, **kw)
    except Exception as e:  # noqa: BLE001
        out["exc"], out["res"] = e, None
    f = d / "steps.jsonl"
    out["nodes"] = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
    out["summ"] = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    return out


def acts(nodes):
    return [n for n in nodes if n.get("type") != "human"]


def obs_of(screen):
    o = screen.observe()
    o.setdefault("origin", (0, 0))
    return o, {m["id"]: m for m in o["marks"]}


# ============================================================================ unit: choosing a method
def test_plan_rules():
    s = Screen()
    o, marks = obs_of(s)
    r = router.Router(FakeEx(s), invisible=FakeUIA(s), observe=s.observe)
    assert r.plan("move_files", {}, marks, o) == "command" and r.plan("web_search", {}, marks, o) == "command"
    assert r.plan("wait", {}, marks, o) == "command" and r.plan("open_url", {}, marks, o) == "browser"
    assert r.plan("done", {}, marks, o) == r.plan("ask_human", {}, marks, o) == "none"
    assert r.plan("click", {"id": 1}, marks, o) == "uia"                         # a box in a normal window
    assert r.plan("scroll", {"id": 1}, marks, o) == "uia"
    assert r.plan("click", {"x": 5, "y": 5}, marks, o) == "mouse"                # no box -> real mouse
    assert r.plan("type_text", {"text": "hi"}, marks, o) == "mouse"              # nothing clicked yet
    assert r.plan("drag", {"from_id": 1, "to_id": 2}, marks, o) == "mouse"
    assert r.plan("click", {"id": 1}, marks, {**o, "page_source": "dom"}) == "mouse"   # live buddy browser page
    off = router.Router(FakeEx(s), observe=s.observe)                             # injected screen: no UIA
    assert off.try_invisible is None and off.plan("click", {"id": 1}, marks, o) == "mouse"


def test_invisible_switch(monkeypatch):
    s = Screen()
    monkeypatch.delenv("BUDDY_INVISIBLE", raising=False)
    from rbot.desktop import uia_exec
    assert getattr(router.Router(FakeEx(s), real_screen=True).try_invisible, "func", None) is uia_exec.try_invisible
    monkeypatch.setenv("BUDDY_INVISIBLE", "0")
    assert router.Router(FakeEx(s), real_screen=True).try_invisible is None
    assert getattr(router.Router(FakeEx(s), invisible=True).try_invisible, "func", None) is uia_exec.try_invisible   # explicit wins
    monkeypatch.setitem(sys.modules, "rbot.desktop.uia_exec", None)                         # module missing -> mouse only
    assert router.Router(FakeEx(s), invisible=True).try_invisible is None


def test_uia_then_mouse_in_the_same_attempt():
    s = Screen(live={(15, 5)})
    o, marks = obs_of(s)
    uia = FakeUIA(s, results=[None])
    ex = FakeEx(s)
    r = router.Router(ex, invisible=uia, observe=s.observe)
    assert r.execute("click", {"id": 1}, marks, o) == "ok" and r.method == "mouse"
    assert uia.calls == [("click", "el1", (0, 0))] and s.calls == [("click", (15, 5))]
    uia.results = ["ok - toggled 'el2' (UIA Toggle)"]
    assert r.execute("click", {"id": 2}, marks, o).startswith("ok - toggled") and r.method == "uia"
    assert len(s.calls) == 1                                                     # the real mouse stayed still
    uia.results = [RuntimeError("COM error")]                                    # a broken UIA call -> mouse
    r.execute("click", {"id": 1}, marks, o)
    assert r.method == "mouse" and r.fails["uia"] == 1


def test_type_text_goes_to_the_box_clicked_last():
    s = Screen(live={(15, 5)})
    o, marks = obs_of(s)
    uia = FakeUIA(s)
    r = router.Router(FakeEx(s), invisible=uia, observe=s.observe)
    r.execute("click", {"id": 3}, marks, o)
    r.execute("type_text", {"text": "hello"}, marks, o)
    assert [c[:2] for c in uia.calls] == [("click", "el3"), ("type_text", "el3")] and r.method == "uia"
    r.execute("hotkey", {"keys": ["ctrl", "s"]}, marks, o)                         # focus may have moved
    assert r.plan("type_text", {"text": "x"}, marks, o) == "mouse"


def test_failing_twice_falls_to_the_next_method_and_success_resets():
    s = Screen()
    o, marks = obs_of(s)
    r = router.Router(FakeEx(s), invisible=FakeUIA(s), observe=s.observe)
    for i in range(2):
        r.execute("click", {"id": 1}, marks, o)
        assert r.method == "uia"
        r.after_attempt({"ok": False, "method": "uia"}, True, "click", {"id": 1}, marks)
    assert r.plan("click", {"id": 1}, marks, o) == "mouse"
    r.execute("click", {"id": 1}, marks, o)
    assert r.method == "mouse"
    r.after_attempt({"ok": True, "method": "mouse"}, True)
    assert r.plan("click", {"id": 1}, marks, o) == "uia"
    r.after_attempt({"ok": False, "method": "uia"}, False)                         # not executed: not counted
    assert r.fails["uia"] == 0


@pytest.mark.parametrize("answer", [None, "No", "Yes"])
def test_risky_step_never_falls_back_silently(answer):
    s = Screen()
    o, marks = obs_of(s)
    asked = []
    ask = (lambda q, opts: asked.append((q, opts)) or answer) if answer else None
    ex = FakeEx(s)
    r = router.Router(ex, invisible=FakeUIA(s, results=["ok"], change=False), observe=s.observe, ask=ask)
    args = {"id": 1, "reason": "send the message"}
    r.fails["uia"] = 2                                                            # the quiet way failed twice
    if answer == "Yes":
        assert r.execute("click", args, marks, o) == "ok" and r.method == "mouse" and s.calls
    else:
        with pytest.raises(BadAction, match="^BLOCKED"):
            r.execute("click", args, marks, o)
        assert not s.calls                                                      # nothing was clicked
    if answer:
        assert asked == [("I couldn't do \"el1\" the quiet way. May I use the mouse to do it?", ["Yes", "No"])]
        assert r.take_confirms()[0]["answer"] == answer
    assert router.is_risky("click", {"reason": "Pay now"}) and router.is_risky("click", {}, {"name": "Send"})
    assert not router.is_risky("click", {"reason": "open the report"}, {"name": "Sender list"})
    assert not router.is_risky("delete_files", {"reason": "delete"})               # commands ask by themselves


def test_risky_uia_cannot_falls_to_mouse_without_question():
    """try_invisible returned None = nothing was executed, so the real mouse doing it is not a retry."""
    s = Screen()
    o, marks = obs_of(s)
    r = router.Router(FakeEx(s), invisible=FakeUIA(s, results=[None]), observe=s.observe, ask=lambda q, o: "No")
    r.execute("click", {"id": 1, "reason": "send it"}, marks, o)
    assert r.method == "mouse" and s.calls and not r.take_confirms()


def test_background_observe_and_surfaces():
    desk, tab = Screen(), Tab()
    b = FakeBrowser(tab)
    r = router.Router(FakeEx(desk), mode="background", task="buy a mug on shop.example.com", observe=desk.observe,
                      browser_ex=b, observe_tab=tab.observe)
    assert r.surface == "browser" and r.observe()["background"] is True
    o, marks = r.observe(), {m["id"]: m for m in tab.observe()["marks"]}
    assert r.plan("click", {"id": 1}, marks, o) == "browser"
    r.execute("click", {"id": 1}, marks, o)
    assert b.calls == [("click", 15, 5)] and r.method == "browser" and not desk.calls
    r.execute("open_app", {"name": "notepad"}, marks, desk.observe())              # a desktop step
    assert r.surface == "desktop" and "background" not in r.observe() and desk.calls[0][0] == "open_app"
    r.execute("open_url", {"url": "shop.example.com"}, marks, desk.observe())
    assert b.calls[-1] == ("open_url", "https://shop.example.com") and r.surface == "browser"
    assert r.method == "browser"
    plain = router.Router(FakeEx(desk), mode="background", task="sort my Downloads", observe=desk.observe,
                          browser_ex=b, observe_tab=tab.observe)
    assert plain.surface == "desktop" and "background" not in plain.observe()

    def broken():
        raise LookupError("no buddy-browser tab")
    r2 = router.Router(FakeEx(desk), mode="background", task="open example.com", observe=desk.observe,
                       browser_ex=b, observe_tab=broken)
    assert "background" not in r2.observe()                                       # no tab yet -> the screen
    live = router.Router(FakeEx(desk), mode="live", task="open example.com", observe=desk.observe)
    assert live.browser_ex is None and "background" not in live.observe()


def test_background_without_browser_module_runs_live(monkeypatch):
    monkeypatch.setitem(sys.modules, "rbot.browser.browser_exec", None)
    s = Screen()
    r = router.Router(FakeEx(s), mode="background", task="open example.com", observe=s.observe)
    assert r.mode == "live" and r.browser_ex is None


@pytest.mark.parametrize("risky,answer", [(False, None), (True, "Yes"), (True, "No"), (True, None)])
def test_background_browser_fails_twice_then_real_screen(risky, answer):
    desk, tab = Screen(), Tab()
    b = FakeBrowser(tab)
    ask = (lambda q, o: answer) if answer else None
    r = router.Router(FakeEx(desk), mode="background", task="open example.com", observe=desk.observe,
                      browser_ex=b, observe_tab=tab.observe, ask=ask)
    args = {"id": 1, "reason": "pay now" if risky else "open it"}
    assert not r.after_attempt({"ok": False, "method": "browser"}, True, "click", args, {})
    switched = r.after_attempt({"ok": False, "method": "browser"}, True, "click", args, {})
    allowed = not risky or answer == "Yes"
    assert switched is allowed and r.desktop_fallback is allowed
    assert (("front",) in b.calls) is allowed
    assert ("background" in r.observe()) is not allowed
    if allowed:
        assert r.after_attempt({"ok": True, "method": "mouse"}, True) is True      # back to the tab afterwards
        assert r.observe()["background"] is True


def test_wrap_handover_brings_the_tab_window_to_the_front():
    desk, tab = Screen(), Tab()
    b = FakeBrowser(tab)
    r = router.Router(FakeEx(desk), mode="background", task="sign in on example.com", observe=desk.observe,
                      browser_ex=b, observe_tab=tab.observe)
    h = r.wrap_handover(lambda *a, **k: b.calls.append(("human",)) or {"outcome": "done"})
    assert h("login") == {"outcome": "done"} and b.calls == [("front",), ("human",), ("back",)]
    r.surface = "desktop"
    b.calls.clear()
    h("login")
    assert b.calls == [("human",)]


# ============================================================================ both loops
def test_loop_uia_and_mouse_methods():
    def make(e):
        s = Screen(live={(25, 5)})
        uia = FakeUIA(s, results=["ok - pressed 'el1' (UIA Invoke)", None])
        r = go(e, [C("click", id=1), C("click", id=2), C("done", summary="k")], s, invisible=uia)
        r["uia"], r["s"] = uia.calls, s
        return r
    a, b = both(make)
    assert b["res"] == ("done", "k")
    assert [n["method"] for n in acts(b["nodes"])] == ["uia", "mouse", "none"]
    assert b["s"].calls == [("click", (25, 5))]                                    # only the 2nd click was real
    assert "1. click #1 'el1' -> ok - pressed 'el1' (UIA Invoke)" in b["seen"][-1][2]
    ev = b["events"]
    check_events(ev)
    assert [e["method"] for e in of(ev, "action")] == ["uia", "uia", "none"]       # planned
    assert [e["method"] for e in of(ev, "step_result")] == ["uia", "mouse", "none"]  # what really did it


def test_loop_uia_fails_twice_then_mouse():
    """UIA 'did it' but nothing changed, twice -> the next click on a box uses the real mouse."""
    def make(e):
        s = Screen(live={(35, 5)})
        uia = FakeUIA(s, results=["ok"], change=False)
        return go(e, [C("click", id=1), C("click", id=2), C("click", id=3), C("done", summary="k")], s,
                  invisible=uia)
    a, b = both(make)
    n = acts(b["nodes"])
    assert [(x["step"], x["level"], x["method"], x["ok"]) for x in n] == [
        (1, 1, "uia", False), (1, 2, "uia", False), (2, 1, "mouse", True), (3, 1, "none", True)]
    assert n[0]["fail_reason"] == "it did not change the screen"


@pytest.mark.parametrize("hitl,answer", [(False, None), (True, "No"), (True, "Yes")])
def test_loop_risky_never_falls_back_silently(hitl, answer):
    def make(e):
        s = Screen(live={(35, 5)})
        uia = FakeUIA(s, results=["ok"], change=False)
        loop = setup(s, answers=[answer, "Stop"])[0] if hitl else None
        script = [C("click", id=1, reason="send it"), C("click", id=2, reason="send it"),
                  C("click", id=3, reason="send the message"), C("click", id=3, reason="send the message"),
                  C("done", summary="k")]
        return go(e, script, s, loop=loop, invisible=uia, max_steps=3)
    a, b = both(make)
    n = acts(b["nodes"])
    third = [x for x in n if x["step"] == 2]
    if answer == "Yes":
        assert third[0]["method"] == "mouse" and third[0]["ok"]
        assert third[0]["confirm"] == [{"question": "I couldn't do \"el3\" the quiet way. May I use the mouse to "
                                                    "do it?", "answer": "Yes"}]
    else:
        assert third[0]["fail_reason"].startswith("BLOCKED:") and not third[0]["ok"]
        assert not any(x["method"] == "mouse" and x["ok"] for x in n)            # the mouse never did it
    if hitl:
        check_events(b["events"])
        asks = [e for e in of(b["events"], "needs_you") if e["kind"] == "ask"]
        assert asks and asks[0]["detail"].startswith("I couldn't do")


def test_loop_background_mode_uses_the_browser_executor(monkeypatch):
    def no_roots(obs):
        assert not obs.get("background"), "mark_roots on a background tab"
        return {}
    monkeypatch.setattr(agent, "mark_roots", no_roots)

    def make(e):
        desk, tab = Screen(), Tab()
        b = FakeBrowser(tab)
        uia = FakeUIA(desk)
        r = go(e, [C("open_url", url="shop.example.com"), C("click", id=1), C("type_text", text="mug"),
                   C("press_and_hold", id=2, seconds=2), C("open_app", name="notepad"), C("done", summary="k")],
               desk, task="buy a mug on shop.example.com", mode="background", browser_ex=b,
               observe_tab=tab.observe, invisible=uia)
        r["b"], r["desk"], r["uia"] = b.calls, desk.calls, uia.calls
        return r
    a, b = both(make)
    assert b["res"] == ("done", "k")
    assert [x["method"] for x in acts(b["nodes"])] == ["browser", "browser", "browser", "browser", "mouse", "none"]
    assert b["b"] == [("open_url", "https://shop.example.com"), ("click", 15, 5), ("type_text", "mug"),
                      ("hold", 25, 5, 2.0)]
    assert b["desk"] == [("open_app", ("notepad",))] and b["uia"] == []             # the real PC: only open_app
    ev = b["events"]
    check_events(ev)
    assert [e["method"] for e in of(ev, "action")][:4] == ["browser"] * 4
    assert b["summ"]["result"] == "done"


def test_loop_background_hitl_handover_fronts_the_tab():
    from test_human_loop import Blocker

    def make(e):
        desk, tab = Screen(), Tab(live={(15, 5)}, blocker=Blocker("login", "sign in", True, "Shop - tab"))
        b = FakeBrowser(tab)
        loop = setup(tab)[0]
        r = go(e, [C("click", id=1), C("done", summary="k")], desk, loop=loop, task="log in on shop.example.com",
               mode="background", browser_ex=b, observe_tab=tab.observe)
        r["b"], r["loop"] = b.calls, loop
        return r
    a, b = both(make)
    assert b["res"] == ("done", "k")
    assert b["b"][:2] == [("front",), ("back",)] and ("click", 15, 5) in b["b"]
    assert "handover" not in vars(b["loop"])                                      # unwrapped again


@pytest.mark.parametrize("hitl", [False, True])
def test_loop_mouse_denied_stops_the_task(hitl):
    """MouseDenied comes only after the gate waited (300 s) or the user pressed Stop on its card: task "stopped"."""
    def make(e):
        s = Screen(live={(15, 5), (25, 5)})
        ex = DenyEx(s, {"click": (2,)})
        loop = setup(s, ex=ex)[0] if hitl else None
        return go(e, [C("click", id=1), C("click", id=2), C("done", summary="k")], s, ex, loop)
    a, b = both(make)
    assert b["res"] == ("stopped", "you needed the mouse, so I stopped (the user said Not now)")
    assert b["exc"] is None and b["summ"]["result"] == "stopped"
    assert [(x["step"], x["ok"]) for x in acts(b["nodes"])] == [(1, True)]          # no retry, no model failure
    check_events(b["events"])
    assert b["events"][-1]["result"] == "stopped" and of(b["events"], "say")[-1]["text"] == "Stopped"
    assert agent.end_result(MouseDenied()) == ("stopped", "you needed the mouse, so I stopped")


def test_gate_new_task_and_one_burst_per_mouse_step():
    import contextlib

    class Gate:
        def __init__(self):
            self.n = 0

        def new_task(self):
            self.n += 1

    class StepEx(FakeEx):
        def __init__(self, screen):
            super().__init__(screen)
            self.gate, self.bursts = Gate(), []

        @contextlib.contextmanager
        def step(self):
            self.bursts.append("in")
            yield
            self.bursts.append("out")

    s = Screen(live={(15, 5)})
    ex = StepEx(s)
    r = router.Router(ex, invisible=FakeUIA(s, results=["ok", None]), observe=s.observe)
    assert ex.gate.n == 1                                                          # new task for the gate
    o, marks = obs_of(s)
    r.execute("click", {"id": 1}, marks, o)                                        # invisible: no burst
    assert ex.bursts == []
    r.execute("click", {"id": 1}, marks, o)                                        # uia can't -> real mouse
    assert ex.bursts == ["in", "out"] and r.method == "mouse"
    router.Router(FakeEx(s), observe=s.observe)                                    # no gate attribute: fine


def test_loop_press_and_hold_without_hold_support():
    class NoHold(FakeEx):
        def __getattr__(self, name):
            if name == "hold":
                raise AttributeError(name)
            return super().__getattr__(name)

    def make(e):
        s = Screen(live={(15, 5)})
        return go(e, [C("press_and_hold", id=1, seconds=3), C("done", summary="k")], s, NoHold(s))
    a, b = both(make)
    assert acts(b["nodes"])[0]["fail_reason"] == "press and hold is not available with this executor"


def test_nodes_are_json_and_methods_known(tmp_path):
    s = Screen(live={(15, 5)})
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    r = go("graph", [C("click", id=1), C("list_files", folder=str(tmp_path)), C("done", summary="k")], s,
           invisible=FakeUIA(s), allowed_folders=[str(tmp_path)])
    assert [n["method"] for n in acts(r["nodes"])] == ["uia", "command", "none"]
    assert all(n["method"] in router.METHODS for n in acts(r["nodes"]))
    assert clean(r["nodes"]) == json.loads(json.dumps(clean(r["nodes"])))
