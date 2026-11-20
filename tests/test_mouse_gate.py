"""Offline tests for mouse_gate.py (real-mouse policy, PLAN §5) and its wiring into actions.Executor.

  python -m pytest -q tests/test_mouse_gate.py

Fake clock (virtual seconds), fake input (ActivityMonitor fed synthetic pynput-shaped events), fake question UI.
Nothing here moves the real mouse, presses keys or opens windows.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from rbot.desktop import actions  # noqa: E402
from rbot.desktop import mouse_gate as mg  # noqa: E402
from rbot.desktop.mouse_gate import ASK_TEXT, NOT_NOW, NOTICE_TEXT, YES, MouseDenied  # noqa: E402

BLOCK = object()


# ============================================================================ fakes
class FakeClock:
    """Virtual time: sleep() advances it (and runs tick hooks = the fake user)."""

    def __init__(self, t=1000.0):
        self.t = t
        self.hooks = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for h in list(self.hooks):
            h(self.t)
        time.sleep(0.0005)                         # let helper threads (the ask thread) run


class FakeUI:
    """HumanUI stand-in: ask() blocks like the real one. reply(question, n) -> answer | BLOCK."""

    def __init__(self, reply, clock=None):
        self.reply, self.clock = reply, clock
        self.asked, self.answered = [], []
        self.stop_clicked = threading.Event()
        self._open = None
        self._mu = threading.Lock()
        self._one = threading.Lock()

    def ask(self, question, options=None, allow_text=True, timeout_s=300, mood="calling"):
        self.asked.append((question, tuple(options or ()), self.clock.now() if self.clock else None))
        r = self.reply(question, len(self.asked))
        if r is not BLOCK:
            return r
        box = {"ev": threading.Event(), "answer": None}
        with self._one:                             # one question at a time, like HumanUI._ask_lock
            with self._mu:
                self._open = box
            box["ev"].wait(10)                      # safety net so a broken test can't hang
            with self._mu:
                if self._open is box:
                    self._open = None
        return box["answer"]

    def questions(self):
        return [q for q, _, _ in self.asked]

    def answer(self, text):
        self.answered.append(text)
        with self._mu:
            if self._open is not None:
                self._open["answer"] = text
                self._open["ev"].set()

    def dismiss_all(self):
        self.answer(None)

    @property
    def is_open(self):
        return self._open is not None


class FakeCursor:
    def __init__(self):
        self.posts = []

    def post(self, name, *args):
        self.posts.append((name,) + args)


def make(reply=None, idle_for=30.0, ui=True, **kw):
    """-> (gate, clock, monitor, ui). The user starts `idle_for` seconds idle."""
    clock = FakeClock()
    mon = mg.ActivityMonitor(clock, seed=clock.t - idle_for)
    fui = FakeUI(reply or (lambda q, n: None), clock) if ui else None
    kw.setdefault("poll_s", 0.1)
    kw.setdefault("ui_grace_s", 0)
    kw.setdefault("settings_path", None)
    gate = mg.MouseGate(ui=fui, cursor=kw.pop("cursor", None), monitor=mon, clock=clock,
                        lock=kw.pop("lock", mg._MouseLock()), log=lambda *a: None, **kw)
    return gate, clock, mon, fui


def busy_until(clock, mon, t_end):
    """The fake user moves the real mouse until virtual time t_end."""
    def hook(t):
        if t < t_end:
            mon.on_move(int(t) % 500, 300, False)
    clock.hooks.append(hook)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("BUDDY_IDLE_S", raising=False)
    monkeypatch.delenv("BUDDY_MOUSE_POLICY", raising=False)


# ============================================================================ idle detector (real-input proof)
def test_injected_input_is_not_activity():
    clock = FakeClock()
    mon = mg.ActivityMonitor(clock, seed=clock.t - 20)
    # our own SendInput: pynput reports injected=True on every callback -> ignored
    mon.on_move(10, 10, True)
    mon.on_click(10, 10, "left", True, True)
    mon.on_scroll(10, 10, 0, -1, True)
    mon.on_press("a", True)
    mon.on_release("a", True)
    assert mon.idle_for() == pytest.approx(20) and mon.count == 0
    # SetCursorPos from the Executor (own_move) seen as an uninjected move at exactly that spot -> ignored
    mon.own_move(400, 300)
    mon.on_move(400, 300, False)
    assert mon.count == 0
    clock.t += 1.0                                   # too late to be ours -> counts
    mon.on_move(400, 300, False)
    assert mon.count == 1 and mon.idle_for() == 0
    # real keyboard / mouse
    clock.t += 7
    mon.on_press("a", False)
    assert mon.idle_for() == 0 and mon.key_count == 1
    clock.t += 3
    mon.on_click(1, 1, "left", True, False)
    assert mon.count == 3 and mon.key_count == 1 and mon.idle_for() == 0


def test_injected_only_keeps_idle_path():
    """The agent's own input between checks must not make the user look busy."""
    gate, clock, mon, ui = make(idle_for=0.0)
    clock.hooks.append(lambda t: mon.on_move(5, 5, True))    # injected moves all the time
    clock.sleep(6.5)
    assert gate.acquire() == "idle" and ui.questions() == [NOTICE_TEXT]
    gate.release()


# ============================================================================ §5 paths
def test_idle_path_notice_then_go():
    gate, clock, mon, ui = make(idle_for=30)
    t0 = clock.t
    assert gate.acquire() == "idle"
    assert clock.t - t0 >= 3.0 - 1e-6                         # the 3 s notice ran
    assert ui.asked[0][:2] == (NOTICE_TEXT, (NOT_NOW,))
    assert gate.held()
    gate.release()
    assert not gate.held() and gate.lock.owner is None


def test_idle_path_without_ui_uses_buddy_say():
    cur = FakeCursor()
    gate, clock, mon, _ = make(idle_for=30, ui=False, cursor=cur)
    assert gate.acquire() == "idle"
    gate.release()
    says = [p[1] for p in cur.posts if p[0] == "say"]
    assert says and says[0].startswith("Using the mouse")


def test_busy_path_ask_yes():
    gate, clock, mon, ui = make(lambda q, n: YES if q == ASK_TEXT else None, idle_for=0)
    busy_until(clock, mon, 1e9)
    t0 = clock.t
    assert gate.acquire() == "yes"
    assert ui.questions() == [ASK_TEXT] and ui.asked[0][1] == (YES, NOT_NOW)
    assert clock.t - t0 < 1.0                                 # asked right away, went right away
    gate.release()


def test_busy_no_answer_then_idle_auto_go():
    gate, clock, mon, ui = make(lambda q, n: BLOCK if q == ASK_TEXT else None, idle_for=0)
    t0 = clock.t
    busy_until(clock, mon, t0 + 2)                            # busy 2 s, then hands off
    assert gate.acquire() == "auto"
    assert ui.questions() == [ASK_TEXT, NOTICE_TEXT]          # question auto-closed, then the notice
    assert YES in ui.answered and not ui.is_open
    assert clock.t - t0 >= 2 + 6 + 3 - 0.2                    # idle 6 s, then 3 s notice
    gate.release()


def test_not_now_reask_then_denied():
    gate, clock, mon, ui = make(lambda q, n: NOT_NOW, idle_for=0, poll_s=0.5)
    busy_until(clock, mon, 1e9)
    t0 = clock.t
    with pytest.raises(MouseDenied):
        gate.acquire()
    times = [t for _, _, t in ui.asked]
    assert all(q == ASK_TEXT for q in ui.questions())
    assert 4 <= len(times) <= 6                               # 0, 60, 120, 180, 240 (300 = max wait)
    assert all(b - a >= 60 - 1e-6 for a, b in zip(times, times[1:]))   # never more often than every 60 s
    assert clock.t - t0 >= 300 - 1e-6
    assert gate.lock.owner is None                            # lock released on the way out


def test_notice_not_now_goes_back_to_waiting():
    gate, clock, mon, ui = make(lambda q, n: NOT_NOW if n == 1 else None, idle_for=30)
    t0 = clock.t
    assert gate.acquire() == "auto"
    assert ui.questions() == [NOTICE_TEXT, NOTICE_TEXT]
    assert clock.t - t0 >= 60 + 3 - 0.2                       # waited the re-ask interval, notice again
    gate.release()


def test_typing_during_notice_switches_to_asking():
    gate, clock, mon, ui = make(lambda q, n: BLOCK if q == NOTICE_TEXT else YES, idle_for=30)
    t0 = clock.t
    fired = []

    def typer(t):
        if t > t0 + 1 and not fired:
            fired.append(t)
            mon.on_press("x", False)
    clock.hooks.append(typer)
    assert gate.acquire() == "yes"
    assert ui.questions() == [NOTICE_TEXT, ASK_TEXT] and not ui.is_open
    gate.release()


def test_kill_switch_while_waiting():
    gate, clock, mon, ui = make(lambda q, n: BLOCK, idle_for=0)
    busy_until(clock, mon, 1e9)
    t0 = clock.t

    class Killed(Exception):
        pass

    def check():
        if clock.t > t0 + 5:
            raise Killed()
    with pytest.raises(Killed):
        gate.acquire(check)
    assert clock.t - t0 < 6 and not ui.is_open and gate.lock.owner is None


def test_stop_clicked_on_card_denies():
    gate, clock, mon, ui = make(lambda q, n: BLOCK, idle_for=0)
    busy_until(clock, mon, 1e9)
    clock.hooks.append(lambda t: ui.stop_clicked.set() if t > clock.t0 + 2 else None)
    clock.t0 = clock.t
    with pytest.raises(MouseDenied):
        gate.acquire()
    assert not ui.is_open


# ============================================================================ lock
def test_lock_exclusive_between_threads_and_reentrant():
    lock = mg._MouseLock()
    clock = mg.RealClock()
    gates = [mg.MouseGate(monitor=mg.ActivityMonitor(clock, seed=clock.now() - 60), clock=clock, idle_s=6,
                          notice_s=0, poll_s=0.005, settings_path=None, lock=lock, log=lambda *a: None)
             for _ in range(2)]
    spans, errors = [], []

    def worker(g, tag):
        try:
            for _ in range(3):
                with g.burst():
                    assert g.acquire() == "nested"           # re-entrant on the same thread
                    g.release()
                    a = time.perf_counter()
                    time.sleep(0.03)
                    spans.append((a, time.perf_counter(), tag))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(g, i)) for i, g in enumerate(gates)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not errors and len(spans) == 6
    spans.sort()
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:]))   # never two bursts at once
    assert lock.owner is None and lock.depth == 0


def test_lock_wait_respects_max_wait_and_kill():
    lock = mg._MouseLock()
    other = threading.Thread(target=lock.try_acquire)            # another thread holds it forever
    other.start()
    other.join()
    gate, clock, mon, ui = make(idle_for=30, lock=lock, max_wait_s=5)
    with pytest.raises(MouseDenied):
        gate.acquire()
    assert ui.asked == []                                        # nobody is asked while another burst runs


# ============================================================================ policies + continuity
def test_continuity_no_activity_no_new_notice():
    gate, clock, mon, ui = make(idle_for=30)
    assert gate.acquire() == "idle"
    gate.release()
    clock.sleep(20)                                              # model thinking, user away
    assert gate.acquire() == "continue"
    gate.release()
    assert ui.questions() == [NOTICE_TEXT]
    mon.on_move(1, 2, False)                                     # user touched the mouse between steps
    busy_until(clock, mon, clock.t + 1)
    ui.reply = lambda q, n: YES
    assert gate.acquire() == "yes"                               # -> asks again
    gate.release()
    gate.new_task()
    clock.sleep(10)
    assert gate.acquire() == "idle"                              # new task: no continuity carried over
    gate.release()


def test_policy_always_ask():
    gate, clock, mon, ui = make(lambda q, n: YES, idle_for=30, policy="always_ask")
    assert gate.acquire() == "yes" and ui.questions() == [ASK_TEXT]   # idle, but still asks (no notice)
    gate.release()
    # no answer + idle: never goes by itself
    gate2, clock2, mon2, ui2 = make(lambda q, n: BLOCK, idle_for=30, policy="always_ask", max_wait_s=20)
    with pytest.raises(MouseDenied):
        gate2.acquire()
    assert ui2.questions() == [ASK_TEXT] and not ui2.is_open


def test_policy_ask_once_per_task():
    gate, clock, mon, ui = make(lambda q, n: YES if q == ASK_TEXT else None, idle_for=30,
                                policy="ask_once_per_task")
    assert gate.acquire() == "yes"                               # first time in the task: ask even if idle
    gate.release()
    mon.on_move(3, 3, False)
    clock.sleep(7)
    assert gate.acquire() == "idle"                              # after that: like auto (notice)
    gate.release()
    assert ui.questions() == [ASK_TEXT, NOTICE_TEXT]
    gate.new_task()
    mon.on_move(3, 3, False)
    clock.sleep(7)
    assert gate.acquire() == "yes" and ui.questions()[-1] == ASK_TEXT
    gate.release()


def test_always_ask_without_ui_falls_back_to_notice():
    gate, clock, mon, _ = make(idle_for=30, ui=False, policy="always_ask")
    assert gate.acquire() == "idle"
    gate.release()


def test_no_ui_busy_waits_for_idle():
    cur = FakeCursor()
    gate, clock, mon, _ = make(idle_for=0, ui=False, cursor=cur)
    t0 = clock.t
    busy_until(clock, mon, t0 + 4)
    assert gate.acquire() == "auto"
    assert clock.t - t0 >= 4 + 6 + 3 - 0.2
    assert any(p[0] == "say" and p[1] == mg.WAIT_SAY for p in cur.posts)
    gate.release()


# ============================================================================ settings
def test_settings_key_matches_hub(tmp_path, monkeypatch):
    from rbot.hub import core as hub_core
    assert mg.SETTINGS_KEY_IDLE in hub_core.DEFAULTS                  # the Hub's "Using your mouse" 4/6/10 s
    assert Path(mg.SETTINGS_PATH) == Path(hub_core.SETTINGS_PATH)
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"idle_seconds": 10, "mouse_policy": "always_ask", "skin": "mint"}), encoding="utf-8")
    assert mg.read_settings(p) == (10.0, "always_ask")
    gate, *_ = make(settings_path=p)
    assert (gate.idle_s, gate.policy) == (10.0, "always_ask")
    p.write_text(json.dumps({"idle_seconds": 4}), encoding="utf-8")   # changed in the Hub: next acquire sees it
    assert gate.reload_settings() == (4.0, "auto")
    monkeypatch.setenv("BUDDY_IDLE_S", "2.5")
    monkeypatch.setenv("BUDDY_MOUSE_POLICY", "ask_once_per_task")
    assert mg.read_settings(p) == (2.5, "ask_once_per_task")
    monkeypatch.delenv("BUDDY_IDLE_S")
    monkeypatch.delenv("BUDDY_MOUSE_POLICY")
    p.write_text(json.dumps({"idle_seconds": 999, "mouse_policy": "yolo"}), encoding="utf-8")
    assert mg.read_settings(p) == (60.0, "auto")                      # same clamp as hub_core, bad policy -> auto
    p.write_text("not json", encoding="utf-8")
    assert mg.read_settings(p) == (6.0, "auto")
    assert mg.read_settings(tmp_path / "missing.json") == (6.0, "auto")
    gate2, *_ = make(settings_path=p, idle_s=8, policy="always_ask")  # explicit args win
    assert (gate2.idle_s, gate2.policy) == (8.0, "always_ask")


# ============================================================================ Executor wiring
class Dummy:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass

    def stop(self):
        pass


class FakeMouse:
    def __init__(self):
        self._pos, self.log = (0, 0), []

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, p):
        self._pos = p
        self.log.append(("move", p))

    def press(self, b):
        self.log.append(("press", b))

    def release(self, b):
        self.log.append(("release", b))

    def scroll(self, dx, dy):
        self.log.append(("scroll", dy))


class FakeKb:
    def __init__(self):
        self.log = []

    def type(self, t):
        self.log.append(("type", t))

    def press(self, k):
        self.log.append(("press", k))

    def release(self, k):
        self.log.append(("release", k))


class CountingGate(mg.MouseGate):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fresh = 0

    def acquire(self, check=None):
        how = super().acquire(check)
        if how != "nested":
            self.fresh += 1
        return how


def make_executor(monkeypatch, gate=None):
    monkeypatch.setattr(actions.mouse, "Listener", Dummy)
    monkeypatch.setattr(actions.keyboard, "GlobalHotKeys", Dummy)
    monkeypatch.setattr(actions, "STEP", 0.0)
    ex = actions.Executor(gate=gate)
    ex.mouse, ex.kb = FakeMouse(), FakeKb()
    ex._last = (0, 0)
    return ex


def counting_gate(reply=None, idle_for=30, **kw):
    clock = FakeClock()
    mon = mg.ActivityMonitor(clock, seed=clock.t - idle_for)
    ui = FakeUI(reply or (lambda q, n: None), clock)
    g = CountingGate(ui=ui, monitor=mon, clock=clock, settings_path=None, poll_s=0.1, ui_grace_s=0,
                     lock=mg._MouseLock(), log=lambda *a: None, **kw)
    return g, clock, mon, ui


def test_executor_without_gate_unchanged(monkeypatch):
    ex = make_executor(monkeypatch)
    assert ex.gate is None
    with ex.step():                                              # no-op without a gate
        ex.click(10, 20)
    assert ("press", actions.Button.left) in ex.mouse.log


def test_executor_every_real_input_goes_through_gate(monkeypatch):
    g, clock, mon, ui = counting_gate()
    ex = make_executor(monkeypatch, g)
    calls = [lambda: ex.click(1, 1), lambda: ex.double_click(1, 1), lambda: ex.right_click(1, 1),
             lambda: ex.move(2, 2), lambda: ex.drag(1, 1, 5, 5, steps=2), lambda: ex.scroll(1, 1, -3),
             lambda: ex.type_text("hi"), lambda: ex.press_key("enter"), lambda: ex.hotkey(["ctrl", "a"]),
             lambda: ex.hold(3, 3, 0.01)]
    for i, c in enumerate(calls, 1):
        c()
        assert g.fresh == i and not g.held()                     # one burst per call, released afterwards
    assert ui.questions() == [NOTICE_TEXT]                       # continuity: one notice for the whole run
    ex.wait(0.01)                                                # not real input: no gate
    assert g.fresh == len(calls)
    # SetCursorPos positions were registered as our own moves
    assert len(mon._own) > 0


def test_executor_step_is_one_lazy_burst(monkeypatch):
    g, clock, mon, ui = counting_gate()
    ex = make_executor(monkeypatch, g)
    with ex.step():
        pass                                                     # no real input -> gate not consulted
    assert g.fresh == 0
    with ex.step():
        ex.click(1, 1)
        assert g.held()                                          # held across the whole step
        ex.type_text("x")
        ex.open_app("notepad")
    assert g.fresh == 1 and not g.held()


def test_executor_takeover_only_during_burst(monkeypatch):
    g, clock, mon, ui = counting_gate()
    ex = make_executor(monkeypatch, g)
    calls = []
    ex.takeover_handler = lambda: calls.append(1)
    ex._grace_until = 0.0
    ex._on_click(5, 5, None, True, False)                        # between bursts: the PC is yours
    assert not ex.takeover_pending and not calls
    with pytest.raises(actions.HumanTakeover):
        with ex.step():
            ex.click(1, 1)
            ex._on_click(9, 9, None, True, False)                # during the burst: a real click = takeover
            assert ex.takeover_pending and calls == [1]
            ex.type_text("x")
    assert ex.kb.log == [] and not g.held()


def test_executor_kill_switch_at_gate(monkeypatch):
    g, clock, mon, ui = counting_gate(lambda q, n: BLOCK, idle_for=0)
    ex = make_executor(monkeypatch, g)
    t0 = clock.t
    busy_until(clock, mon, 1e9)
    clock.hooks.append(lambda t: setattr(ex, "killed", True) if t > t0 + 3 else None)
    with pytest.raises(actions.KillSwitch):
        ex.click(1, 1)
    assert ex.mouse.log == [] and not ui.is_open and not g.held()
    assert ui.questions() == [ASK_TEXT]


def test_executor_mouse_denied(monkeypatch):
    g, clock, mon, ui = counting_gate(lambda q, n: NOT_NOW, idle_for=0, max_wait_s=30)
    ex = make_executor(monkeypatch, g)
    busy_until(clock, mon, 1e9)
    with pytest.raises(MouseDenied):
        ex.type_text("secret")
    assert ex.kb.log == [] and not g.held()


def test_executor_clicks_while_waiting_are_not_takeover(monkeypatch):
    """Clicking Yes on the question card (or anything else while we wait) must not abort the burst."""
    def reply(q, n):
        ex._on_click(7, 7, None, True, False)                    # the user's click on "Yes"
        return YES
    g, clock, mon, ui = counting_gate(reply, idle_for=0)
    ex = make_executor(monkeypatch, g)
    ex.takeover_handler = lambda: None
    ex._grace_until = 0.0
    busy_until(clock, mon, 1e9)
    ex.click(4, 4)
    assert ("press", actions.Button.left) in ex.mouse.log


def test_hold_presses_waits_releases(monkeypatch):
    ex = make_executor(monkeypatch)                              # works without a gate too
    t = time.perf_counter()
    ex.hold(50, 60, 0.15)
    assert time.perf_counter() - t >= 0.15
    log = ex.mouse.log
    assert log[-2:] == [("press", actions.Button.left), ("release", actions.Button.left)]
    assert ("move", (50, 60)) in log


def test_hold_releases_button_on_takeover_and_kill(monkeypatch):
    g, clock, mon, ui = counting_gate()
    ex = make_executor(monkeypatch, g)
    ex.takeover_handler = lambda: None
    ex._grace_until = 0.0
    def when_pressed(fn):                                        # act once the button is really down
        def run():
            end = time.time() + 5
            while ("press", actions.Button.left) not in ex.mouse.log and time.time() < end:
                time.sleep(0.005)
            time.sleep(0.02)
            fn()
        threading.Thread(target=run, daemon=True).start()
    when_pressed(lambda: ex._on_click(1, 1, None, True, False))
    with pytest.raises(actions.HumanTakeover):
        ex.hold(10, 10, 5)
    assert ex.mouse.log[-1] == ("release", actions.Button.left) and not g.held()
    ex.mouse.log.clear()
    when_pressed(lambda: setattr(ex, "killed", True))
    with pytest.raises(actions.KillSwitch):
        ex.hold(10, 10, 5)
    assert ex.mouse.log[-1] == ("release", actions.Button.left) and not g.held()
