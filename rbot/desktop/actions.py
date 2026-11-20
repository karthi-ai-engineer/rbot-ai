"""Real mouse/keyboard actions for the agent, with the buddy cursor gliding along (research prototype).

All coordinates are PHYSICAL screen pixels (same as perception.observe()).
Safety: Ctrl+Alt+Q kills the agent (KillSwitch raised on the next action);
moving the real mouse yourself pauses the agent for 3 s.

Human-in-the-loop (set by agent.run_task when a HumanLoop is given):
  ex.takeover_handler = fn   real (non-injected) mouse movement > TAKEOVER_PX, a real click or wheel turn
                             while the agent runs -> fn() is called once (listener thread, keep it tiny) and
                             the next _check() raises HumanTakeover instead of the 3 s pause.
  ex.before_press = fn       (set by agent.execute around one click) fn(x, y) runs after the pointer arrived and
                             before the button goes down: the hover check. It may raise to cancel the click.
  ex.human_turn = True       a human turn is in progress (handover / question): the human owns the mouse,
                             all movement is ignored. Setting it back to False re-bases the watch position
                             and gives a short grace period so moving away from a dialog is not a takeover.

Real-mouse policy (PLAN §5, mouse_gate.py), opt-in:
  Executor(cursor, gate=mouse_gate.make_gate(ui))   every method that sends real input first passes the gate
                             (idle -> 3 s notice, busy -> ask, MouseDenied after max wait). gate=None = old behaviour.
  with ex.step(): ...        one agent step = one mouse burst: the gate is consulted at the first real input of
                             the step and the lock is released when the block ends. Without step() every
                             method call is its own burst (the gate's continuity rule avoids repeated notices).
  With a gate, a real click counts as a takeover only DURING a burst; between bursts the user may use the PC
  (the gate asks again next time). While waiting at the gate, Ctrl+Alt+Q still raises KillSwitch.
"""
import ctypes
import math
import time
from contextlib import contextmanager

ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: pixels == input coords

from pynput import keyboard, mouse                      # noqa: E402  (after DPI awareness on purpose)
from pynput.keyboard import Key                         # noqa: E402
from pynput.mouse import Button                         # noqa: E402

STEP = 0.05                 # pause between move / press / release so apps register each event
TAKEOVER_PX = 15            # old pause mode (no takeover handler)
TAKEOVER_MOVE_PX = 150      # takeover mode: clicks/scrolls always count, movement only if it is a real move
PAUSE_S = 3.0
TAKEOVER_GRACE_S = 2.0      # after arming / after a human turn, movement only re-bases (no takeover)
ALIASES = {"escape": "esc", "return": "enter", "del": "delete", "control": "ctrl", "win": "cmd", "windows": "cmd",
           "super": "cmd", "meta": "cmd", "pgup": "page_up", "pageup": "page_up", "pgdn": "page_down",
           "pagedown": "page_down", "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
           "bksp": "backspace", "ins": "insert", "prtsc": "print_screen", "printscreen": "print_screen",
           "capslock": "caps_lock", "spacebar": "space", "apps": "menu"}


class KillSwitch(Exception):
    """User pressed Ctrl+Alt+Q."""


class HumanTakeover(Exception):
    """The user grabbed the mouse (moved / clicked / scrolled) while the agent was working."""


def to_key(name):
    n = name.strip().lower().replace(" ", "_").replace("-", "_")
    n = ALIASES.get(n.replace("_", ""), ALIASES.get(n, n))
    if len(n) == 1:
        return keyboard.KeyCode.from_char(n)
    return getattr(Key, n)                              # AttributeError for unknown key names


class Executor:
    def __init__(self, cursor=None, gate=None):
        self.cursor = cursor
        self.gate = gate                               # mouse_gate.MouseGate or None (no real-mouse policy)
        self._gating = False                           # waiting at the gate: the user's input is theirs
        self._bursting = False                         # holding the mouse lock (gate mode)
        self._step = False                             # inside `with ex.step():`
        self._step_held = False                        # ... and the gate was already passed for it
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        self.killed = False
        self.paused_until = 0.0
        self._busy = False
        self._last = self.mouse.position
        self._handler = None                           # takeover_handler (None = old 3 s pause behaviour)
        self._human = False                            # human_turn
        self._takeover = False                         # real user input seen -> next _check raises
        self._grace_until = 0.0
        self.before_press = None                       # hover check hook, see module docstring
        self._hotkeys = keyboard.GlobalHotKeys({"<ctrl>+<alt>+q": self._kill})
        self._hotkeys.start()
        self._watch = mouse.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self._watch.start()

    # ---- safety ------------------------------------------------------------
    def _kill(self):
        print("KILL SWITCH (Ctrl+Alt+Q)")
        self.killed = True

    @property
    def takeover_handler(self):
        return self._handler

    @takeover_handler.setter
    def takeover_handler(self, fn):
        self._handler = fn
        self._takeover = False
        self._rebase()

    @property
    def human_turn(self):
        return self._human

    @human_turn.setter
    def human_turn(self, on):
        self._human = bool(on)
        self._takeover = False
        self.paused_until = 0.0
        if not on:
            self._rebase()

    @property
    def takeover_pending(self):
        return self._takeover and self._handler is not None and not self._human

    def _rebase(self):
        try:
            self._last = self.mouse.position
        except Exception:  # noqa: BLE001
            pass
        self._grace_until = time.time() + TAKEOVER_GRACE_S

    def _user_input(self):
        """Real input from the user (listener thread)."""
        if self._handler is None:
            self.paused_until = time.time() + PAUSE_S
        elif not self._takeover:
            self._takeover = True
            try:
                self._handler()
            except Exception:  # noqa: BLE001
                pass

    def _ignore(self, injected):
        if self.gate is not None and (self._gating or not self._bursting):
            return True                                # gate mode: only input DURING a burst is a takeover
        return self._busy or injected or self._human

    def _on_move(self, x, y, injected=False):
        if self._ignore(injected):
            return
        if time.time() < self._grace_until:            # e.g. moving away from the question bubble
            self._last = (x, y)
            return
        if self._handler is not None:                  # takeover mode: only a real CLICK means "I'm taking over";
            return                                     # moving the mouse to watch the screen is not a takeover
        lx, ly = self._last
        if math.hypot(x - lx, y - ly) > TAKEOVER_PX:
            self._user_input()

    def _on_click(self, x, y, button, pressed, injected=False):
        if pressed and self._handler is not None and not self._ignore(injected) \
                and time.time() >= self._grace_until:
            self._user_input()

    def _on_scroll(self, x, y, dx, dy, injected=False):
        return                                         # reading/scrolling elsewhere is not a takeover

    def _check_takeover(self):
        if self.killed:
            raise KillSwitch()
        if self.takeover_pending:
            self._takeover = False
            raise HumanTakeover()

    def _check(self):
        self._check_takeover()
        if time.time() < self.paused_until:
            print("paused: user is using the mouse")
            while time.time() < self.paused_until:
                self._check_takeover()
                time.sleep(0.1)
            self._last = self.mouse.position          # resume from wherever the user left it

    def stop(self):
        self._hotkeys.stop()
        self._watch.stop()
        if self.gate is not None:
            try:
                self.gate.stop()
            except Exception:  # noqa: BLE001
                pass

    # ---- real-mouse gate (PLAN §5) -----------------------------------------
    def _check_kill(self):
        if self.killed:
            raise KillSwitch()

    def _gate_acquire(self):
        self._gating = True
        try:
            how = self.gate.acquire(check=self._check_kill)
        finally:
            self._gating = False
        if how != "nested":                            # fresh burst: whatever the user did before is not a takeover
            self._takeover = False
            self.paused_until = 0.0
            try:
                self._last = self.mouse.position
            except Exception:  # noqa: BLE001
                pass
            self._bursting = True
        return how

    def _gate_release(self):
        if self.gate.held() and self.gate.lock.depth == 1:
            self._bursting = False
        self.gate.release()

    @contextmanager
    def _real(self):
        """Around every method that sends real input."""
        if self.gate is None:
            yield
            return
        if self._step:
            if not self._step_held:
                self._gate_acquire()
                self._step_held = True
            yield                                      # released when the step ends
            return
        self._gate_acquire()
        try:
            yield
        finally:
            self._gate_release()

    @contextmanager
    def step(self):
        """One agent step = one mouse burst. Lazy: a step without real input never touches the gate."""
        if self.gate is None or self._step:
            yield
            return
        self._step, self._step_held = True, False
        try:
            yield
        finally:
            self._step = False
            if self._step_held:
                self._step_held = False
                self._gate_release()

    def _set_pos(self, x, y):
        if self.gate is not None:
            self.gate.own_move(x, y)                   # SetCursorPos: not the user's activity
        self.mouse.position = (int(x), int(y))

    # ---- pointer -----------------------------------------------------------
    def _glide(self, x, y):
        """Buddy cursor flies to (x, y) first, then the real mouse jumps there."""
        self._check()
        if self.cursor:
            self.cursor.post("move_to_px", x, y)
            time.sleep(0.06)                           # let the Qt thread pick up the new target
            end = time.time() + 1.5
            while not self.cursor.arrived and time.time() < end:
                time.sleep(0.02)
            self._check()                              # user may have grabbed the mouse during the glide
        self._busy = True
        self._last = (x, y)
        self._set_pos(x, y)
        time.sleep(STEP)

    def _ripple(self):
        if self.cursor:
            self.cursor.post("click")

    def _press(self, button, count=1):
        try:
            for i in range(count):
                self.mouse.press(button)
                time.sleep(0.03 if count > 1 else STEP)
                self.mouse.release(button)
                if i < count - 1:
                    time.sleep(0.03)
            self._ripple()
            time.sleep(STEP)
        finally:
            self._busy = False

    def _hover(self, x, y):
        """Pointer is at (x, y), button not pressed yet: run the before_press hook (may cancel the click)."""
        try:
            if self.before_press is not None:
                self.before_press(x, y)
            self._check_takeover()                     # kill switch pressed during the check -> no click
        except BaseException:
            self._busy = False
            raise

    def click(self, x, y):
        with self._real():
            self._glide(x, y)
            self._hover(x, y)
            self._press(Button.left)

    def double_click(self, x, y):
        with self._real():
            self._glide(x, y)
            self._hover(x, y)
            self._press(Button.left, 2)

    def right_click(self, x, y):
        with self._real():
            self._glide(x, y)
            self._hover(x, y)
            self._press(Button.right)

    def move(self, x, y):
        with self._real():
            self._glide(x, y)
            self._busy = False

    def hold(self, x, y, seconds):
        """Press and hold the left button at (x, y) for `seconds` (press-and-hold buttons), then release.
        A real click (takeover) or Ctrl+Alt+Q during the hold releases the button and raises."""
        with self._real():
            self._glide(x, y)
            self._hover(x, y)
            pressed = False
            try:
                self.mouse.press(Button.left)
                pressed = True
                self._busy = False                     # no more moves from us: a real click now is a takeover
                end = time.time() + max(0.0, float(seconds))
                while time.time() < end:
                    self._check_takeover()
                    time.sleep(min(0.05, max(0.0, end - time.time())))
            finally:
                self._busy = False
                if pressed:
                    self.mouse.release(Button.left)
            self._ripple()
            time.sleep(STEP)

    def drag(self, x1, y1, x2, y2, steps=15):
        with self._real():
            self._drag(x1, y1, x2, y2, steps)

    def _drag(self, x1, y1, x2, y2, steps):
        self._glide(x1, y1)
        try:
            self.mouse.press(Button.left)
            time.sleep(0.1)
            if self.cursor:
                self.cursor.post("move_to_px", x2, y2)
            for i in range(1, steps + 1):
                x, y = x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps
                self._last = (x, y)
                self._set_pos(x, y)
                time.sleep(0.03)
            time.sleep(0.1)
            self.mouse.release(Button.left)
            self._ripple()
            time.sleep(STEP)
        finally:
            self._busy = False

    def scroll(self, x, y, clicks):
        """clicks > 0 scrolls up, < 0 scrolls down."""
        with self._real():
            self._glide(x, y)
            try:
                self.mouse.scroll(0, clicks)
                time.sleep(STEP)
            finally:
                self._busy = False

    # ---- keyboard ----------------------------------------------------------
    def type_text(self, text):
        with self._real():
            self._check()
            self.kb.type(text)                         # unicode-safe; "\n" -> Enter, "\t" -> Tab
            time.sleep(STEP)

    def press_key(self, name):
        k = to_key(name)                               # unknown key names fail before the gate asks
        with self._real():
            self._check()
            self.kb.press(k)
            time.sleep(STEP)
            self.kb.release(k)
            time.sleep(STEP)

    def hotkey(self, names):
        keys = [to_key(n) for n in names]
        with self._real():
            self._check()
            for k in keys:
                self.kb.press(k)
                time.sleep(STEP)
            for k in reversed(keys):
                self.kb.release(k)
                time.sleep(0.02)
            time.sleep(STEP)

    # ---- composite -----------------------------------------------------------
    def open_app(self, name):
        with self._real():                             # one burst for the whole sequence
            self.press_key("win")
            self.wait(0.6)
            self.type_text(name)
            self.wait(0.8)
            self.press_key("enter")

    def wait(self, seconds):
        self._check()
        end = time.time() + seconds
        while time.time() < end:
            self._check_takeover()
            time.sleep(min(0.1, max(0.0, end - time.time())))
