"""Buddy Hub: the always-running chat + status window for the buddy agent.

  python -m rbot                  tray icon + Hub (Ctrl+Alt+B shows / hides it, Ctrl+Alt+Q = emergency stop)
  python -m rbot --demo           scripted fake run (login handover, consent question, stuck ask, finished tasks);
                                  nothing is clicked or typed, the agent is not started
  python -m rbot --sheet          offscreen screenshots of every state -> runs/hub_*.png
  python -m rbot --selftest       non-visual checks (offscreen)

One task at a time; new tasks wait in Queued. While a task runs in Live mode the Hub collapses into a small
mini-bar at the top of the screen so it never covers what the agent clicks; both windows are excluded from
screen capture, so the agent's screenshots (and recordings) never contain them. The buddy's own question card
and "your turn" chip stay the primary place to answer; the Hub mirrors each request under "Needs you".

Runner contract (agent_graph.get_runner(), else agent.run_task): see rbot/hub/runner.py.
on_event dicts: task_started, thinking, action, step_result, needs_you, human_done, say, task_finished.
"""
import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")     # same physical-pixel space as the buddy overlay

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget  # noqa: E402

from rbot.hub import core  # noqa: E402
from rbot.hub.runner import HitlControls, TaskWorker  # noqa: E402
from rbot.hub.widgets import CTX, A1, A2, paint_face, px  # noqa: E402
from rbot.hub.views import HubWindow, MiniBar  # noqa: E402

from rbot import RUNS  # noqa: E402
HOTKEY_TEXT = "Ctrl+Alt+B"
_SHEET = False                                         # --sheet: windows are never put on screen


def use_offscreen():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))


# ---------------------------------------------------------------- win32 helpers
def exclude_from_capture(w):
    if _SHEET:
        return
    try:
        from rbot.human.hitl_ui import exclude_from_capture as ex
        ex(w, True)                                   # SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception as e:  # noqa: BLE001
        print(f"exclude_from_capture: {e}")


def screen_scale(screen):
    env = os.environ.get("HUB_SCALE")
    if env:
        return float(env)
    try:
        return max(1.0, round(screen.logicalDotsPerInch() / 96.0 * 4) / 4)
    except Exception:  # noqa: BLE001
        return 1.0


def open_path(p):
    try:
        os.startfile(str(p))                           # noqa: S606  (video / log folder, user clicked)
    except Exception as e:  # noqa: BLE001
        print(f"open {p}: {e}")


class GlobalHotkey(QObject):
    """Ctrl+Alt+B (Win+Shift+Space = Windows' input-language switch, Ctrl+Alt+Space is taken on this PC). RegisterHotKey, with a pynput
    keyboard listener as fallback (a separate listener from the Ctrl+Alt+Q kill switch, no conflict)."""
    activated = Signal()
    ID = 0xB0D1

    def __init__(self):
        super().__init__()
        self.how, self._filter, self._listener = "none", None, None
        if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.u32 = u32
        if u32.RegisterHotKey(None, self.ID, 0x0002 | 0x0001 | 0x4000, 0x42):   # CTRL|ALT|NOREPEAT, VK_B
            from PySide6.QtCore import QAbstractNativeEventFilter

            hk = self

            class _F(QAbstractNativeEventFilter):
                def nativeEventFilter(self, etype, message):
                    try:
                        msg = wintypes.MSG.from_address(int(message))
                        if msg.message == 0x0312 and msg.wParam == hk.ID:            # WM_HOTKEY
                            hk.activated.emit()
                            return True, 0
                    except Exception:  # noqa: BLE001
                        pass
                    return False, 0

            self._filter = _F()
            QApplication.instance().installNativeEventFilter(self._filter)
            self.how = "RegisterHotKey"
            return
        try:
            from pynput import keyboard
            self._listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+b": self.activated.emit})
            self._listener.daemon = True
            self._listener.start()
            self.how = "pynput"
        except Exception as e:  # noqa: BLE001
            print(f"hotkey unavailable: {e}")

    def close(self):
        if self.how == "RegisterHotKey":
            self.u32.UnregisterHotKey(None, self.ID)
        if self._listener is not None:
            self._listener.stop()


def tray_icon(state="idle", size=64):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    paint_face(p, QRectF(0, 0, size, size), A1, A2, ring=True)
    if state in ("running", "needs"):
        c = QColor("#34D399") if state == "running" else QColor("#FBBF24")
        r = size * 0.17
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 14, 30))
        p.drawEllipse(QPointF(size - r - 2, size - r - 2), r + size * 0.05, r + size * 0.05)
        p.setBrush(c)
        p.drawEllipse(QPointF(size - r - 2, size - r - 2), r, r)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- the controller
class HubController(QObject):
    """Owns the model, the worker, the windows. Every slot runs on the Qt thread; the worker's events arrive
    through queued signals (TaskWorker.bridge.event, HubHumanUI.mirror)."""
    DODGE_MARGIN = 40

    def __init__(self, settings, *, executor=None, cursor=None, ui=None, runner=None, engine=None,
                 history_path=core.HISTORY_PATH, record_fns=None, guard=None, human_loop_cls=None,
                 runs_dir=RUNS, tray=True, hotkey=True, autostart_reader=core.autostart_enabled,
                 autostart_writer=core.set_autostart, next_delay_ms=700):
        super().__init__()
        self.settings = settings
        self.cursor, self.ex, self.ui = cursor, executor, ui
        self.autostart_writer = autostart_writer
        self.next_delay_ms = next_delay_ms
        self.model = core.HubModel(max_steps=settings["max_steps"], history_path=history_path)
        self.hitl = HitlControls(ui)
        self.worker = TaskWorker(executor, cursor, ui, runner, engine, guard=guard, human_loop_cls=human_loop_cls,
                                 runs_dir=runs_dir, record_fns=record_fns)
        self.worker.bridge.event.connect(self.on_event)
        if ui is not None:
            ui.on_stop.append(self.worker.stop)      # the chip / card [Stop agent] stops the run (like chat_app)
            ui.should_stop = lambda: self.worker.stop_event.is_set() or bool(getattr(self.ex, "killed", False))
            if hasattr(ui, "mirror"):
                ui.mirror.connect(self.on_event)
        CTX.set_theme(settings["theme"])
        CTX.skin = settings["skin"]
        scr = QGuiApplication.primaryScreen()
        CTX.scale = screen_scale(scr)
        try:
            from rbot.human.hitl_ui import reduced_motion
            CTX.reduced = reduced_motion() if QGuiApplication.platformName() == "windows" else False
        except Exception:  # noqa: BLE001
            CTX.reduced = False
        self.autostart_on = bool(autostart_reader())
        self.collapsed = False
        self.dodge_until = 0.0
        self.win = HubWindow()
        self.mini = MiniBar()
        self._wire_window()
        self.win.build(settings.data, self.autostart_on, HOTKEY_TEXT)
        if not self.model.chat:
            self.model.say("buddy", "Hi! Tell me a task and I'll do it on your screen. "
                                    "I'll shrink to a small bar at the top while I work.")
        self.win.render(self.model, animate=False)
        g = scr.availableGeometry()
        self.win.move(g.right() - self.win.width() + px(4), g.bottom() - self.win.height() + px(4))
        for w in (self.win, self.mini):
            exclude_from_capture(w)
        self.win_scale = self.mini_scale = CTX.scale
        self.win.winId()
        self.win.windowHandle().screenChanged.connect(self._hub_screen_changed)
        self.dodge = QTimer(self)
        self.dodge.setInterval(50)
        self.dodge.timeout.connect(self._dodge_tick)
        self.tray = self._make_tray() if tray and QSystemTrayIcon.isSystemTrayAvailable() else None
        self.hotkey = GlobalHotkey() if hotkey else None
        if self.hotkey is not None:
            self.hotkey.activated.connect(self.toggle)
        self._first_close_hint = True

    # ---- wiring ----
    def _wire_window(self):
        w = self.win
        w.submit.connect(self.submit)
        w.stop.connect(self.stop)
        w.go_there.connect(self.go_there)
        w.done.connect(self.hitl.done)
        w.answer.connect(self.answer)
        w.remove_queued.connect(self.remove_queued)
        w.open_video.connect(open_path)
        w.open_folder.connect(open_path)
        w.setting.connect(self.change_setting)
        w.autostart.connect(self.toggle_autostart)
        w.hide_requested.connect(self.hide_hub)
        self.mini.stop.connect(self.stop)
        self.mini.expand.connect(self.restore)

    def _make_tray(self):
        t = QSystemTrayIcon(tray_icon("idle"))
        t.setToolTip("rbot-ai: idle")
        m = QMenu()
        self.act_open = QAction("Open rbot-ai", m)
        self.act_open.triggered.connect(self.restore)
        self.act_stop = QAction("Stop the current task", m)
        self.act_stop.triggered.connect(self.stop)
        self.act_stop.setEnabled(False)
        act_set = QAction("Settings", m)
        act_set.triggered.connect(lambda: (self.restore(), self.win.open_settings()))
        act_quit = QAction("Quit rbot-ai", m)
        act_quit.triggered.connect(self.quit)
        for a in (self.act_open, self.act_stop):
            m.addAction(a)
        m.addSeparator()
        m.addAction(act_set)
        m.addAction(act_quit)
        t.setContextMenu(m)
        t.activated.connect(lambda r: self.toggle() if r == QSystemTrayIcon.Trigger else None)
        self._tray_menu = m
        t.show()
        return t

    # ---- events from the worker / HumanUI mirror ----
    def on_event(self, ev):
        parts = self.model.apply(ev)
        if ev.get("type") == "task_finished":
            self._finished()
        self.refresh(parts)

    def refresh(self, parts=None):
        self.win.render(self.model, parts)
        busy = self.model.current is not None
        if busy:
            self.mini.show_state(self.model)
        handover = bool(self.model.needs and self.model.needs.get("handover"))
        if self.collapsed and busy:
            if handover and self.mini.isVisible():
                self.mini.dismiss()                   # the your-turn chip takes the same spot at the top
            elif not handover and not self.mini.isVisible() and time.time() >= self.dodge_until:
                self.present_mini()
        if self.tray is not None:
            state = "needs" if self.model.needs else "running" if busy else "idle"
            self.tray.setIcon(tray_icon(state))
            tip = {"idle": "rbot-ai: idle", "running": "rbot-ai: working", "needs": "rbot-ai: needs you"}[state]
            if busy:
                tip += f" (step {max(self.model.current.step, 1)} of {self.model.current.max_steps})"
            self.tray.setToolTip(tip)
            self.act_stop.setEnabled(busy)

    # ---- chat / tasks ----
    def submit(self, text):
        m = self.model
        m.say("user", text)
        kind = core.intent(text)
        if m.needs and m.needs.get("allow_text") and kind == "task":
            self.hitl.answer(text)
            m.say("buddy", "Got it, thanks!")
        elif kind == "status":
            m.say("buddy", m.status_line())
        elif kind == "stop":
            if m.current is not None:
                m.say("buddy", "Okay, stopping.")
                self.stop(quiet=True)
            else:
                m.say("buddy", "Nothing is running right now.")
        else:
            how, t = m.submit(text)
            if how == "start":
                m.say("buddy", "On it!")
                self.start(t)
            else:
                ahead = len(m.queue) - 1
                m.say("buddy", f'Okay, queued after "{core.one_line(m.current.text, 48)}"'
                               + (f" and {ahead} more." if ahead else "."))
        self.refresh({"chat", "queue", "running", "needs"})

    def start(self, task):
        s = self.settings
        self.worker.start(task.text, max_steps=s["max_steps"], record=bool(s["record_video"]),
                          keep_images=bool(s["keep_screenshots"]), mode=s["mode"])
        if self.cursor is not None:
            self.cursor.show()                        # the buddy is only on screen while a task runs
        self.dodge.start()
        if s["mode"] == "live":
            self.collapse()

    def _finished(self):
        if self.model.queue:
            QTimer.singleShot(self.next_delay_ms, self._next)
            return
        self.dodge.stop()
        if self.collapsed:
            self.restore()
        if self.cursor is not None:                   # after its "Done!" bubble, the buddy goes away
            QTimer.singleShot(2500, lambda: self.model.current is None and self.cursor.hide())

    def _next(self):
        if self.worker.busy:                          # the worker emits task_finished as its very last act
            QTimer.singleShot(100, self._next)
            return
        t = self.model.next_task()
        if t is None:
            if self.collapsed:
                self.restore()
            return
        self.model.say("buddy", f'Next: "{core.one_line(t.text, 60)}"')
        self.start(t)
        self.refresh({"running", "queue", "chat", "needs"})

    def stop(self, quiet=False):
        if self.model.current is None:
            return
        if not quiet:
            self.model.say("buddy", "Stopping…")
        self.hitl.stop()                              # HumanUI's own Stop path: closes cards, unparks the buddy
        self.worker.stop()
        self.refresh({"chat"})

    def answer(self, text):
        self.model.say("user", text)
        self.hitl.answer(text)
        self.refresh({"chat"})

    def remove_queued(self, task_id):
        if self.model.remove_queued(task_id):
            self.refresh({"queue"})

    def go_there(self):
        """Needs you -> [Go there]: get the Hub out of the way, then bring the question card or the window the
        agent was working in to the front."""
        def focus():
            if self.hitl.focus_card():
                return
            hwnd = (getattr(self.worker.hitl, "agent_win", None) or (None,))[0]
            if hwnd:
                try:
                    from rbot.human.human_loop import focus_window
                    focus_window(hwnd)
                except Exception as e:  # noqa: BLE001
                    print(f"go there: {e}")
        if self.model.current is not None:
            self.collapse(then=focus)
        else:
            focus()

    # ---- DPI: each window is built for the scale of the monitor it is on ----
    def _use_scale(self, which, screen):
        s = screen_scale(screen)
        CTX.scale = s
        if which == "mini" and abs(self.mini_scale - s) > 1e-3:
            self.mini_scale = s
            self.mini.build()
            self.mini.show_state(self.model)
        elif which == "hub" and abs(self.win_scale - s) > 1e-3:
            k = s / self.win_scale
            self.win_scale = s
            size = self.win.size()
            self.rebuild(mini=False)
            self.win.setMinimumSize(px(380) + 2 * self.win.margin(), px(560) + 2 * self.win.margin())
            self.win.resize(int(size.width() * k), int(size.height() * k))

    def _hub_screen_changed(self, screen):
        if self.win.isVisible() and screen is not None:
            self._use_scale("hub", screen)

    def present_mini(self):
        scr = QGuiApplication.primaryScreen()
        self._use_scale("mini", scr)
        self.mini.show_state(self.model)
        self.mini.present(scr)
        exclude_from_capture(self.mini)

    # ---- collapse / restore / show / hide ----
    def collapse(self, then=None):
        """Full Hub -> mini-bar (task running in Live mode)."""
        self.collapsed = True
        if self.win.drawer_open:
            self.win.close_settings()

        def after():
            handover = bool(self.model.needs and self.model.needs.get("handover"))
            if self.model.current is not None and not handover:
                self.present_mini()
            if then:
                then()
        self.win.fade_out(after)

    def restore(self):
        self.collapsed = False
        self.mini.dismiss()
        if self.win.isMinimized():
            self.win.showNormal()
        scr = self.win.screen() or QGuiApplication.primaryScreen()
        self._use_scale("hub", scr)
        self.win.fade_in()
        exclude_from_capture(self.win)

    def hide_hub(self):
        if self.model.current is not None:
            self.collapse()
            return
        self.win.fade_out()
        if self.tray is not None and self._first_close_hint:
            self._first_close_hint = False
            self.tray.showMessage("rbot-ai is still here", f"Click the tray icon or press {HOTKEY_TEXT} to open the Hub.",
                                  tray_icon("idle"), 4000)

    def toggle(self):
        if self.win.isVisible() and not self.win.isMinimized() and self.win.windowOpacity() > 0.5:
            self.hide_hub()
        else:
            self.restore()

    def _dodge_tick(self):
        """Keep both windows out from under the buddy (the real pointer follows it and would click them)."""
        cur = self.cursor
        if cur is None or self.model.current is None:
            return
        pts = [getattr(cur, "target", None), getattr(cur, "pos_", None)]
        pts = [QPoint(int(p.x()), int(p.y())) for p in pts if p is not None]
        if not pts:
            return
        m = px(self.DODGE_MARGIN)
        if self.mini.isVisible() and not self.mini.closing:
            zone = self.mini.geometry().adjusted(-m, -m, m, m)
            if any(zone.contains(p) for p in pts):
                self.mini.hide_now()
                self.dodge_until = time.time() + 1.5
        elif self.collapsed and not self.mini.isVisible() and time.time() >= self.dodge_until:
            zone = QRect(self.mini.pos(), self.mini.size()).adjusted(-m, -m, m, m)
            if not any(zone.contains(p) for p in pts):
                self.refresh(set())
        if self.win.isVisible() and not self.collapsed:
            if any(self.win.geometry().contains(p) for p in pts):
                self.collapse()

    # ---- settings ----
    def change_setting(self, key, value):
        self.settings.set(key, value)
        if key == "theme":
            CTX.set_theme(value)
            self.rebuild()
        elif key == "skin":
            CTX.skin = value
            apply_skin(self.cursor, value)
            for w in self.win.findChildren(QWidget):
                w.update()
            if self.tray is not None:
                self.refresh(set())

    def rebuild(self, mini=True):
        drawer = self.win.drawer_open
        self.win.build(self.settings.data, self.autostart_on, HOTKEY_TEXT)
        if mini:
            self.mini.build()
            self.mini_scale = CTX.scale
        self.win.render(self.model, animate=False)
        self.win.update()
        if drawer:
            self.win.drawer_open = False
            self.win.open_settings()
            self.win.slide.setCurrentTime(self.win.slide.duration())

    def toggle_autostart(self, on):
        if self.autostart_writer(on):
            self.autostart_on = on
        else:
            self.model.say("buddy", "I couldn't change the Windows startup setting.")
            self.refresh({"chat"})

    def quit(self):
        if self.model.current is not None:
            self.stop(quiet=True)
        if self.hotkey is not None:
            self.hotkey.close()
        if self.tray is not None:
            self.tray.hide()
        QApplication.instance().quit()


def apply_skin(cursor, name):
    if cursor is None:
        return
    from rbot.hub.widgets import SKINS
    c1, c2 = (QColor(c) for c in SKINS.get(name, SKINS["grape"]))
    cursor.c1, cursor.c2 = c1, c2
    cursor.limb_col, cursor.foot_col = QColor(c1).darker(122), QColor(c1).darker(190)
    cursor.update()


# ---------------------------------------------------------------- single instance
def already_running():
    """If a Hub is already running, ask it to show itself and return True."""
    from PySide6.QtNetwork import QLocalSocket
    s = QLocalSocket()
    s.connectToServer("rbot-ai-" + os.environ.get("USERNAME", "user"))
    if s.waitForConnected(300):
        s.write(b"show")
        s.flush()
        s.waitForBytesWritten(300)
        s.disconnectFromServer()
        return True
    return False


def serve_instance(ctrl):
    from PySide6.QtNetwork import QLocalServer
    name = "rbot-ai-" + os.environ.get("USERNAME", "user")
    QLocalServer.removeServer(name)
    srv = QLocalServer()
    srv.listen(name)
    srv.newConnection.connect(lambda: (srv.nextPendingConnection(), ctrl.restore()))
    return srv


# ---------------------------------------------------------------- entry points
def make_app():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("rbot-ai")
    return app


def main_live(demo=False):
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if not demo:
        from rbot.desktop import perception  # noqa: F401  (per-monitor DPI awareness before Qt)
    app = make_app()
    if already_running():
        print("rbot-ai is already running; showed it.")
        return
    from rbot.desktop.cursor_overlay import AgentCursor
    from rbot.hub.runner import make_hub_human_ui
    settings = core.Settings()
    cur = AgentCursor(theme=settings["skin"])
    ui = make_hub_human_ui(cur)
    if demo:
        from types import SimpleNamespace
        from rbot.hub.runner import DemoHitl, DemoRunner
        ex = SimpleNamespace(killed=False)                 # no mouse / keyboard hooks at all in the demo
        ctrl = HubController(settings, executor=ex, cursor=cur, ui=ui, runner=DemoRunner(), engine="demo",
                             human_loop_cls=DemoHitl, history_path=None, record_fns=None)
        cur.hide()
    else:
        import ctypes
        from rbot.desktop import perception
        from rbot.desktop.actions import Executor
        from rbot.apps.chat_app import general_guard, start_recording, stop_recording
        try:
            from rbot.human.human_loop import HumanLoop
        except Exception as e:  # noqa: BLE001
            print(f"human_loop unavailable: {e}")
            HumanLoop = None
        hw, aff = int(cur.winId()), ctypes.windll.user32.SetWindowDisplayAffinity
        aff(hw, 0)                                        # buddy visible in recordings, hidden from the agent
        perception.BEFORE_CAPTURE.append(lambda: (aff(hw, 0x11), time.sleep(0.06)))
        perception.AFTER_CAPTURE.append(lambda: aff(hw, 0))
        try:
            from rbot.desktop import mouse_gate
            gate = mouse_gate.make_gate(ui=ui, cursor=cur)   # real-mouse policy: idle 6 s, else ask
        except Exception as e:  # noqa: BLE001
            print(f"mouse gate unavailable ({e}); the agent uses the mouse without asking")
            gate = None
        ex = Executor(cursor=cur, gate=gate)
        from rbot.hub.guard import HubGuard
        box = {}                                        # the controller exists only after the guard is made
        guard = HubGuard(lambda: box["c"].worker.hitl if "c" in box else None,
                         lambda: settings["teams_allowed"])
        ctrl = HubController(settings, executor=ex, cursor=cur, ui=ui, guard=guard,
                             human_loop_cls=HumanLoop, record_fns=(start_recording, stop_recording))
        box["c"] = ctrl
        print(f"engine: {ctrl.worker.engine} · hotkey: {ctrl.hotkey.how if ctrl.hotkey else 'off'}")
        cur.hide()
    srv = serve_instance(ctrl)  # noqa: F841
    ctrl.win.fade_in()
    if demo:
        def queue_demo():
            from rbot.hub.runner import demo_scripts
            for text, _ in demo_scripts():
                ctrl.submit(text)
        QTimer.singleShot(1200, queue_demo)
    app.exec()
    if not demo:
        ex.stop()


# ---------------------------------------------------------------- offscreen screenshot sheet
def sheet(out_dir=RUNS, scale=None):
    """Render every state into runs/hub_*.png (+ a contact sheet) with the native renderer (real DirectWrite
    fonts) but WA_DontShowOnScreen: nothing appears on screen."""
    global _SHEET
    _SHEET = True
    app = make_app()
    from PySide6.QtGui import QFont, QLinearGradient
    import tempfile
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    if scale:
        os.environ["HUB_SCALE"] = str(scale)
    shots = []

    def ctrl_for(theme="dark"):
        s = core.Settings(tmp / f"s_{theme}.json")
        s.data["theme"] = theme
        c = HubController(s, history_path=None, runner=lambda *a, **k: ("done", ""), engine="fake", tray=False,
                          hotkey=False, autostart_reader=lambda: False, autostart_writer=lambda on: True)
        for w in (c.win, c.mini):
            w.setAttribute(Qt.WA_DontShowOnScreen)
        c.win.show()
        return c

    def freeze(c):
        for w in c.win.findChildren(QWidget) + c.mini.findChildren(QWidget):
            if hasattr(w, "frozen"):
                w.frozen = 0.35
                w.timer.stop()

    def backdrop(img_w, img_h, dark=True):
        pm = QPixmap(img_w, img_h)
        p = QPainter(pm)
        g = QLinearGradient(0, 0, img_w, img_h)
        if dark:
            g.setColorAt(0, QColor("#2B3A55"))
            g.setColorAt(1, QColor("#43304F"))
        else:
            g.setColorAt(0, QColor("#C9D6EA"))
            g.setColorAt(1, QColor("#E6D5EC"))
        p.fillRect(pm.rect(), g)
        p.end()
        return pm

    def grab(c, name, caption, widget=None, dark=True):
        w = widget or c.win
        for _ in range(6):                                # layout requests arrive as posted events, in passes
            app.processEvents()
            time.sleep(0.02)
        freeze(c)
        w.repaint()
        img = w.grab()
        pad = 24
        pm = backdrop(img.width() + 2 * pad, img.height() + 2 * pad, dark)
        p = QPainter(pm)
        p.drawPixmap(pad, pad, img)
        p.end()
        path = out_dir / f"hub_{name}.png"
        pm.save(str(path))
        shots.append((path, caption, pm))

    def seed_done(m):
        now = time.time()
        for i, (text, res, summ, steps, secs, vid) in enumerate([
                ("Move the PDFs in Downloads to my Reports folder", "done",
                 "Moved 12 PDFs from Downloads to Documents\\Reports", 2, 14, ""),
                ("Check the price of the grape mug", "fail", "step 2: every level failed (no change)", 3, 96, ""),
                ("Send Tharun the November report in Teams", "done", "Sent the report to Tharun K in Teams", 9, 141,
                 "C:/x/hub_1_SMOOTH.mp4"),
                ("Book a meeting room for Friday", "stopped", "stopped by the user", 4, 52, "")]):
            t = m.new_task(text)
            t.state, t.result, t.summary = "finished", res, core.plain_result_text(res, summ)
            t.steps, t.seconds, t.t_end, t.run_dir, t.video = steps, secs, now - 600 * (i + 1), "C:/x", vid
            m.done.append(t)

    def running(m, text, step, method, level, step_text, detail=""):
        how, t = m.submit(text)
        m.apply({"type": "action", "step": step, "level": level, "name": "click", "desc": detail,
                 "reason": step_text, "method": method})
        m.current.t_start = time.time() - 42
        return t

    # 1. idle / empty
    c = ctrl_for()
    grab(c, "idle", "Idle, nothing done yet: friendly empty states")
    # 2. running with steps
    c = ctrl_for()
    m = c.model
    m.say("user", "Download the November report from the Test Shop")
    m.say("buddy", "On it!")
    running(m, "Download the November report from the Test Shop", 4, "browser", 2, "Open the November report",
            'click "November 2026"')
    seed_done(m)
    m.done = m.done[:1]
    c.win.render(m, animate=False)
    grab(c, "running", "Running: live step, step 4 of 25, method + level badges, elapsed, Stop")
    # 3. needs you: login handover
    m.apply({"type": "needs_you", "kind": "login", "title": "Please sign in",
             "detail": "This page needs your login. Sign in and I'll carry on by myself", "source": "ui",
             "handover": True})
    m.needs["status"] = "I'll continue by myself once you're signed in"
    c.win.render(m, animate=False)
    grab(c, "needs_login", "Needs you: login handover (Go there / I'm done / Stop)")
    # 4. needs you: consent with options
    m.needs = None
    m.apply({"type": "needs_you", "kind": "consent", "title": "Your choice", "detail": "", "source": "runner"})
    m.apply({"type": "needs_you", "kind": "ask", "title": "This site asks about cookies. What should I do?",
             "options": ["Reject all", "Accept all", "Only necessary"], "allow_text": True, "source": "ui"})
    c.win.render(m, animate=False)
    grab(c, "needs_consent", "Needs you: consent question with options (mirrors the card next to the buddy)")
    # 5. queued
    c = ctrl_for()
    m = c.model
    running(m, "Download the November report from the Test Shop", 2, "browser", 1, "Go to the Reports page",
            'click "Reports"')
    for q in ("Rename it to Nov-2026.pdf and move it into Reports", "Send it to Tharun in Teams",
              "Find last week's sprint notes and open them"):
        m.submit(q)
        m.say("user", q)
    m.say("buddy", 'Okay, queued after "Download the November report from the Test Shop" and 2 more.')
    c.win.render(m, animate=False)
    grab(c, "queued", "Queued: tasks typed while one runs, with remove buttons")
    # 6. done list
    c = ctrl_for()
    m = c.model
    seed_done(m)
    m.say("user", "what are you doing?")
    m.say("buddy", m.status_line())
    c.win.render(m, animate=False)
    grab(c, "done", "Done today: ✓ done, ✗ failed, ■ stopped; video + log buttons")
    # 7. settings
    c.win.open_settings()
    c.win.slide.setCurrentTime(c.win.slide.duration())
    c.win.slide.stop()
    c.win._slide_frame(1.0)
    grab(c, "settings", "Settings drawer")
    c.win.drawer.sa.verticalScrollBar().setValue(10_000)
    grab(c, "settings_more", "Settings drawer, scrolled: Teams people, startup, hotkeys")
    # 8. mini-bar
    c = ctrl_for()
    m = c.model
    running(m, "Download the November report from the Test Shop", 4, "browser", 2,
            "Open the November report", 'click "November 2026"')
    c.mini.show_state(m)
    c.mini.show()
    grab(c, "minibar", "Mini-bar while a task runs (top-centre, always on top, never focused)", c.mini)
    m.apply({"type": "needs_you", "kind": "ask", "title": "This site asks about cookies. What should I do?",
             "options": ["Reject all"], "source": "ui", "allow_text": True})
    c.mini.show_state(m)
    grab(c, "minibar_needs", "Mini-bar, waiting for an answer", c.mini)
    # 9. light theme
    c = ctrl_for("light")
    m = c.model
    running(m, "Download the November report from the Test Shop", 4, "browser", 2, "Open the November report",
            'click "November 2026"')
    m.apply({"type": "needs_you", "kind": "login", "title": "Please sign in",
             "detail": "This page needs your login. Sign in and I'll carry on by myself", "source": "ui",
             "handover": True})
    seed_done(m)
    c.win.render(m, animate=False)
    grab(c, "light", "Light theme", dark=False)

    # contact sheet
    cols, gap = 4, 32
    big = [s for s in shots if "minibar" not in s[0].name]
    cw = max(s[2].width() for s in big)
    ch = max(s[2].height() for s in big)
    rows = (len(big) + cols - 1) // cols
    mini = [s for s in shots if "minibar" in s[0].name]
    W = cols * cw + (cols + 1) * gap
    H = rows * (ch + 40) + (rows + 1) * gap + sum(s[2].height() + 40 for s in mini)
    pm = QPixmap(W, H)
    pm.fill(QColor("#101014"))
    p = QPainter(pm)
    f = QFont("Segoe UI")
    f.setPixelSize(18)
    p.setFont(f)
    p.setPen(QColor("#DDD"))
    for i, (path, cap, img) in enumerate(big):
        x, y = gap + (i % cols) * (cw + gap), gap + (i // cols) * (ch + 40 + gap)
        p.drawText(x, y + 22, f"{path.stem}: {cap}"[:70])
        p.drawPixmap(x, y + 34, img)
    y = gap + rows * (ch + 40 + gap)
    for path, cap, img in mini:
        p.drawText(gap, y + 22, f"{path.stem}: {cap}")
        p.drawPixmap(gap, y + 34, img)
        y += img.height() + 40
    p.end()
    pm.save(str(out_dir / "hub_sheet.png"))
    for path, cap, _ in shots:
        print(f"{path}  {cap}")
    print(out_dir / "hub_sheet.png")


# ---------------------------------------------------------------- self-test
def selftest():
    use_offscreen()
    app = make_app()
    import tempfile
    from types import SimpleNamespace
    from rbot.hub.runner import FakeRunner
    fails = []

    def check(name, ok):
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    def wait(cond, t=8.0):
        end = time.time() + t
        while time.time() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    check("intent status", core.intent("What are you doing?") == "status")
    check("intent stop", core.intent("stop") == "stop")
    check("intent task", core.intent("stop the music in spotify") == "task")
    tmp = Path(tempfile.mkdtemp())
    s = core.Settings(tmp / "s.json")
    s.set("keep_screenshots", True)
    check("settings persist", core.Settings(tmp / "s.json")["keep_screenshots"] is True)
    script = [{"type": "action", "step": 1, "level": 1, "name": "click", "desc": "click", "reason": "Open it",
               "method": "browser"}, ("sleep", 0.05),
              {"type": "step_result", "step": 1, "level": 1, "ok": True, "desc": "", "detail": ""},
              ("finish", "done", "ok")]
    fr = FakeRunner(script, speed=1)
    c = HubController(s, executor=SimpleNamespace(killed=False), runner=fr, engine="fake", history_path=None,
                      tray=False, hotkey=False, autostart_reader=lambda: False, runs_dir=tmp, next_delay_ms=10)
    c.submit("first task")
    c.submit("second task")
    check("one at a time", c.model.current.text == "first task" and len(c.model.queue) == 1)
    check("collapsed while running", c.collapsed)
    check("both tasks finish in order", wait(lambda: len(c.model.done) == 2))
    check("done order", [t.text for t in c.model.done] == ["second task", "first task"])
    check("keep_images passed", fr.calls[0]["keep_images"] is True)
    check("restored after the queue drained", wait(lambda: not c.collapsed))
    print("selftest:", "OK" if not fails else f"{len(fails)} failed")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demo", action="store_true", help="play a scripted fake run (no agent, no input)")
    ap.add_argument("--sheet", nargs="?", const=str(RUNS), help="write state screenshots (offscreen)")
    ap.add_argument("--scale", type=float, help="UI scale for --sheet (default: 1.25)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.sheet:
        sheet(a.sheet, a.scale or 1.25)
        return
    main_live(demo=a.demo)


if __name__ == "__main__":
    main()
