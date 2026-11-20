"""Buddy Agent chat window: type what you want done, the grape buddy does it on your PC.

  python -m rbot.apps.chat_app

The window hides while the agent works (so it never covers what the agent needs to see) and comes back
with the result. Ctrl+Alt+Q = emergency stop. Tick "Record video" to get a smart-edited mp4 of the run.
Human in the loop: the buddy asks questions in a little card next to it, and hands the screen over to you
(login, captcha, ...) with a "your turn" chip at the top of the screen — [Stop agent] there stops the run.
"""
import ctypes
import html
import inspect
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from rbot.desktop import perception  # noqa: F401  (DPI awareness first)
from rbot.core import agent
from rbot.core import agent_graph
from rbot.core.agent import BadAction
from rbot.llm import MODEL
from rbot.human.hitl_ui import HumanUI

try:
    from rbot.human.human_loop import HumanLoop
except ImportError:
    HumanLoop = None
except Exception as _e:  # noqa: BLE001  (a broken human_loop.py must not take the chat app down)
    print(f"human_loop unavailable: {type(_e).__name__}: {_e}")
    HumanLoop = None
RUN_TASK_TAKES_HITL = "hitl" in inspect.signature(agent.run_task).parameters

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from rbot import ROOT, RUNS  # noqa: E402

# ---------------------------------------------------------------- light, general-purpose guard
RISKY = re.compile(r"\b(delete|remove|uninstall|format|purchase|buy( now)?|pay( now)?|place order|checkout|"
                   r"transfer|sign ?out|log ?out|shut ?down|restart|empty (recycle|trash))\b", re.I)
BLOCKED_APPS = re.compile(r"\b(cmd|command prompt|powershell|terminal|regedit|registry|control panel|settings|"
                          r"task manager|disk|format)\b", re.I)
BLOCKED_HOTKEYS = [{"alt", "f4"}, {"ctrl", "alt", "delete"}, {"win", "l"}, {"ctrl", "shift", "esc"}]


def general_guard(name, args, obs, marks):
    if name == "open_app" and BLOCKED_APPS.search(str(args.get("name", ""))):
        raise BadAction("BLOCKED: that app is not allowed for the agent")
    if name == "hotkey":
        keys = args.get("keys")
        keys = re.split(r"\s*\+\s*", keys) if isinstance(keys, str) else [str(k) for k in keys or []]
        if {k.lower() for k in keys} in BLOCKED_HOTKEYS:
            raise BadAction("BLOCKED: that key combination is not allowed")
    if name in ("click", "double_click", "right_click"):
        i = args.get("id")
        m = marks.get(int(i)) if str(i).isdigit() else None
        if m is None and args.get("x") is not None and args.get("y") is not None:   # a position click:
            try:                                                                 # which box is under it?
                W, H = obs["size"]
                px, py = float(args["x"]) * W / 1000, float(args["y"]) * H / 1000
                m = next((mk for mk in marks.values() if mk.get("area") == "systray"
                          and mk["box"][0] <= px <= mk["box"][2] and mk["box"][1] <= py <= mk["box"][3]), None)
            except (TypeError, ValueError, KeyError):
                m = None
        if m is not None and m.get("area") == "systray":
            raise BadAction(f"BLOCKED: '{m['name'][:40]}' is a Windows system icon (bottom-right corner), "
                            "not part of the task. Stay in the app you are working in.")
        if str(i).isdigit() and int(i) in marks:
            m = marks[int(i)]
            if RISKY.search(m["name"]):
                raise BadAction(f"BLOCKED: '{m['name'][:40]}' looks destructive or costs money")
            app = m["name"].split(" - ")[0].replace("​", "").strip()     # taskbar: 'Microsoft Edge - 1 running window'
            fg = perception.foreground_title().replace("​", "")
            if "running window" in m["name"] and app and app in fg:
                raise BadAction(f"BLOCKED: {app} is already in front; clicking its taskbar button would minimize it. "
                                f"Work in the window that is already open.")


# ---------------------------------------------------------------- recording helpers
def start_recording(path):
    return subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "gdigrab", "-framerate", "12",
                             "-draw_mouse", "1", "-i", "desktop", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                             str(path)], stdin=subprocess.PIPE)


def stop_recording(proc):
    try:
        proc.communicate(b"q", timeout=20)
    except Exception:  # noqa: BLE001
        proc.kill()


# ---------------------------------------------------------------- the window
STYLE = """
QWidget#root { background: #17121f; }
QLabel#title { color: white; font: 600 15px 'Segoe UI'; }
QLabel#sub { color: #a99bc4; font: 11px 'Segoe UI'; }
QTextBrowser { background: #1f1830; border: 1px solid #2f2545; border-radius: 12px; color: #eee;
               font: 13px 'Segoe UI'; padding: 6px; }
QLineEdit { background: #261d3a; border: 1px solid #4b3a70; border-radius: 18px; color: white;
            font: 13px 'Segoe UI'; padding: 8px 14px; }
QLineEdit:focus { border: 1px solid #a78bfa; }
QPushButton#send { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #8B5CF6, stop:1 #EC4899);
                   color: white; border: none; border-radius: 18px; font: 600 13px 'Segoe UI'; padding: 8px 18px; }
QPushButton#send:disabled { background: #3a3150; color: #888; }
QCheckBox { color: #c9bde0; font: 12px 'Segoe UI'; }
"""


class Bridge(QObject):
    finished = Signal(str, str)        # result kind, html summary


class ChatWindow(QWidget):
    def __init__(self, cursor, executor, ui=None):
        super().__init__(None, Qt.WindowStaysOnTopHint)
        self.cur, self.ex, self.ui = cursor, executor, ui
        self.stop = threading.Event()
        if ui is not None:                                 # chip / question card [Stop agent]
            ui.on_stop.append(self.stop_agent)
            ui.should_stop = lambda: self.stop.is_set() or self.ex.killed
        self.bridge = Bridge()
        self.bridge.finished.connect(self.on_finished)
        self.setObjectName("root")
        self.setWindowTitle("rbot-ai")
        self.setStyleSheet(STYLE)
        self.resize(440, 600)

        title = QLabel("rbot-ai")
        title.setObjectName("title")
        sub = QLabel(f"{MODEL.split('/')[-1]} · on-prem · Ctrl+Alt+Q = stop")
        sub.setObjectName("sub")
        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Tell me what to do…  e.g. open chatgpt.com and ask it for a joke")
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("send")
        self.send_btn.clicked.connect(self.send)
        self.record = QCheckBox("Record video")

        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addWidget(self.log, 1)
        lay.addLayout(row)
        lay.addWidget(self.record)
        self.bubble("agent", "Hi! I'm rbot-ai. Tell me a task and I'll do it on your screen — "
                             "I'll hide this window while I work.")

        g = QApplication.primaryScreen().availableGeometry()
        self.move(g.right() - self.width() - 20, g.bottom() - self.height() - 40)

    def bubble(self, who, text):
        if who == "user":
            h = (f"<div style='margin:8px 0 8px 60px; text-align:right'><span style='background:#7c3aed;"
                 f"color:white; padding:8px 12px; border-radius:12px'>{html.escape(text)}</span></div>")
        else:
            h = (f"<div style='margin:8px 60px 8px 0'><span style='background:#2b2240; color:#eee;"
                 f"padding:8px 12px; border-radius:12px'>{text}</span></div>")
        self.log.append(h)

    def send(self):
        task = self.input.text().strip()
        if not task or not self.send_btn.isEnabled():
            return
        self.input.clear()
        self.bubble("user", task)
        self.send_btn.setEnabled(False)
        self.input.setEnabled(False)
        QTimer.singleShot(400, lambda: self.start(task, self.record.isChecked()))

    def start(self, task, record):
        self.hide()                                       # get out of the agent's way
        self.cur.post("say", "On it!", False)
        threading.Thread(target=self.work, args=(task, record), daemon=True).start()

    def stop_agent(self):
        """[Stop agent] in the HITL chip / card (Qt thread): same effect as the Ctrl+Alt+Q kill switch."""
        self.stop.set()
        self.ex.killed = True

    def work(self, task, record):
        self.ex.killed = False
        self.stop.clear()
        if self.ui is not None:
            self.ui.reset()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = RUNS / f"agent_{stamp}"
        rec, raw = None, RUNS / f"chat_{stamp}.mp4"
        if record:
            rec = start_recording(raw)
            time.sleep(1.0)
        t0 = time.time()
        extra = {}
        if RUN_TASK_TAKES_HITL:
            hitl = None
            if HumanLoop is not None and self.ui is not None:
                try:
                    hitl = HumanLoop(self.cur, self.ui, self.ex, run_dir)
                except Exception as e:  # noqa: BLE001
                    print(f"HumanLoop init failed: {type(e).__name__}: {e}")
            extra["hitl"] = hitl
        try:
            kind, text = agent_graph.get_runner()(task, self.ex, self.cur, 25, stop=self.stop, run_dir=run_dir,
                                        guard=general_guard, **extra)
        except Exception as e:  # noqa: BLE001
            kind, text = "error", f"{type(e).__name__}: {e}"
        if self.ui is not None:
            self.ui.dismiss_all()                         # no question card / chip left behind
        video = ""
        if rec:
            time.sleep(1.0)
            stop_recording(rec)
            smooth = RUNS / f"chat_{stamp}_SMOOTH.mp4"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "smart_edit.py"), str(smooth),
                            f"{raw}|{task[:60]}"], capture_output=True)
            video = smooth if smooth.exists() else raw
        self.bridge.finished.emit(kind, self.summary(kind, text, run_dir, time.time() - t0, video))

    @staticmethod
    def summary(kind, text, run_dir, secs, video):
        icon = {"done": "✅", "fail": "⚠️", "killed": "🛑", "stopped": "🛑"}.get(kind, "⚠️")
        steps = []
        try:
            for line in (run_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines():
                n = json.loads(line)
                if n["ok"]:
                    a = n.get("args") or {}
                    detail = a.get("text") or a.get("url") or a.get("name") or a.get("key") or a.get("reason") or ""
                    steps.append(f"{n['step']}. {n['action']} <span style='color:#a99bc4'>"
                                 f"{html.escape(str(detail)[:50])}</span>")
        except Exception:  # noqa: BLE001
            pass
        out = f"{icon} <b>{kind}</b> — {html.escape(text)}<br><span style='color:#a99bc4'>{secs:.0f}s · " \
              f"{len(steps)} steps</span>"
        if steps:
            out += "<br>" + "<br>".join(steps[-12:])
        if video:
            out += f"<br>🎬 <a style='color:#f0abfc' href='file:///{Path(video).as_posix()}'>{Path(video).name}</a>"
        return out

    def on_finished(self, kind, summary):
        self.bubble("agent", summary)
        self.show()
        self.raise_()
        self.activateWindow()
        self.send_btn.setEnabled(True)
        self.input.setEnabled(True)
        self.input.setFocus()


def main():
    from rbot.desktop.actions import Executor
    from rbot.desktop.cursor_overlay import AgentCursor
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    cur = AgentCursor()
    cur.show()
    # buddy visible to recordings, hidden only while the agent takes its own screenshot
    hw, aff = int(cur.winId()), ctypes.windll.user32.SetWindowDisplayAffinity
    aff(hw, 0)
    perception.BEFORE_CAPTURE.append(lambda: (aff(hw, 0x11), time.sleep(0.06)))
    perception.AFTER_CAPTURE.append(lambda: aff(hw, 0))
    ex = Executor(cursor=cur)
    ui = HumanUI(cur)                                     # question card + handover chip (never in screenshots)
    win = ChatWindow(cur, ex, ui)
    win.show()
    app.exec()
    ex.stop()


if __name__ == "__main__":
    main()
