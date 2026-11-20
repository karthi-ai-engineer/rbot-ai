"""Offline tests for agent_graph.py (the LangGraph agent loop).

  python -m pytest -q tests/test_agent_graph.py

1. parity: extra loop paths (kill switch, stop event, loop detector, soft streak, max_steps, adaptive start level,
   model errors, guard veto, executor errors, unexpected exceptions) run on BOTH engines and must produce the same
   result, the same steps.jsonl nodes, the same prompts and the same summary (timings aside).
   The HITL paths are covered on both engines by test_human_loop.py / test_refine_hover.py (engine fixture).
2. the Hub event contract, 3. screenshot cleanup, 4. JSON-serialisable state / checkpoints, 5. engine switch.
Same fakes as test_human_loop.py: nothing here moves the mouse, presses keys or opens windows.
"""
import json
import tempfile
import threading
from pathlib import Path

import pytest

from PIL import Image  # noqa: E402

from rbot.desktop import actions  # noqa: E402
from rbot.core import agent  # noqa: E402
from rbot.core import agent_graph  # noqa: E402
import test_human_loop as H  # noqa: E402
from test_human_loop import Blocker, C, FakeEx, Screen, model, msg, setup  # noqa: E402

ENGINES = {"legacy": agent.run_task, "graph": agent_graph.run_task}
VOLATILE = {"latency_s", "waited_s"}


# ============================================================================ helpers
class KillEx(FakeEx):
    """FakeEx whose n-th call of a method hits the kill switch (Ctrl+Alt+Q)."""
    def __init__(self, screen, kill_on=None, boom_on=None, raise_on=None):
        super().__init__(screen, raise_on)
        self.kill_on, self.boom_on = dict(kill_on or {}), dict(boom_on or {})

    def _do(self, name, *a):
        k = self.n.get(name, 0) + 1
        if self.kill_on.get(name) == k:
            self.n[name] = k
            raise actions.KillSwitch()
        if self.boom_on.get(name) == k:
            self.n[name] = k
            raise OSError("SendInput failed")
        super()._do(name, *a)


def go(engine, script, screen, ex=None, loop=None, max_steps=10, levels=(("m", False), ("m", True)), **kw):
    """One run -> dict(res, nodes, summ, seen, d, events, exc)."""
    d = Path(tempfile.mkdtemp())
    llm = kw.pop("llm", None) or model(list(script))
    ex = ex or FakeEx(screen)
    events = []
    if engine == "graph":
        kw.setdefault("on_event", events.append)
    out = {"d": d, "events": events, "exc": None, "seen": getattr(llm, "seen", [])}
    try:
        out["res"] = ENGINES[engine]("test task", ex, None, max_steps, observe=kw.pop("observe", screen.observe),
                                     diff_ratio=screen.diff, llm=llm, levels=list(levels), run_dir=d, settle=False,
                                     hitl=loop, keep_images=kw.pop("keep_images", True), **kw)
    except Exception as e:  # noqa: BLE001
        out["exc"], out["res"] = e, None
    f = d / "steps.jsonl"
    out["nodes"] = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
    out["summ"] = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    return out


def clean(x):
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items() if k not in VOLATILE}
    if isinstance(x, list):
        return [clean(v) for v in x]
    return x


def both(make):
    """make(engine) -> go(...) result; asserts both engines behave the same. -> (legacy, graph)."""
    a, b = make("legacy"), make("graph")
    assert a["res"] == b["res"], (a["res"], b["res"])
    assert type(a["exc"]) is type(b["exc"]) and str(a["exc"]) == str(b["exc"]), (a["exc"], b["exc"])
    assert clean(a["nodes"]) == clean(b["nodes"]), (a["nodes"], b["nodes"])
    assert [s[:3] for s in a["seen"]] == [s[:3] for s in b["seen"]]
    sa = {k: v for k, v in clean(a["summ"]).items() if k not in ("total_time_s", "engine")}
    sb = {k: v for k, v in clean(b["summ"]).items() if k not in ("total_time_s", "engine")}
    assert sa == sb, (sa, sb)
    assert b["summ"]["engine"] == "graph" and "engine" not in a["summ"]
    return a, b


def check_events(events):
    """The Hub contract: types/fields, task_started first, task_finished last and once, needs_you/human_done pairs."""
    assert events and events[0]["type"] == "task_started" and events[-1]["type"] == "task_finished", events
    assert [e["type"] for e in events].count("task_finished") == 1
    assert [e["type"] for e in events].count("task_started") == 1
    fields = {"task_started": {"task": str, "run_dir": str, "t": float},
              "thinking": {"step": int, "level": int, "model": str, "thinking": bool},
              "action": {"step": int, "level": int, "name": str, "desc": str, "reason": str, "method": str},
              "step_result": {"step": int, "level": int, "ok": bool, "desc": str, "detail": str, "latency_s": float,
                              "method": str},
              "needs_you": {"kind": str, "title": str, "detail": str},
              "human_done": {"kind": str, "outcome": str, "note": str},
              "say": {"text": str},
              "task_finished": {"result": str, "text": str, "steps": int, "seconds": float, "run_dir": str}}
    open_kind = None
    for e in events:
        spec = fields[e["type"]]
        assert set(e) == set(spec) | {"type"} | ({"diff"} if e["type"] == "step_result" else set()), e
        for k, t in spec.items():
            assert isinstance(e[k], t), (k, e)
        if e["type"] == "step_result":
            assert e["diff"] is None or isinstance(e["diff"], float)
        if e["type"] in ("action", "step_result"):
            assert e["method"] in ("mouse", "browser", "uia", "command", "none")
        if e["type"] == "needs_you":
            assert open_kind is None, ("needs_you while another is open", events)
            assert e["kind"] in ("login", "otp", "captcha", "payment", "consent", "stuck", "takeover", "ask")
            assert e["title"]
            open_kind = e["kind"]
        if e["type"] == "human_done":
            assert open_kind == e["kind"], ("human_done without its needs_you", events)
            open_kind = None
    assert open_kind is None
    assert events[-1]["result"] in ("done", "fail", "max_steps", "stopped", "killed", "error")
    return [e["type"] for e in events]


def of(events, kind):
    return [e for e in events if e["type"] == kind]


# ============================================================================ 1. parity on more loop paths
@pytest.mark.parametrize("hitl", [False, True])
def test_parity_kill_switch(hitl):
    def make(e):
        s = Screen(live={(15, 5)})
        ex = KillEx(s, kill_on={"click": 2})
        loop = setup(s, ex=ex)[0] if hitl else None
        return go(e, [C("click", id=1), C("click", id=2), C("done", summary="k")], s, ex, loop)
    a, b = both(make)
    assert b["res"][0] == "killed" and b["res"][1].startswith("KillSwitch")
    check_events(b["events"])
    assert b["events"][-1]["result"] == "killed" and of(b["events"], "say")[-1]["text"] == "Stopped"


@pytest.mark.parametrize("hitl", [False, True])
def test_parity_stop_event(hitl):
    def make(e):
        s = Screen(live={(15, 5), (25, 5)})
        stop = threading.Event()
        script = [C("click", id=1), C("click", id=2), C("done", summary="k")]
        llm = model(script)
        inner = llm

        def stopping(m, messages, thinking):
            r = inner(m, messages, thinking)
            if len(inner.seen) == 2:
                stop.set()                                 # Stop pressed while the model was thinking
            return r
        stopping.seen = inner.seen
        loop = setup(s)[0] if hitl else None
        return go(e, [], s, loop=loop, llm=stopping, stop=stop)
    a, b = both(make)
    assert b["res"][0] == "stopped" and len(b["nodes"]) == 2
    check_events(b["events"])


@pytest.mark.parametrize("hitl,answers", [(False, ()), (True, ["Stop"]), (True, ["I'll do it"])])
def test_parity_loop_detector(hitl, answers):
    def make(e):
        s = Screen(live={(15, 5), (25, 5)})
        script = [C("click", id=1), C("click", id=2), C("click", id=1), C("click", id=2), C("click", id=1),
                  C("click", id=5), C("done", summary="k"), C("done", summary="k")]
        loop = setup(s, answers=answers)[0] if hitl else None
        return go(e, script, s, loop=loop)
    a, b = both(make)
    circ = [n for n in b["nodes"] if n.get("fail_reason", "").startswith("going in circles")]
    assert circ and circ[0]["step"] == 5 and circ[0]["level"] == 1
    if hitl:                                               # a loop asks the human right away (no L2 attempt)
        assert not [n for n in b["nodes"] if n.get("step") == 5 and n.get("level") == 2]
        assert of(b["events"], "needs_you")[0]["kind"] == "stuck"
    check_events(b["events"])


@pytest.mark.parametrize("hitl", [False, True])
def test_parity_soft_streak(hitl):
    def make(e):
        s = Screen()                                       # nothing is live: clicks never change the screen
        script = [C("click", id=1), C("click", id=2), C("click", id=3), C("click", id=4),
                  C("click", id=5), C("click", id=6), C("done", summary="k")]
        loop = setup(s, answers=["Stop"])[0] if hitl else None
        return go(e, script, s, loop=loop, max_steps=3)
    a, b = both(make)
    if hitl:   # first soft step: continue; second in a row: ask the human (stuck) -> Stop
        assert b["res"][0] == "stopped" and "stuck" in b["res"][1]
        assert [e["kind"] for e in of(b["events"], "needs_you")] == ["stuck"]
    else:      # no human: soft steps just continue until max_steps
        assert b["res"] == ("max_steps", "stopped after 3 steps")
    assert b["summ"]["attempts_per_level"]["L1"]["attempts"] >= 2
    check_events(b["events"])


def test_parity_max_steps_and_adaptive_start():
    def make(e):
        s = Screen(live={(i * 10 + 5, 5) for i in range(10)})
        script = [C("click", id=41), C("click", id=1),     # steps 1-2: L1 picks a box that does not exist,
                  C("click", id=42), C("click", id=2),     # L2 gets it right
                  C("click", id=3), C("click", id=4)]      # step 3 starts at L2, step 4 back at L1
        return go(e, script, s, max_steps=4)
    a, b = both(make)
    assert b["res"] == ("max_steps", "stopped after 4 steps")
    lv = [(n["step"], n["level"]) for n in b["nodes"]]
    assert lv == [(1, 1), (1, 2), (2, 1), (2, 2), (3, 2), (4, 1)], lv   # L2 needed twice -> start at L2
    check_events(b["events"])


def test_parity_model_error_guard_executor_error_top_fail():
    def guard(name, args, obs, marks):
        if name == "press_key":
            raise agent.BadAction("BLOCKED: no keys today")

    def make(e):
        s = Screen(live={(15, 5)})
        ex = KillEx(s, boom_on={"type_text": 1})
        script = [RuntimeError("gateway 502"), C("click", id=1),            # step 1: L1 error, L2 ok
                  C("press_key", key="enter"), C("type_text", text="hi")]   # step 2: guard veto, executor error
        return go(e, script, s, ex, guard=guard)

    def make_fail(e):                                      # "fail": retried by L2, accepted at the top level
        return go(e, [C("fail", reason="cannot"), C("fail", reason="really cannot")], Screen())
    a, b = both(make)
    assert b["res"] == ("fail", "step 2: every level failed (executor error: SendInput failed)"), b["res"]
    a2, b2 = both(make_fail)
    assert b2["res"] == ("fail", "really cannot")
    assert b2["nodes"][0]["fail_reason"] == "gave up (cannot); a stronger level will retry"
    fr = [n["fail_reason"] for n in b["nodes"]]
    assert fr[0].startswith("model call error: gateway 502") and fr[2] == "BLOCKED: no keys today"
    assert fr[3] == "executor error: SendInput failed"
    check_events(b["events"])


def test_parity_unexpected_exception_is_raised():
    def make(e):
        s = Screen(live={(15, 5)})
        n = {"obs": 0}

        def observe():
            n["obs"] += 1
            if n["obs"] == 3:
                raise RuntimeError("screen capture broke")
            return s.observe()
        return go(e, [C("click", id=1), C("click", id=1), C("done", summary="k")], s, observe=observe)
    a, b = both(make)
    assert isinstance(b["exc"], RuntimeError) and b["summ"]["result"] == "error"
    assert b["summ"]["message"] == "RuntimeError: screen capture broke"
    check_events(b["events"])
    assert b["events"][-1]["result"] == "error"


def test_parity_takeover_without_hitl_is_executor_error():
    def make(e):
        s = Screen(live={(15, 5)})
        return go(e, [C("click", id=1), C("click", id=1), C("done", summary="k")], s,
                  FakeEx(s, raise_on={"click": 1}))
    a, b = both(make)
    assert b["nodes"][0]["fail_reason"] == "executor error: "


# ============================================================================ 2. events
def test_events_simple_run():
    s = Screen(live={(15, 5)})
    r = go("graph", [C("click", id=1), C("done", summary="all good")], s)
    types = check_events(r["events"])
    assert r["res"] == ("done", "all good")
    assert types == ["task_started", "say", "thinking", "action", "say", "step_result",
                     "say", "thinking", "action", "step_result", "say", "task_finished"], types
    st, th, act, res = r["events"][0], r["events"][2], r["events"][3], r["events"][5]
    assert st["task"] == "test task" and st["run_dir"] == str(r["d"])
    assert th == {"type": "thinking", "step": 1, "level": 1, "model": "m", "thinking": False}
    assert act["name"] == "click" and act["method"] == "mouse" and act["reason"] == "r-click" and "#1" in act["desc"]
    assert res["ok"] and res["detail"] == "ok" and res["diff"] == 0.05
    assert r["events"][8]["method"] == "none"
    fin = r["events"][-1]
    assert fin["result"] == "done" and fin["text"] == "all good" and fin["steps"] == 2 and fin["run_dir"] == str(r["d"])
    assert [e["text"] for e in of(r["events"], "say")] == ["Thinking", "r-click", "Thinking", "Done!"]


def test_events_failed_attempt_and_escalation():
    s = Screen()
    r = go("graph", [C("click", id=42), C("click", id=43)], s)
    check_events(r["events"])
    sr = of(r["events"], "step_result")
    assert [(e["level"], e["ok"]) for e in sr] == [(1, False), (2, False)]
    assert sr[0]["detail"].startswith("unknown mark id 42") and sr[0]["diff"] is None
    assert [e["thinking"] for e in of(r["events"], "thinking")] == [False, True]
    assert [e["level"] for e in of(r["events"], "action")] == [1, 2]   # unknown id: found when executing
    assert r["events"][-1]["result"] == "fail"


def test_events_hitl_pairs():
    # blocker handover
    s = Screen(live={(15, 5)}, blocker=Blocker("login", "sign in + password field", True, "Sign in - Chrome"))
    loop, ui, *_ = setup(s)
    r = go("graph", [C("click", id=1), C("done", summary="ok")], s, loop=loop)
    check_events(r["events"])
    ny, hd = of(r["events"], "needs_you"), of(r["events"], "human_done")
    assert ny == [{"type": "needs_you", "kind": "login", "title": "Please sign in",
                   "detail": "This page needs your login. Sign in and I'll carry on by myself"}]
    assert hd[0]["kind"] == "login" and hd[0]["outcome"] == "done" and hd[0]["note"] == "the human signed in"
    assert r["events"].index(ny[0]) < r["events"].index(of(r["events"], "thinking")[0])
    assert loop.ui is ui                                   # the event proxy is removed again
    # consent question
    s = Screen(live={(170, 110)}, blocker=Blocker("consent", "cookie banner: Accept all / Reject all", False, "x"),
               buttons=["Accept all", "Reject all"])
    loop, *_ = setup(s, answers=["Reject all"])
    r = go("graph", [C("click", id=11), C("done", summary="k")], s, loop=loop)
    check_events(r["events"])
    assert of(r["events"], "needs_you")[0] == {"type": "needs_you", "kind": "consent", "title": "Your choice",
                                               "detail": "This website is asking about cookies. What should I do?"}
    assert of(r["events"], "human_done")[0] == {"type": "human_done", "kind": "consent", "outcome": "instructed",
                                                "note": "Reject all"}
    # ask_human
    s = Screen()
    loop, *_ = setup(s, answers=["Sales"])
    r = go("graph", [C("ask_human", question="Which Tharun?", options=["Sales", "IT"]), C("done", summary="k")], s,
           loop=loop)
    check_events(r["events"])
    assert of(r["events"], "needs_you")[0]["kind"] == "ask" and of(r["events"], "needs_you")[0]["detail"] == "Which Tharun?"
    assert of(r["events"], "human_done")[0] == {"type": "human_done", "kind": "ask", "outcome": "answered", "note": "Sales"}
    assert of(r["events"], "action")[0]["method"] == "none"
    # stuck -> I'll do it (one pair, although the human also gets the handover chip)
    s = Screen(live={(15, 5)})
    loop, *_ = setup(s, answers=["I'll do it"])
    r = go("graph", [C("click", id=42), C("click", id=43), C("done", summary="human did it")], s, loop=loop)
    check_events(r["events"])
    ny = of(r["events"], "needs_you")
    assert [e["kind"] for e in ny] == ["stuck"] and ny[0]["title"] == "Can you help?"
    assert ny[0]["detail"].startswith("I couldn't manage this step: r-click")
    assert of(r["events"], "human_done")[0]["outcome"] == "done"
    # takeover during an action
    s = Screen(live={(15, 5)})
    ex = FakeEx(s, raise_on={"click": 1})
    loop, *_ = setup(s, ex=ex)
    r = go("graph", [C("click", id=1), C("done", summary="k")], s, ex, loop)
    check_events(r["events"])
    assert of(r["events"], "needs_you")[0]["kind"] == "takeover"
    assert of(r["events"], "needs_you")[0]["title"] == "You're in control"
    # stuck-ask limit: the 4th stuck has no question -> no needs_you, pairs still balanced
    loop, ui, *_ = setup(Screen(), answers=["Try again"] * 5)
    r = go("graph", [C("click", id=42), C("click", id=43)] * 4, Screen(), loop=loop)
    check_events(r["events"])
    assert len(of(r["events"], "needs_you")) == 3 and r["res"][0] == "fail"


def test_events_timeout_and_payment():
    s = Screen(blocker=Blocker("login", "sign in", True, "x"))
    loop, *_ = setup(s, outcomes=[("timeout", "timeout 300s idle")], on_wait=lambda: None)
    r = go("graph", [C("done", summary="never")], s, loop=loop)
    check_events(r["events"])
    assert of(r["events"], "human_done")[0]["outcome"] == "timeout" and r["events"][-1]["result"] == "stopped"
    s = Screen(blocker=Blocker("payment", "card number + cvv", False, "x"))
    loop, *_ = setup(s, answers=["Yes, continue"], outcomes=[("done", "clicked buddy")])
    r = go("graph", [C("done", summary="k")], s, loop=loop)
    check_events(r["events"])
    ny = of(r["events"], "needs_you")
    assert len(ny) == 1 and ny[0]["kind"] == "payment" and "pay yourself" in ny[0]["detail"].lower()


def test_events_on_event_errors_never_break_the_run():
    s = Screen(live={(15, 5)})
    calls = []

    def bad(ev):
        calls.append(ev["type"])
        raise ValueError("hub crashed")
    r = go("graph", [C("click", id=1), C("done", summary="ok")], s, on_event=bad)
    assert r["res"] == ("done", "ok") and calls[0] == "task_started" and calls[-1] == "task_finished"


def test_events_dom_page_and_methods():
    # live mode: a DOM-marked page is still clicked with the real mouse (the browser method = background tab only)
    s = Screen(live={(15, 5)})
    orig = s.observe
    s.observe = lambda: {**orig(), "page_source": "dom"}
    r = go("graph", [C("click", id=1), C("wait", seconds=0), C("done", summary="ok")], s)
    check_events(r["events"])
    assert [e["method"] for e in of(r["events"], "action")] == ["mouse", "command", "none"]
    assert [e["method"] for e in of(r["events"], "step_result")] == ["mouse", "command", "none"]
    assert [n["method"] for n in r["nodes"]] == ["mouse", "command", "none"]
    m = agent_graph._method
    assert m("open_url", {}) == "browser" and m("web_search", {}) == "command" and m("click", {}) == "mouse"
    assert m("ask_human", {"page_source": "dom"}) == "none" and m("type_text", None) == "mouse"
    assert m("click", {"background": True}) == "browser" and m("list_files", {}) == "command"


def test_events_hitl_without_ui_attribute():
    """A hitl object without .ui (custom HumanLoop) still yields balanced needs_you / human_done."""
    s = Screen(live={(15, 5)}, blocker=Blocker("login", "sign in", True, "x"))
    loop, *_ = setup(s)

    class NoUI:
        def __init__(self, inner):
            self._i = inner

        def __getattr__(self, n):
            if n == "ui":
                raise AttributeError(n)
            return getattr(self._i, n)
    r = go("graph", [C("ask_human", question="Q?"), C("done", summary="k")], s, loop=NoUI(loop))
    check_events(r["events"])
    assert [e["kind"] for e in of(r["events"], "needs_you")] == ["login", "ask"]


# ============================================================================ 3. screenshot cleanup
class FileSession(H.FakeSession):
    """FakeSession that writes what hitl.HumanSession writes: human_N/session.json + frame_NNN.jpg."""
    def start(self):
        super().start()
        self.dir = Path(self.run_dir) / f"human_{len(H.FakeHitl.sessions)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(self.dir / "frame_000.jpg")
        (self.dir / "session.json").write_text("{}", encoding="utf-8")


def _cleanup_run(engine, keep_images):
    s = Screen(live={(15, 5)}, blocker=Blocker("login", "sign in", True, "x"))
    loop, ui, h, ex, _ = setup(s)
    h.HumanSession = FileSession
    d = Path(tempfile.mkdtemp())
    (d / "chat.mp4").write_bytes(b"\0")                    # a recording made next to the run
    Image.new("RGB", (8, 8)).save(d / "extra.png")
    zoom = ("click", {"x": 300, "y": 300, "target": "menu", "reason": "r"})
    script = [msg(calls=[zoom]), msg('{"x": 500, "y": 500}'), C("done", summary="ok")]
    s.live.add(agent.resolve({"x": 300, "y": 300}, {}, (1000, 500)))
    llm = model(script)
    ENGINES[engine]("test task", ex, None, 5, observe=s.observe, diff_ratio=s.diff, llm=llm,
                    levels=[("m", False), ("m", True)], run_dir=d, settle=False, hitl=loop, keep_images=keep_images)
    return d


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_cleanup_images(engine, monkeypatch):
    monkeypatch.setattr(agent, "HOVER_CHECK", False)
    d = _cleanup_run(engine, keep_images=False)
    left = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    assert left == ["chat.mp4", "human_1/session.json", "steps.jsonl", "summary.json"], left


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_keep_images(engine, monkeypatch):
    monkeypatch.setattr(agent, "HOVER_CHECK", False)
    d = _cleanup_run(engine, keep_images=True)
    names = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
    assert {"step_01.jpg", "step_01_zoom.jpg", "step_02.jpg", "human_1/frame_000.jpg", "extra.png",
            "human_1/session.json", "steps.jsonl", "summary.json", "chat.mp4"} <= names, names


def test_cleanup_also_on_error():
    s = Screen()

    def observe():
        if s.state == 0 and len(list(d.glob("step_*.jpg"))) >= 1:
            raise RuntimeError("boom")
        return s.observe()
    d = Path(tempfile.mkdtemp())
    with pytest.raises(RuntimeError):
        agent_graph.run_task("t", FakeEx(s), None, 5, observe=observe, diff_ratio=s.diff,
                             llm=model([C("click", id=1), C("click", id=2)]), levels=[("m", False), ("m", True)],
                             run_dir=d, settle=False)
    assert not list(d.glob("*.jpg")) and (d / "summary.json").exists() and (d / "steps.jsonl").exists()


# ============================================================================ 4. state is JSON
def test_state_serialisable_every_checkpoint():
    from langgraph.checkpoint.memory import MemorySaver
    cp = MemorySaver()
    s = Screen(live={(15, 5)}, blocker=Blocker("login", "sign in", True, "x"))
    loop, *_ = setup(s, answers=["Sales", "I'll do it"])
    r = go("graph", [C("ask_human", question="Which?"), C("click", id=1), C("click", id=42), C("click", id=43),
                     C("done", summary="k")], s, loop=loop, checkpointer=cp)
    assert r["res"][0] == "done"
    cps = list(cp.list(None))
    assert len(cps) > 20
    for t in cps:
        vals = {k: v for k, v in t.checkpoint["channel_values"].items() if not k.startswith(("__", "branch:"))}
        json.dumps(vals)                                   # raises on PIL images, obs dicts, messages, tuples ok
        for k in ("obs_ref", "attempt_ref", "msg_ref"):
            assert vals.get(k) is None or isinstance(vals[k], str)
    last = cps[0].checkpoint["channel_values"]
    assert last["result"] == ["done", "k"] and last["steps"] == 4 and last["humans"]
    assert set(agent_graph.AgentState.__annotations__) >= {k for k in last if not k.startswith(("__", "branch:"))}


def test_side_cache_is_pruned():
    s = Screen(live={(i * 10 + 5, 5) for i in range(10)})
    seen = {}
    orig = agent_graph.Run.prune

    def prune(self, st):
        orig(self, st)
        seen["max"] = max(seen.get("max", 0), len(self.cache))
    agent_graph.Run.prune = prune
    try:
        go("graph", [C("click", id=i) for i in range(1, 8)] + [C("done", summary="k")], s)
    finally:
        agent_graph.Run.prune = orig
    assert seen["max"] <= 3


# ============================================================================ 5. engine switch
def test_get_runner(monkeypatch):
    monkeypatch.delenv("BUDDY_ENGINE", raising=False)
    assert agent_graph.get_runner() is agent_graph.run_task
    ev = []
    r = agent_graph.get_runner(on_event=ev.append)
    assert r.func is agent_graph.run_task and r.keywords == {"on_event": ev.append}
    monkeypatch.setenv("BUDDY_ENGINE", "legacy")
    assert agent_graph.get_runner(on_event=ev.append) is agent.run_task
    monkeypatch.setenv("BUDDY_ENGINE", "graph")
    assert agent_graph.get_runner() is agent_graph.run_task


def test_recursion_limit_covers_long_runs():
    assert agent_graph.recursion_limit(25, 2) >= 25 * (6 + 9 * 2 * 5)
    s = Screen()                                           # 25 soft steps x 2 levels, no human
    r = go("graph", [C("click", id=i % 10) for i in range(60)], s, max_steps=25)
    assert r["res"] == ("max_steps", "stopped after 25 steps") and r["exc"] is None
