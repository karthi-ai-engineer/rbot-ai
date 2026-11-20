"""Buddy Hub tests (offscreen: nothing is shown, no agent runs, no real mouse / keyboard).

  python -m pytest -q tests/test_hub.py
"""
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

import pytest  # noqa: E402
from PySide6.QtCore import QPointF, QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rbot.hub import core  # noqa: E402
from rbot.hub.runner import DemoHitl, FakeRunner, TaskWorker, make_hub_human_ui  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    a.setQuitOnLastWindowClosed(False)
    return a


def wait(app, cond, t=8.0):
    end = time.time() + t
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


class FakeCursor:
    """Enough of AgentCursor for HumanUI and the dodge check."""
    def __init__(self):
        self.posts = []
        self.target = self.pos_ = QPointF(-5000, -5000)
        self.parked_clicked = None

    def post(self, *a):
        self.posts.append(a)

    def show(self):
        pass

    def hide(self):
        pass


def make_ctrl(app, tmp_path, runner, *, ui=None, cursor=None, settings=None, **kw):
    from rbot.hub.app import HubController
    s = settings or core.Settings(tmp_path / "settings.json")
    c = HubController(s, executor=NS(killed=False), cursor=cursor, ui=ui, runner=runner, engine="fake",
                      history_path=None, tray=False, hotkey=False, autostart_reader=lambda: False,
                      autostart_writer=kw.pop("autostart_writer", lambda on: True), runs_dir=tmp_path / "runs",
                      next_delay_ms=10, **kw)
    return c


def blocking_runner(gate, finish="done"):
    """A runner that waits until gate is set (or stop), emitting a couple of events first."""
    calls = []

    def run(task, executor, cursor=None, max_steps=25, *, stop=None, run_dir=None, on_event=None,
            keep_images=False, **kw):
        calls.append({"task": task, "keep_images": keep_images, "t": time.time(), "thread": threading.get_ident()})
        on_event({"type": "task_started", "task": task, "run_dir": str(run_dir), "t": time.time()})
        on_event({"type": "action", "step": 1, "level": 1, "name": "click", "desc": 'click "Go"',
                  "reason": "Press Go", "method": "mouse"})
        while not gate.is_set():
            if stop.is_set() or executor.killed:
                on_event({"type": "task_finished", "result": "stopped", "text": "stopped", "steps": 1,
                          "seconds": 0.1, "run_dir": str(run_dir)})
                return "stopped", "stopped"
            time.sleep(0.01)
        on_event({"type": "task_finished", "result": finish, "text": f"{task}: ok", "steps": 1, "seconds": 0.2,
                  "run_dir": str(run_dir)})
        return finish, "ok"
    run.calls = calls
    return run


# ---------------------------------------------------------------- model / event -> UI mapping
def test_event_to_ui_mapping(app, tmp_path):
    gate = threading.Event()
    c = make_ctrl(app, tmp_path, blocking_runner(gate))
    c.submit("Download the November report")
    assert wait(app, lambda: c.model.current and c.model.current.step_text == "Press Go")
    w = c.win
    for ev in ({"type": "thinking", "step": 3, "level": 2, "model": "m", "thinking": ""},
               {"type": "action", "step": 3, "level": 2, "name": "click", "desc": 'click "November 2026"',
                "reason": "Open the November report", "method": "browser"},
               {"type": "step_result", "step": 3, "level": 2, "ok": True, "desc": "", "detail": "", "latency_s": 1,
                "diff": 0.1}):
        c.on_event(ev)
    assert w.running.isVisibleTo(w) and not w.idle.isVisibleTo(w)
    assert w.running.counter.text() == "Step 3 of 25"
    assert w.running.step.text() == "Open the November report"
    assert w.running.detail.full == 'click "November 2026"'
    assert w.running.method.text == "Browser" and w.running.level.text == "L2"
    assert w.title.status.text == "Working"
    assert not w.needs.isVisibleTo(w)

    c.on_event({"type": "needs_you", "kind": "login", "title": "Please sign in", "detail": "Sign in please"})
    assert w.needs.isVisibleTo(w) and w.needs.title.text() == "Please sign in"
    assert w.needs.icon.icon == "key" and w.needs.done_btn.isVisibleTo(w)
    assert w.title.status.text == "Needs you" and w.running.state.text() == "Waiting for you"
    assert c.model.chat[-1] == ("buddy", "I need you: Please sign in")
    c.on_event({"type": "human_done", "kind": "login", "outcome": "done", "note": ""})
    assert not w.needs.isVisibleTo(w)

    # runner says "consent", the HumanUI mirror adds the real question + options -> merged card
    c.on_event({"type": "needs_you", "kind": "consent", "title": "Your choice", "detail": "Cookies?"})
    c.on_event({"type": "needs_you", "kind": "ask", "title": "Cookies?", "options": ["Reject all", "Accept all"],
                "allow_text": True, "source": "ui"})
    assert c.model.needs["kind"] == "consent" and c.model.needs["options"] == ["Reject all", "Accept all"]
    assert not w.needs.done_btn.isVisibleTo(w) and w.needs.hint.isVisibleTo(w)
    assert c.model.chat[-1] == ("buddy", "I need you: Cookies?")
    assert "answer" in w.composer.input.placeholderText().lower()
    c.on_event({"type": "needs_clear", "source": "ui"})
    assert c.model.needs is None and not w.needs.isVisibleTo(w)

    gate.set()
    assert wait(app, lambda: len(c.model.done) == 1)
    t = c.model.done[0]
    assert t.result == "done" and t.steps == 1 and t.text == "Download the November report"
    assert w.done_list.isVisibleTo(w) and w.done_head.count.text() == "1"
    assert wait(app, lambda: w.title.status.text == "Idle")


def test_model_results_and_status_line():
    m = core.HubModel()
    assert "Nothing right now" in m.status_line()
    how, t = m.submit("a")
    assert how == "start" and m.current is t
    assert m.submit("b")[0] == "queued"
    m.apply({"type": "action", "step": 2, "level": 1, "reason": "Open it", "desc": "click", "method": "mouse"})
    assert "step 2 of 25: Open it" in m.status_line() and "1 more" in m.status_line()
    m.apply({"type": "task_finished", "result": "fail", "text": "step 2: every level failed (x)", "steps": 2,
             "seconds": 3})
    d = m.done[0]
    assert d.tone == "bad" and d.result_label == "Couldn't finish" and "stuck" in d.summary
    assert m.current is None and m.next_task().text == "b"
    m.apply({"type": "task_finished", "result": "killed", "text": "KillSwitch: ", "steps": 0})
    assert m.done[0].tone == "neutral" and "emergency stop" in m.done[0].summary


def test_intents():
    assert core.intent("What are you doing?") == "status"
    assert core.intent("whats happening") == "status"
    assert core.intent("status") == "status"
    assert core.intent("Stop!") == "stop"
    assert core.intent("stop the music in spotify") == "task"
    assert core.intent("open chatgpt.com and ask for a joke") == "task"


def test_chat_status_and_answer_routing(app, tmp_path):
    gate = threading.Event()
    c = make_ctrl(app, tmp_path, blocking_runner(gate))
    answers = []
    c.hitl.answer = answers.append
    c.submit("what are you doing?")
    assert c.model.chat[-1][1].startswith("Nothing right now") and c.model.current is None
    c.submit("Do the thing")
    assert wait(app, lambda: c.model.current is not None and c.model.current.step_text == "Press Go")
    c.submit("what are you doing?")
    assert 'Working on "Do the thing"' in c.model.chat[-1][1]
    c.on_event({"type": "needs_you", "kind": "ask", "title": "Which folder?", "allow_text": True, "source": "ui"})
    c.submit("Reports please")                              # typed while a question is open = the answer
    assert answers == ["Reports please"] and not c.model.queue
    c.on_event({"type": "needs_clear", "source": "ui"})
    c.submit("stop")
    assert wait(app, lambda: c.model.current is None)
    assert c.model.done[0].result == "stopped"


# ---------------------------------------------------------------- queue
def test_queue_one_at_a_time(app, tmp_path):
    gate = threading.Event()
    run = blocking_runner(gate)
    c = make_ctrl(app, tmp_path, run)
    c.submit("first")
    c.submit("second")
    c.submit("third")
    assert wait(app, lambda: len(run.calls) == 1)
    time.sleep(0.15)
    app.processEvents()
    assert len(run.calls) == 1, "only one task may run at a time"
    assert [t.text for t in c.model.queue] == ["second", "third"]
    assert c.win.queue.isVisibleTo(c.win) and c.win.queue_head.count.text() == "2"
    c.remove_queued(c.model.queue[1].id)                     # the remove button
    assert [t.text for t in c.model.queue] == ["second"]
    gate.set()                                               # first finishes -> second starts after task_finished
    assert wait(app, lambda: len(c.model.done) == 2)
    assert [x["task"] for x in run.calls] == ["first", "second"]
    assert run.calls[1]["t"] >= run.calls[0]["t"]
    assert [t.text for t in c.model.done] == ["second", "first"]
    assert c.model.current is None and not c.model.queue
    assert not c.win.queue.isVisibleTo(c.win)


def test_next_task_waits_for_task_finished(app, tmp_path):
    """The next task must not start on any other event, only after task_finished arrived."""
    gate = threading.Event()
    run = blocking_runner(gate)
    c = make_ctrl(app, tmp_path, run)
    c.submit("one")
    c.submit("two")
    assert wait(app, lambda: len(run.calls) == 1)
    c.on_event({"type": "step_result", "step": 1, "level": 1, "ok": False, "desc": "", "detail": ""})
    c.on_event({"type": "human_done", "kind": "login", "outcome": "done", "note": ""})
    time.sleep(0.1)
    app.processEvents()
    assert len(run.calls) == 1 and c.model.current.text == "one"
    gate.set()
    assert wait(app, lambda: len(run.calls) == 2)


# ---------------------------------------------------------------- threads
def test_worker_events_are_marshalled_to_qt_thread(app, tmp_path):
    seen = []
    c = make_ctrl(app, tmp_path, FakeRunner([("sleep", 0.01), ("finish", "done", "ok")]))
    orig = c.model.apply

    def spy(ev):
        seen.append((ev["type"], QThread.currentThread() is app.thread()))
        return orig(ev)
    c.model.apply = spy
    c.submit("x")
    assert wait(app, lambda: c.model.done)
    assert seen and all(main for _, main in seen), seen
    assert [t for t, _ in seen].count("task_finished") == 1      # the runner's own one is held and re-sent once


def test_hitl_mirror_and_hub_buttons(app, tmp_path):
    """Real HumanUI (offscreen): the worker thread hands over / asks; the Hub mirrors it and its buttons
    (I'm done, an option) resolve the very same HumanUI request."""
    cur = FakeCursor()
    ui = make_hub_human_ui(cur)
    script = [("handover", "login", "Please sign in", "Sign in and I'll carry on"),
              ("ask", "consent", "Cookies?", ["Reject all", "Accept all"]),
              ("finish", "done", "ok")]
    fr = FakeRunner(script, speed=1, ui_timeout=20)
    c = make_ctrl(app, tmp_path, fr, ui=ui, cursor=cur, human_loop_cls=DemoHitl)
    c.submit("shop")
    assert wait(app, lambda: c.model.needs and c.model.needs["kind"] == "login" and c.model.needs["handover"])
    assert ui.chip.isVisible()
    assert c.win.needs.done_btn.isVisibleTo(c.win)
    c.win.needs.done_btn.click()                              # Hub [I'm done] -> HumanUI done (Qt thread)
    assert wait(app, lambda: c.model.needs and c.model.needs["kind"] == "consent" and c.model.needs["options"])
    assert ui.popup.isVisible()
    c.win.needs.answer.emit("Reject all")                     # Hub option button
    assert wait(app, lambda: c.model.done)
    assert fr.answers == ["Reject all"]
    assert c.model.done[0].result == "done" and c.model.needs is None


def test_hub_stop_goes_through_humanui(app, tmp_path):
    cur = FakeCursor()
    ui = make_hub_human_ui(cur)
    fr = FakeRunner([("handover", "captcha", "Robot check", "Please solve it"), ("finish", "done", "ok")],
                    ui_timeout=20)
    c = make_ctrl(app, tmp_path, fr, ui=ui, cursor=cur, human_loop_cls=DemoHitl)
    c.submit("x")
    assert wait(app, lambda: c.model.needs is not None)
    c.win.needs.stop_btn.click()
    assert ui.stop_clicked.is_set() and c.worker.stop_event.is_set() and c.ex.killed
    assert wait(app, lambda: c.model.done)
    assert c.model.done[0].result in ("stopped", "killed")


# ---------------------------------------------------------------- keep_images / legacy runner
def test_keep_images_passthrough(app, tmp_path):
    s = core.Settings(tmp_path / "s.json")
    fr = FakeRunner([("finish", "done", "ok")])
    c = make_ctrl(app, tmp_path, fr, settings=s)
    c.submit("a")
    assert wait(app, lambda: len(c.model.done) == 1)
    assert fr.calls[-1]["keep_images"] is False                # default OFF
    s.set("keep_screenshots", True)
    c.submit("b")
    assert wait(app, lambda: len(c.model.done) == 2)
    assert fr.calls[-1]["keep_images"] is True


def test_legacy_runner_synthesised_events(app, tmp_path):
    """agent.run_task shape (no on_event, no keep_images): task_started/finished are synthesised, steps.jsonl
    becomes action/step_result, the cursor's speech becomes the live step, screenshots are deleted."""
    def legacy(task, executor, cursor=None, max_steps=25, *, stop=None, run_dir=None, guard=None, hitl=None):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True)
        (run_dir / "step_01.jpg").write_bytes(b"x")
        cursor.post("say", "Open the Reports page", False)
        with open(run_dir / "steps.jsonl", "w", encoding="utf-8") as f:
            f.write('{"step": 1, "level": 2, "action": "click", "args": {"id": 7}, "reason": "Open Reports", '
                    '"ok": true, "fail_reason": "", "latency_s": 1.0, "diff": 0.2}\n')
        time.sleep(0.6)
        (run_dir / "summary.json").write_text('{"steps": 1, "total_time_s": 0.7}')
        return "done", "all good"

    w = TaskWorker(NS(killed=False), FakeCursor(), None, legacy, "legacy", runs_dir=tmp_path)
    assert not w.native_events and not w.takes_keep
    evs = []
    w.bridge.event.connect(evs.append)
    w.start("t", keep_images=False)
    assert wait(app, lambda: any(e["type"] == "task_finished" for e in evs))
    types = [e["type"] for e in evs]
    assert types[0] == "task_started" and types[-1] == "task_finished"
    assert "say" in types and "action" in types and "step_result" in types
    act = next(e for e in evs if e["type"] == "action")
    assert act["method"] == "mouse" and act["level"] == 2 and act["reason"] == "Open Reports"
    fin = evs[-1]
    assert fin["result"] == "done" and fin["steps"] == 1
    assert not list(Path(fin["run_dir"]).glob("*.jpg"))


def test_graph_runner_signature_detected():
    from rbot.core import agent_graph
    w = TaskWorker(NS(killed=False), None, None, agent_graph.get_runner(), "graph")
    assert w.native_events and w.takes_keep and w.takes_hitl


# ---------------------------------------------------------------- settings
def test_settings_persistence(tmp_path):
    p = tmp_path / "settings.json"
    s = core.Settings(p)
    assert s["record_video"] is True and s["keep_screenshots"] is False and s["mode"] == "live"
    s.set("skin", "mint")
    s.set("teams_allowed", ["Tharun K"])
    s.set("idle_seconds", 10)
    s2 = core.Settings(p)
    assert s2["skin"] == "mint" and s2["teams_allowed"] == ["Tharun K"] and s2["idle_seconds"] == 10
    p.write_text('{"skin": "neon", "theme": 5, "mode": "x", "bogus": 1}')
    s3 = core.Settings(p)
    assert s3["skin"] == "grape" and s3["theme"] == "dark" and s3["mode"] == "live"
    with pytest.raises(KeyError):
        s3.set("bogus", 1)


def test_settings_drawer_saves(app, tmp_path):
    from rbot.hub.widgets import CTX
    s = core.Settings(tmp_path / "s.json")
    calls = []
    c = make_ctrl(app, tmp_path, FakeRunner([]), settings=s, autostart_writer=lambda on: calls.append(on) or True)
    c.win.drawer.autostart.emit(True)
    assert calls == [True] and c.autostart_on
    c.win.setting.emit("keep_screenshots", True)
    c.win.setting.emit("theme", "light")
    assert core.Settings(tmp_path / "s.json")["keep_screenshots"] is True
    assert core.Settings(tmp_path / "s.json")["theme"] == "light" and CTX.t["dark"] is False
    c.win.setting.emit("theme", "dark")
    assert CTX.t["dark"] is True


def test_autostart_registry_only_on_toggle():
    class Reg:
        HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 3, 4

        def __init__(self):
            self.vals, self.ops = {}, []

        def OpenKey(self, *a):
            reg = self

            class K:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            reg.ops.append(("open", a[1]))
            return K()

        def QueryValueEx(self, k, name):
            if name not in self.vals:
                raise FileNotFoundError(name)
            return self.vals[name], 1

        def SetValueEx(self, k, name, _r, _t, v):
            self.vals[name] = v

        def DeleteValue(self, k, name):
            if name not in self.vals:
                raise FileNotFoundError(name)
            del self.vals[name]

    r = Reg()
    assert core.autostart_enabled(r) is False
    assert core.set_autostart(True, r) and core.autostart_enabled(r)
    assert "run_module('rbot'" in r.vals[core.RUN_VALUE] and r.ops[-1][1] == core.RUN_KEY
    assert core.set_autostart(False, r) and not core.autostart_enabled(r)
    assert core.set_autostart(False, r)                       # removing twice is fine


# ---------------------------------------------------------------- mini-bar
def test_minibar_collapse_and_restore(app, tmp_path):
    gate = threading.Event()
    cur = FakeCursor()
    c = make_ctrl(app, tmp_path, blocking_runner(gate), cursor=cur)
    c.win.show()
    c.submit("long task")
    assert c.collapsed
    assert wait(app, lambda: not c.win.isVisible() and c.mini.isVisible())
    assert wait(app, lambda: "Press Go" in c.mini.text.full)
    assert c.mini.counter.text() == "1/25"
    # handover: the your-turn chip takes the top-centre spot, the mini-bar steps aside
    c.on_event({"type": "needs_you", "kind": "login", "title": "Please sign in", "handover": True, "source": "ui"})
    assert wait(app, lambda: not c.mini.isVisible())
    c.on_event({"type": "needs_clear", "source": "ui"})
    assert wait(app, lambda: c.mini.isVisible())
    # question (no handover): mini-bar stays and turns amber
    c.on_event({"type": "needs_you", "kind": "ask", "title": "Cookies?", "options": ["Reject"], "source": "ui"})
    assert c.mini.isVisible() and c.mini.dot.state == "needs" and c.mini.text.full.startswith("Needs you")
    c.on_event({"type": "needs_clear", "source": "ui"})
    # hotkey / tray / expand: restore mid-run, then hide again -> back to the mini-bar
    c.toggle()
    assert wait(app, lambda: c.win.isVisible() and not c.mini.isVisible()) and not c.collapsed
    c.toggle()
    assert wait(app, lambda: not c.win.isVisible() and c.mini.isVisible())
    # the buddy flies to where the mini-bar is -> it gets out of the way at once
    g = c.mini.geometry()
    cur.target = QPointF(g.center().x(), g.center().y())
    assert wait(app, lambda: not c.mini.isVisible(), 2)
    cur.target = cur.pos_ = QPointF(-5000, -5000)
    c.dodge_until = 0
    assert wait(app, lambda: c.mini.isVisible(), 3)
    gate.set()                                                # task ends -> full Hub is back
    assert wait(app, lambda: c.win.isVisible() and not c.mini.isVisible() and not c.collapsed)


def test_capture_exclusion_called_for_both_windows(app, tmp_path, monkeypatch):
    from rbot.hub import app as hub_app
    got = []
    monkeypatch.setattr(hub_app, "exclude_from_capture", lambda w: got.append(type(w).__name__))
    make_ctrl(app, tmp_path, FakeRunner([]))
    assert "HubWindow" in got and "MiniBar" in got


def test_selftest_passes(app):
    from rbot.hub import app as hub_app
    assert hub_app.selftest() == 0


def test_rescale_when_hub_moves_to_another_dpi(app, tmp_path, monkeypatch):
    """Mixed 125 % / 100 % monitors: Qt does no scaling (QT_ENABLE_HIGHDPI_SCALING=0, physical pixels), so the
    Hub rebuilds itself for the scale of the monitor it lands on; the mini-bar uses the primary's scale."""
    from rbot.hub import app as hub_app
    from rbot.hub.widgets import CTX
    c = make_ctrl(app, tmp_path, FakeRunner([]))
    c.win.show()
    monkeypatch.setattr(hub_app, "screen_scale", lambda scr: 1.25)
    c._use_scale("hub", None)
    w125, f125 = c.win.width(), c.win.running.title.font().pixelSize()
    assert CTX.scale == 1.25 and f125 == 19                    # 15 px heading * 1.25
    monkeypatch.setattr(hub_app, "screen_scale", lambda scr: 1.0)
    c._use_scale("hub", None)
    assert c.win.running.title.font().pixelSize() == 15
    assert abs(c.win.width() - w125 / 1.25) <= 2
    c._use_scale("mini", None)
    assert c.mini.text.font().pixelSize() == 13 and c.mini_scale == 1.0
