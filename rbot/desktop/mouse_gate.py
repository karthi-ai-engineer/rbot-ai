"""Real-mouse policy (PLAN.md §5): before the agent touches the REAL mouse/keyboard it goes through this gate.

  idle >= IDLE_S      -> 3 s notice "Using the mouse…" [Not now], then go
  busy                -> ask right away "May I use the mouse for a moment?" [Yes] [Not now]
                         Yes -> go now · Not now -> wait (ask again at most every 60 s)
                         no answer and you go idle for IDLE_S -> the question closes, 3 s notice, go
  nothing after MAX_WAIT_S (300 s)  -> MouseDenied

Mouse lock: one process-wide lock, held for one burst (= one agent step's real input). While held there are no
re-checks; the next burst checks again. Continuity: if you have not touched the keyboard/mouse at all since the
last burst ended, the next burst goes straight away (no notice per step while you are away).

Idle = time since the last REAL input. Our own input is ignored: pynput reports SendInput events with
injected=True; positions the Executor sets with SetCursorPos are also registered via own_move() and ignored.

Policies ("mouse_policy" in data/settings.json, env BUDDY_MOUSE_POLICY):
  auto               the flow above (default)
  always_ask         always ask (no idle shortcut, no auto-go); a Yes lasts until you touch the mouse/keyboard
  ask_once_per_task  ask the first time in a task; after a Yes it behaves like auto (notice / ask when busy)

Idle seconds: "idle_seconds" in data/settings.json (the Hub's "Using your mouse" 4/6/10 s setting),
env BUDDY_IDLE_S overrides it.

  gate = make_gate(ui=hitl_ui_or_None)       # starts the real-input listeners
  ex = Executor(cursor=cur, gate=gate)       # every real input of the executor goes through the gate
  gate.new_task()                            # at the start of each task (ask_once_per_task / continuity)
"""
import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from rbot import DATA

SETTINGS_PATH = DATA / "settings.json"         # same file + key as rbot.hub.core.Settings ("idle_seconds")
SETTINGS_KEY_IDLE = "idle_seconds"
SETTINGS_KEY_POLICY = "mouse_policy"
POLICIES = ("auto", "always_ask", "ask_once_per_task")

IDLE_S = 6.0
NOTICE_S = 3.0
REASK_S = 60.0
MAX_WAIT_S = 300.0
POLL_S = 0.1

ASK_TEXT = "May I use the mouse for a moment?"
NOTICE_TEXT = "Using the mouse…"
YES, NOT_NOW = "Yes", "Not now"
WAIT_SAY = "I'll use the mouse when you pause"


class MouseDenied(Exception):
    """The user did not free the mouse within MAX_WAIT_S (or stopped us from the question card)."""


class RealClock:
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


# ---------------------------------------------------------------- real-input (idle) detector
class ActivityMonitor:
    """Time of the last REAL keyboard/mouse input. Feed it pynput-shaped callbacks (injected flag included);
    start() hooks the real pynput listeners."""

    OWN_MOVE_S = 0.5                               # an uninjected move landing exactly where we just put the mouse

    def __init__(self, clock=None, seed=None):
        self.clock = clock or RealClock()
        self.last_real = self.clock.now() if seed is None else seed
        self.count = 0                             # real events seen
        self.key_count = 0                         # real keyboard events seen
        self._own = deque(maxlen=64)               # (x, y, t) positions the Executor set itself
        self._listeners = []

    def note(self, kind="mouse", injected=False):
        if injected:
            return
        self.last_real = self.clock.now()
        self.count += 1
        if kind == "key":
            self.key_count += 1

    def own_move(self, x, y):
        self._own.append((int(x), int(y), self.clock.now()))

    def _is_own(self, x, y):
        now = self.clock.now()
        return any(abs(ox - int(x)) <= 1 and abs(oy - int(y)) <= 1 and now - t <= self.OWN_MOVE_S
                   for ox, oy, t in list(self._own))

    def idle_for(self):
        return max(0.0, self.clock.now() - self.last_real)

    # pynput callbacks (listener threads)
    def on_move(self, x, y, injected=False):
        if not injected and self._is_own(x, y):
            return
        self.note("mouse", injected)

    def on_click(self, x, y, button, pressed, injected=False):
        self.note("mouse", injected)

    def on_scroll(self, x, y, dx, dy, injected=False):
        self.note("mouse", injected)

    def on_press(self, key, injected=False):
        self.note("key", injected)

    def on_release(self, key, injected=False):
        self.note("key", injected)

    def start(self):
        from pynput import keyboard, mouse
        self._listeners = [mouse.Listener(on_move=self.on_move, on_click=self.on_click, on_scroll=self.on_scroll),
                           keyboard.Listener(on_press=self.on_press, on_release=self.on_release)]
        for lst in self._listeners:
            lst.daemon = True
            lst.start()
        return self

    def stop(self):
        for lst in self._listeners:
            try:
                lst.stop()
            except Exception:  # noqa: BLE001
                pass
        self._listeners = []


def seconds_since_last_input():
    """GetLastInputInfo (counts injected input too, so only used to seed the monitor at start-up)."""
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return max(0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------------------------------------------------------------- settings
def read_settings(path=SETTINGS_PATH):
    """-> (idle_s, policy). File first, then env overrides (BUDDY_IDLE_S, BUDDY_MOUSE_POLICY)."""
    raw = {}
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    idle, policy = IDLE_S, "auto"
    try:
        idle = float(max(2, min(60, int(raw.get(SETTINGS_KEY_IDLE, IDLE_S)))))   # same clamp as rbot.hub.core
    except (TypeError, ValueError):
        pass
    if raw.get(SETTINGS_KEY_POLICY) in POLICIES:
        policy = raw[SETTINGS_KEY_POLICY]
    env = os.environ.get("BUDDY_IDLE_S", "").strip()
    if env:
        try:
            idle = max(0.5, float(env))
        except ValueError:
            pass
    env = os.environ.get("BUDDY_MOUSE_POLICY", "").strip().lower()
    if env in POLICIES:
        policy = env
    return idle, policy


# ---------------------------------------------------------------- the process-wide mouse lock
class _MouseLock:
    """Re-entrant per thread; non-blocking try so waiters can poll the kill switch."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mu = threading.Lock()
        self.owner = None
        self.depth = 0

    def try_acquire(self):
        me = threading.get_ident()
        with self._mu:
            if self.owner == me:
                self.depth += 1
                return "nested"
        if not self._lock.acquire(blocking=False):
            return None
        with self._mu:
            self.owner, self.depth = me, 1
        return "fresh"

    def mine(self):
        return self.owner == threading.get_ident()

    def release(self):
        """-> True when the lock was really let go (outermost release)."""
        with self._mu:
            if self.owner != threading.get_ident():
                raise RuntimeError("mouse lock released by a thread that does not hold it")
            self.depth -= 1
            if self.depth > 0:
                return False
            self.owner = None
        self._lock.release()
        return True


MOUSE_LOCK = _MouseLock()


# ---------------------------------------------------------------- a question running on its own thread
class _Prompt:
    """ui.ask() is blocking; run it on a helper thread so the gate keeps watching idle time / the kill switch."""

    def __init__(self, ui, question, options, timeout_s, mood):
        self.ui, self.result = ui, None
        self.done = threading.Event()
        args = (question, options)
        kw = {"allow_text": False, "timeout_s": timeout_s, "mood": mood}
        self.t = threading.Thread(target=self._run, args=(args, kw), name="mouse-gate-ask", daemon=True)
        self.t.start()

    def _run(self, args, kw):
        try:
            self.result = self.ui.ask(*args, **kw)
        except Exception as e:  # noqa: BLE001
            print(f"mouse_gate: ui.ask failed: {type(e).__name__}: {e}")
            self.result = None
        finally:
            self.done.set()

    def close(self, answer=None, grace_s=0.6, tries=8):
        """Close it if still open: wait grace_s for it to end by itself (its own timeout), then answer it
        programmatically / dismiss_all, retried a few times (the card may not be on screen yet)."""
        ended = self.done.wait(grace_s) if grace_s else self.done.is_set()
        for i in range(tries):
            if ended:
                return
            try:
                self.ui.answer(answer) if i % 2 == 0 else self.ui.dismiss_all()
            except Exception:  # noqa: BLE001
                pass
            ended = self.done.wait(0.25)


# ---------------------------------------------------------------- the gate
class MouseGate:
    def __init__(self, ui=None, cursor=None, monitor=None, clock=None, *, idle_s=None, policy=None,
                 settings_path=SETTINGS_PATH, notice_s=NOTICE_S, reask_s=REASK_S, max_wait_s=MAX_WAIT_S,
                 poll_s=POLL_S, ui_grace_s=0.6, lock=None, log=print):
        self.ui = ui
        self.cursor = cursor if cursor is not None else getattr(ui, "cur", None)
        self.clock = clock or RealClock()
        self.monitor = monitor or ActivityMonitor(self.clock)
        self._idle_arg, self._policy_arg = idle_s, policy
        self.settings_path = settings_path
        self.notice_s, self.reask_s, self.max_wait_s = notice_s, reask_s, max_wait_s
        self.poll_s, self.ui_grace_s = poll_s, ui_grace_s
        self.lock = lock or MOUSE_LOCK
        self.log = log or (lambda *a: None)
        self.idle_s, self.policy = IDLE_S, "auto"
        self._last_release = None                   # clock time the last burst of this task ended
        self._task_granted = False                  # a burst was granted in this task (ask_once_per_task)
        self.last_how = None                        # how the last fresh acquire was granted (for logs/tests)
        self.reload_settings()

    # ---- settings / task ----
    def reload_settings(self):
        idle, policy = read_settings(self.settings_path)
        self.idle_s = float(self._idle_arg) if self._idle_arg is not None else idle
        self.policy = self._policy_arg if self._policy_arg in POLICIES else policy
        return self.idle_s, self.policy

    def new_task(self):
        """Forget the previous task's permission (ask_once_per_task) and the continuity shortcut."""
        self._last_release = None
        self._task_granted = False

    def own_move(self, x, y):
        fn = getattr(self.monitor, "own_move", None)
        if fn:
            fn(x, y)

    def held(self):
        """Does the calling thread hold the mouse lock?"""
        return self.lock.mine()

    def stop(self):
        fn = getattr(self.monitor, "stop", None)
        if fn:
            fn()

    # ---- the lock ----
    @contextmanager
    def burst(self, check=None):
        """with gate.burst(): ... one burst of real input (acquire = the §5 check; re-entrant per thread)."""
        self.acquire(check)
        try:
            yield
        finally:
            self.release()

    def acquire(self, check=None):
        """Block until the real mouse may be used. -> "nested" | "continue" | "idle" | "yes" | "auto".
        check(): called every poll, may raise (the Executor passes its kill-switch check).
        Raises MouseDenied after max_wait_s."""
        check = check or (lambda: None)
        deadline = self.clock.now() + self.max_wait_s
        while True:
            check()
            got = self.lock.try_acquire()
            if got == "nested":
                return "nested"
            if got == "fresh":
                break
            if self.clock.now() >= deadline:
                raise MouseDenied("the mouse stayed busy (another task)")
            self.clock.sleep(self.poll_s)
        try:
            how = self._decide(check, deadline)
        except BaseException:
            self.lock.release()
            raise
        self._task_granted = True
        self.last_how = how
        return how

    def release(self):
        if self.lock.release():
            self._last_release = self.clock.now()

    # ---- the policy ----
    def _continuous(self):
        return self._last_release is not None and self.monitor.last_real < self._last_release

    def _stopped(self):
        ev = getattr(self.ui, "stop_clicked", None)
        return bool(ev is not None and ev.is_set())

    def _say(self, text):
        self.log(f"[mouse] {text}")
        if self.cursor is not None:
            try:
                self.cursor.post("say", text, False)
            except Exception:  # noqa: BLE001
                pass

    def _decide(self, check, deadline):
        self.reload_settings()
        if self._continuous():
            return "continue"
        policy = self.policy
        if policy == "ask_once_per_task":
            policy = "auto" if self._task_granted else "always_ask"
        if policy == "always_ask" and self.ui is None:
            policy = "auto"                          # nobody to ask: fall back to notice-when-idle
        cooldown_until, prompt, waited, said_wait = 0.0, None, False, False
        try:
            while True:
                check()
                now = self.clock.now()
                if self._stopped():
                    raise MouseDenied("stopped from the question card")
                if now >= deadline:
                    raise MouseDenied(f"waited {self.max_wait_s:.0f} s for the mouse")
                idle = self.monitor.idle_for() >= self.idle_s
                if prompt is not None and prompt.done.is_set():
                    ans, prompt = prompt.result, None
                    if ans == YES:
                        return "yes"
                    cooldown_until = now + self.reask_s       # Not now / Esc / its own timeout
                    self.log(f"[mouse] not now ({ans!r}); asking again in {self.reask_s:.0f} s at the latest")
                elif now < cooldown_until:
                    pass
                elif policy == "auto" and idle:
                    if prompt is not None:                    # no answer, but you went idle: close it, go
                        prompt.close(YES, grace_s=0)
                        prompt = None
                    r = self._notice(check)
                    if r == "go":
                        return "auto" if waited else "idle"
                    waited = True
                    if r == "not_now":
                        cooldown_until = self.clock.now() + self.reask_s
                    continue
                elif prompt is None:
                    waited = True
                    if self.ui is not None:
                        prompt = _Prompt(self.ui, ASK_TEXT, [YES, NOT_NOW],
                                         max(1.0, deadline - now), "calling")
                    elif not said_wait:
                        self._say(WAIT_SAY)
                        said_wait = True
                self.clock.sleep(self.poll_s)
        finally:
            if prompt is not None:
                prompt.close(None, grace_s=0)

    def _notice(self, check):
        """3 s notice with Not now. -> "go" | "not_now" | "busy" (you started typing / moved, without a ui)."""
        start = self.clock.now()
        keys0, any0 = self.monitor.key_count, self.monitor.count
        prompt = None
        if self.ui is not None:
            prompt = _Prompt(self.ui, NOTICE_TEXT, [NOT_NOW], self.notice_s, "normal")
        else:
            self._say(f"Using the mouse in {self.notice_s:.0f} s… move it to stop me")
        ok = False
        try:
            while self.clock.now() - start < self.notice_s:
                check()
                if self._stopped():
                    raise MouseDenied("stopped from the notice")
                if prompt is not None and prompt.done.is_set() and prompt.result == NOT_NOW:
                    self.log("[mouse] notice: not now")
                    return "not_now"
                if self.monitor.key_count > keys0:          # started typing: don't grab the mouse, ask instead
                    return "busy"
                if prompt is None and self.monitor.count > any0:
                    self._say("Okay, I'll wait")
                    return "busy"
                self.clock.sleep(self.poll_s)
            if prompt is not None and prompt.done.is_set() and prompt.result == NOT_NOW:
                return "not_now"
            ok = True
            return "go"
        finally:
            if prompt is not None:
                prompt.close(None, grace_s=self.ui_grace_s if ok else 0)
                if ok and prompt.result == NOT_NOW:          # clicked in the very last moment: too late, noted
                    self.log("[mouse] Not now came after the notice ended")


def make_gate(ui=None, settings_path=SETTINGS_PATH, cursor=None, start=True, **kw):
    """Production gate: real pynput listeners (injected input ignored), idle seeded from GetLastInputInfo."""
    clock = RealClock()
    seed = None
    try:
        seed = clock.now() - seconds_since_last_input()
    except Exception:  # noqa: BLE001
        pass
    mon = ActivityMonitor(clock, seed=seed)
    if start:
        mon.start()
    return MouseGate(ui=ui, cursor=cursor, monitor=mon, clock=clock, settings_path=settings_path, **kw)
