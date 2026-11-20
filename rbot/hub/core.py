"""Buddy Hub, the non-visual part: settings, the task/queue/needs-you model, event -> state mapping,
chat intents, autostart (HKCU Run) and small formatting helpers. No Qt in here, so it is easy to test.
"""
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rbot import DATA, ROOT

SETTINGS_PATH = DATA / "settings.json"
HISTORY_PATH = DATA / "history.jsonl"

# ---------------------------------------------------------------- settings
DEFAULTS = {
    "mode": "live",                 # live | background (background: web tasks run inside the buddy browser)
    "idle_seconds": 6,              # real-mouse policy (mouse_gate): go after this long idle, else ask
    "mouse_policy": "auto",         # auto | always_ask | ask_once_per_task (mouse_gate reads it)
    "skin": "grape",                # grape | mint | sunset
    "record_video": True,
    "keep_screenshots": False,      # -> runner keep_images
    "teams_allowed": [],            # people the buddy may message in Teams (stored)
    "theme": "dark",                # dark | light
    "max_steps": 25,
}
SKINS = ("grape", "mint", "sunset")


class Settings:
    """A dict of settings stored as JSON in data/settings.json. Unknown/invalid values fall back to defaults."""

    def __init__(self, path=SETTINGS_PATH):
        self.path = Path(path)
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        for k, v in raw.items() if isinstance(raw, dict) else ():
            if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                self.data[k] = v
        if self.data["skin"] not in SKINS:
            self.data["skin"] = "grape"
        if self.data["theme"] not in ("dark", "light"):
            self.data["theme"] = "dark"
        if self.data["mode"] not in ("live", "background"):
            self.data["mode"] = "live"
        self.data["idle_seconds"] = max(2, min(60, int(self.data["idle_seconds"])))
        self.data["teams_allowed"] = [str(p).strip() for p in self.data["teams_allowed"] if str(p).strip()]
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def __getitem__(self, k):
        return self.data[k]

    def set(self, k, v):
        if k not in DEFAULTS:
            raise KeyError(k)
        self.data[k] = v
        self.save()


# ---------------------------------------------------------------- autostart (HKCU\...\Run), only on toggle
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "rbot-ai"


def autostart_command():
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    # the Run key has no working folder, so put the project root on sys.path, then run `python -m rbot`
    boot = f"import sys, runpy; sys.path.insert(0, r'{ROOT}'); runpy.run_module('rbot', run_name='__main__')"
    return f'"{pyw if pyw.exists() else exe}" -c "{boot}"'


def _winreg(reg):
    if reg is not None:
        return reg
    import winreg
    return winreg


def autostart_enabled(reg=None):
    try:
        r = _winreg(reg)
        with r.OpenKey(r.HKEY_CURRENT_USER, RUN_KEY, 0, r.KEY_READ) as k:
            r.QueryValueEx(k, RUN_VALUE)
        return True
    except (OSError, ImportError):
        return False


def set_autostart(on, reg=None):
    """Write / remove the Run value. Only called when the user flips the toggle. -> True on success."""
    try:
        r = _winreg(reg)
        with r.OpenKey(r.HKEY_CURRENT_USER, RUN_KEY, 0, r.KEY_SET_VALUE) as k:
            if on:
                r.SetValueEx(k, RUN_VALUE, 0, r.REG_SZ, autostart_command())
            else:
                try:
                    r.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except (OSError, ImportError) as e:
        print(f"autostart: {e}")
        return False


# ---------------------------------------------------------------- formatting
def clock(seconds):
    """Elapsed time for the running card: 0:42, 12:05, 1:02:03."""
    s = max(0, int(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def duration(seconds):
    """Done list: 42s, 3m 05s, 1h 02m."""
    s = max(0, int(round(seconds or 0)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def one_line(text, n=200):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


RESULTS = {  # result -> (label, tone)   tone: ok | bad | neutral
    "done": ("Done", "ok"),
    "fail": ("Couldn't finish", "bad"),
    "max_steps": ("Ran out of steps", "bad"),
    "error": ("Something went wrong", "bad"),
    "stopped": ("Stopped", "neutral"),
    "killed": ("Stopped (emergency stop)", "neutral"),
}

METHODS = {"mouse": "Mouse", "browser": "Browser", "command": "Command", "uia": "Invisible click"}
# legacy loop: which method an action used (everything there is real mouse + keyboard)
MOUSE_ACTIONS = {"click", "double_click", "right_click", "type_text", "press_key", "scroll", "drag", "move",
                 "hotkey", "open_app", "open_url"}

NEEDS_TITLES = {"login": "Please sign in", "otp": "Verification code needed", "captcha": "Robot check",
                "payment": "Payment: your turn", "consent": "Your choice", "stuck": "Can you help?",
                "takeover": "You're in control", "ask": "Question for you", "mouse": "May I use the mouse?"}

SAY_MAP = {"thinking": "Thinking…", "thinking hard": "Thinking harder…", "taking a closer look": "Taking a closer look…"}


def plain_result_text(result, text):
    """The runner's message is written for logs; turn the common technical ones into plain words."""
    t = one_line(text, 240)
    low = t.lower()
    if result == "killed":
        return "You pressed the emergency stop (Ctrl+Alt+Q)."
    if result == "stopped":
        if "timeout" in low:
            return "I waited for you but got no answer, so I stopped."
        return "Stopped as you asked."
    if result == "max_steps":
        return "I used all my steps without finishing."
    if result == "fail" and "every level failed" in low:
        return "I got stuck on a step and couldn't get past it."
    if result == "error":
        return "Something broke on my side: " + t
    return t


# ---------------------------------------------------------------- chat intents (local only, no LLM)
STATUS_RX = re.compile(r"^(what('?s| is| are)? (are )?(you|u) (doing|up to)|what'?s (going on|happening|up)|"
                       r"status|progress|how'?s it going|where are you|are you done|are you (busy|working))$")
STOP_RX = re.compile(r"^(stop|stop it|stop now|cancel|cancel it|abort)$")


def intent(text):
    """-> 'status' | 'stop' | 'task'"""
    t = re.sub(r"[^\w\s']", "", str(text).lower()).strip()
    t = re.sub(r"\s+", " ", t)
    if STATUS_RX.match(t):
        return "status"
    if STOP_RX.match(t):
        return "stop"
    return "task"


# ---------------------------------------------------------------- the model
@dataclass
class Task:
    id: int
    text: str
    state: str = "queued"           # queued | running | finished
    created: float = field(default_factory=time.time)
    t_start: float = 0.0
    t_end: float = 0.0
    run_dir: str = ""
    step: int = 0
    max_steps: int = 25
    level: int = 1
    method: str = "none"
    step_text: str = ""
    step_detail: str = ""
    result: str = ""
    summary: str = ""
    steps: int = 0
    seconds: float = 0.0
    video: str = ""

    def to_json(self):
        return asdict(self)

    @property
    def tone(self):
        return RESULTS.get(self.result, ("", "bad"))[1]

    @property
    def result_label(self):
        return RESULTS.get(self.result, (self.result or "Finished", "bad"))[0]


class HubModel:
    """Single task at a time; the rest waits in the queue. apply(event) -> set of changed parts:
    running | needs | queue | done | chat."""

    def __init__(self, max_steps=25, history_path=None):
        self.current = None
        self.queue = []
        self.done = []
        self.needs = None               # {"kind","title","detail","options","allow_text","source","status"}
        self.chat = []                  # [(who, text)]   who: user | buddy
        self.max_steps = max_steps
        self._ids = 0
        self.history_path = Path(history_path) if history_path else None
        self.load_history()

    # ---- tasks ----
    def new_task(self, text):
        self._ids += 1
        return Task(self._ids, one_line(text, 500), max_steps=self.max_steps)

    def submit(self, text):
        """-> ('start', task) if nothing runs (caller starts it), else ('queued', task)."""
        t = self.new_task(text)
        if self.current is None:
            self.begin(t)
            return "start", t
        self.queue.append(t)
        return "queued", t

    def begin(self, task):
        task.state, task.t_start = "running", time.time()
        task.step_text = "Getting started…"
        task.max_steps = self.max_steps
        self.current = task
        self.needs = None
        return task

    def next_task(self):
        """After task_finished: pop and begin the next queued task (or None)."""
        if self.current is not None or not self.queue:
            return None
        return self.begin(self.queue.pop(0))

    def remove_queued(self, task_id):
        before = len(self.queue)
        self.queue = [t for t in self.queue if t.id != task_id]
        return len(self.queue) != before

    def say(self, who, text):
        self.chat.append((who, text))
        del self.chat[:-200]

    # ---- events ----
    def apply(self, ev):
        kind = ev.get("type")
        cur = self.current
        ch = set()
        if kind == "needs_clear":
            if self.needs is not None and (ev.get("source") in (None, self.needs.get("source"), "any")):
                self.needs = None
                ch.add("needs")
            return ch
        if kind == "needs_status":
            if self.needs is not None:
                self.needs["status"] = one_line(ev.get("text"), 90)
                ch.add("needs")
            return ch
        if cur is None:
            return ch                   # stray event after the task ended
        if kind == "task_started":
            cur.run_dir = str(ev.get("run_dir") or cur.run_dir)
            ch.add("running")
        elif kind == "thinking":
            cur.step = int(ev.get("step") or cur.step or 1)
            cur.level = int(ev.get("level") or cur.level or 1)
            cur.step_text = "Thinking harder…" if cur.level >= 2 else "Thinking…"
            cur.step_detail = ""
            ch.add("running")
        elif kind == "action":
            cur.step = int(ev.get("step") or cur.step or 1)
            cur.level = int(ev.get("level") or cur.level or 1)
            m = ev.get("method") or "none"
            cur.method = m if m in METHODS else cur.method if m == "none" and cur.method != "none" else m
            reason, desc = one_line(ev.get("reason"), 140), one_line(ev.get("desc"), 120)
            cur.step_text = reason or desc or cur.step_text
            cur.step_detail = desc if reason else ""
            ch.add("running")
        elif kind == "step_result":
            cur.step = max(cur.step, int(ev.get("step") or 0))
            cur.steps = max(cur.steps, cur.step)
            if not ev.get("ok"):
                cur.step_detail = "That didn't work, trying another way"
            ch.add("running")
        elif kind == "needs_you":
            self.set_needs(ev)
            ch |= {"needs", "chat"}
        elif kind == "human_done":
            if self.needs is not None:
                self.needs = None
                ch.add("needs")
            if ev.get("outcome") in ("done", "answered", "instructed", "retry"):
                self.say("buddy", "Thanks! Carrying on.")
                ch.add("chat")
        elif kind == "say":
            text = one_line(ev.get("text"), 140)
            low = text.lower().rstrip("…. ")
            if low in SAY_MAP:
                if ev.get("legacy") and cur.step < cur.steps + 1:
                    cur.step = cur.steps + 1        # legacy loop: thinking = the next step has begun
                cur.step_text, cur.step_detail = SAY_MAP[low], ""
                if low == "thinking hard":
                    cur.level = max(cur.level, 2)
                elif low == "thinking":
                    cur.level = 1
            elif text and low not in ("done!", "done", "stopped", "on it!"):
                cur.step_text = text
            ch.add("running")
        elif kind == "task_finished":
            ch |= self.finish(ev)
        return ch

    def set_needs(self, ev):
        """Merge a request. The runner says *what kind* it is (login, consent, ...); the HumanUI mirror adds
        the exact question and its options. Whichever arrives second fills in the other's gaps."""
        k = ev.get("kind") or "ask"
        new = {"kind": k, "title": one_line(ev.get("title") or NEEDS_TITLES.get(k, "Your turn"), 160),
               "detail": one_line(ev.get("detail"), 260), "options": [str(o) for o in ev.get("options") or []],
               "allow_text": bool(ev.get("allow_text", False)), "source": ev.get("source", "runner"),
               "handover": bool(ev.get("handover", False)), "status": ""}
        old = self.needs
        if old is not None and old.get("source") != new["source"]:
            if new["source"] == "ui":                       # keep the runner's kind, take the real question
                old.update(options=new["options"] or old["options"], allow_text=new["allow_text"],
                           handover=new["handover"] or old["handover"], source="ui")
                if new["options"] or new["allow_text"]:
                    old["detail"] = old["detail"] if old["title"] == new["title"] else new["title"]
                    line = ("buddy", f"I need you: {old['title']}")
                    if self.chat and self.chat[-1] == line:          # say the real question instead
                        self.chat[-1] = ("buddy", f"I need you: {new['title']}")
                return
            if old.get("kind") == "ask" and k != "ask":      # runner tells us the kind after the fact
                old["kind"] = k
                return
        if k == "ask" and any(o.lower().startswith("i'll do it") for o in new["options"]):
            new["kind"] = "stuck"
        self.needs = new
        if not self.chat or self.chat[-1] != ("buddy", f"I need you: {new['title']}"):
            self.say("buddy", f"I need you: {new['title']}")

    def finish(self, ev):
        cur = self.current
        if cur is None:
            return set()
        cur.state, cur.t_end = "finished", time.time()
        cur.result = ev.get("result") or "error"
        cur.summary = plain_result_text(cur.result, ev.get("text", ""))
        cur.steps = int(ev.get("steps") or cur.steps or cur.step or 0)
        cur.seconds = float(ev.get("seconds") or (cur.t_end - cur.t_start))
        cur.run_dir = str(ev.get("run_dir") or cur.run_dir)
        cur.video = str(ev.get("video") or "")
        self.done.insert(0, cur)
        self.current, self.needs = None, None
        label = cur.result_label
        self.say("buddy", f"{label}: {cur.summary}" if cur.result != "done" else f"Done! {cur.summary}")
        self.save_history(cur)
        return {"running", "needs", "done", "chat"}

    # ---- "what are you doing?" ----
    def status_line(self):
        cur = self.current
        if cur is None:
            q = f" {len(self.queue)} task(s) waiting." if self.queue else ""
            return "Nothing right now. Tell me what to do!" + q
        s = f'Working on "{one_line(cur.text, 60)}", step {max(cur.step, 1)} of {cur.max_steps}: {cur.step_text}'
        if self.needs:
            s += f" I'm waiting for you: {self.needs['title']}."
        if self.queue:
            s += f" Next up: {len(self.queue)} more."
        return s

    # ---- done-today history (survives a restart) ----
    def save_history(self, task):
        if not self.history_path:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(task.to_json(), ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"history: {e}")

    def load_history(self):
        if not self.history_path or not self.history_path.exists():
            return
        today = time.strftime("%Y-%m-%d")
        try:
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                d = json.loads(line)
                if time.strftime("%Y-%m-%d", time.localtime(d.get("t_end") or 0)) == today:
                    t = Task(**{k: v for k, v in d.items() if k in Task.__dataclass_fields__})
                    self.done.insert(0, t)
                    self._ids = max(self._ids, t.id)
        except (OSError, ValueError, TypeError) as e:
            print(f"history: {e}")
