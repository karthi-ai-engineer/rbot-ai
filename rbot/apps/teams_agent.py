"""Run the cursor agent inside Microsoft Teams, locked to an allow-list of chats.

  python -m rbot.apps.teams_agent "send the message 'hello' in this chat"
  python -m rbot.apps.teams_agent --chat "Muthiah Karthi (You)" --chat "Appireddy Tharun" --record demo.mp4 "..."

Hard guard (code, not prompt):
- typing/keys only while the Teams window title shows an allowed chat (exact title segment)
- no hotkeys, no open_app
- clicks: never on whole-window elements, risky buttons (call/video/meet/share/delete/...), or any chat
  item that is not on the allow-list (group chats are not allowed even if they contain an allowed name);
  checked on what is REALLY under the click point at click time.
"""
import argparse
import ctypes
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from rbot.desktop import perception  # noqa: F401  (DPI awareness)
from rbot.core import agent
from rbot.core import agent_graph
from rbot.core.agent import BadAction

DANGER = re.compile(r"\b(call|video|meet|meeting|share|sharing|delete|leave|remove|block|record|join|invite|"
                    r"add people|participants?|mute|hang ?up|camera|mic|present|forward|pin)\b", re.I)
SAFE_KEYS = {"enter", "backspace", "esc", "escape", "tab", "space", "left", "right", "up", "down", "home", "end",
             "delete"}


# ---------------------------------------------------------------- allow-list matching
def variants(chats):
    """'Appireddy Tharun' -> also 'Tharun Appireddy' (Teams name order varies)."""
    out = []
    for c in chats:
        out.append(c)
        words = c.split()
        if len(words) == 2 and "(" not in c:
            out.append(f"{words[1]} {words[0]}")
    return out


def title_ok(title, names):
    return "Microsoft Teams" in title and any(seg.strip() in names for seg in title.split("|"))


def item_ok(text, names):
    """A chat-list / search-result item is OK only if it is a 1:1 item for an allowed name
    (not a group chat that merely contains the name)."""
    for n in names:
        if re.search(r"(?:^|\s)(?:Chat|Person) " + re.escape(n) + r"(?!\s*,)", text):  # 1:1 chat item / search result
            return True
        if text.startswith(n) and not re.match(re.escape(n) + r"\s*,", text):   # search result / header item
            return True
    return False


# ---------------------------------------------------------------- window helpers
def find_teams():
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        for w in auto.GetRootControl().GetChildren():
            if "Microsoft Teams" in (w.Name or ""):
                return w.NativeWindowHandle, w.Name
    return None, None


def focus(hwnd):
    """Bring a window to the front despite Windows' focus-stealing protection (tries 3 tricks)."""
    from pynput.keyboard import Controller, Key
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
    for attempt in range(3):
        if attempt == 0:                   # 1) a synthetic Alt tap makes us the "last input" process
            kb = Controller()
            kb.press(Key.alt)
            kb.release(Key.alt)
            u.SetForegroundWindow(hwnd)
        elif attempt == 1:                 # 2) borrow the current foreground thread's input state
            fg_thread = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
            me = k.GetCurrentThreadId()
            u.AttachThreadInput(me, fg_thread, True)
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
            u.AttachThreadInput(me, fg_thread, False)
        else:                              # 3) the shell's own window switcher
            u.SwitchToThisWindow(hwnd, True)
        time.sleep(0.8)
        if u.GetForegroundWindow() == hwnd:
            return True
    return False


# ---------------------------------------------------------------- the guard
FILE_TOOLS = {"list_files", "find_files", "move_files", "copy_files", "rename_file", "make_folder",
              "zip_files", "open_file", "delete_files"}


def make_guard(chats):
    names = variants(chats)

    def guard(name, args, obs, marks):
        title = perception.foreground_title()
        in_teams = "Microsoft Teams" in title
        if name == "open_app" and "teams" not in str(args.get("name", "")).lower():
            raise BadAction("BLOCKED: only Microsoft Teams may be opened in this test")
        if name == "hotkey":
            raise BadAction("BLOCKED: hotkey is not allowed in this Teams test")
        if name in FILE_TOOLS:
            raise BadAction("BLOCKED: file commands are not allowed in this Teams test")
        if name in ("type_text", "press_key"):
            if not title_ok(title, names):
                raise BadAction(f"BLOCKED: typing is only allowed while an allowed chat ({', '.join(chats)}) is open "
                                f"(window now: '{title[:70]}'). Open an allowed chat first.")
            if name == "press_key" and str(args.get("key", "")).lower() not in SAFE_KEYS:
                raise BadAction(f"BLOCKED: key '{args.get('key')}' not allowed")
        if name in ("click", "double_click", "right_click", "drag"):
            for p in (("from_", "to_") if name == "drag" else ("",)):
                i = args.get(p + "id")
                if i not in (None, "") and str(i).isdigit() and int(i) in marks:
                    m = marks[int(i)]
                    if m["type"] in ("Document", "Pane", "Window") or (
                            (m["box"][2] - m["box"][0]) * (m["box"][3] - m["box"][1]) > 0.25 * obs["size"][0] * obs["size"][1]):
                        raise BadAction(f"BLOCKED: #{i} is a whole window/page; click a specific element instead")
                    if DANGER.search(m["name"]):
                        raise BadAction(f"BLOCKED: element #{i} '{m['name'][:50]}' is a risky button")
                    if m["type"] in ("TreeItem", "ListItem") and in_teams and not item_ok(m["name"], names):
                        raise BadAction(f"BLOCKED: element #{i} is a chat/item not on the allow-list ({', '.join(chats)})")
                x, y = agent.resolve(args, marks, obs["size"], p)          # id or 0-1000 coords -> px
                ox, oy = obs.get("origin", (0, 0))
                hit_check(x + ox, y + oy, names)
    return guard


def hit_check(x, y, names):
    """The real safety net: what is ACTUALLY under the click point right now?"""
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        c = auto.ControlFromPoint(int(x), int(y))
        for _ in range(15):                              # the element and its parents (Teams nests deep)
            if c is None:
                break
            kind, name = c.ControlTypeName, (c.Name or "")
            if kind in ("TreeItemControl", "ListItemControl") and name and not item_ok(name, names):
                raise BadAction("BLOCKED: that point is on a chat/item that is not on the allow-list")
            if DANGER.search(name) and kind in ("ButtonControl", "MenuItemControl", "SplitButtonControl"):
                raise BadAction(f"BLOCKED: that point is a risky button ({name[:30]})")
            c = c.GetParentControl()


# ---------------------------------------------------------------- screen recording
def start_recording(path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "gdigrab", "-framerate", "12", "-draw_mouse", "1",
           "-i", "desktop", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "26", "-pix_fmt", "yuv420p", str(path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def stop_recording(proc):
    try:
        proc.communicate(b"q", timeout=20)                # ffmpeg finalises the mp4 on 'q'
    except Exception:  # noqa: BLE001
        proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--chat", action="append", help="allowed chat (repeatable); default: your self-chat")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--record", help="save a screen recording (mp4) of the run here")
    a = ap.parse_args()
    chats = a.chat or ["Muthiah Karthi (You)"]

    hwnd, title = find_teams()
    print(f"teams window: {title!r}" if hwnd else "Teams is NOT running - the agent has to open it itself",
          flush=True)
    print(f"allowed chats: {chats}", flush=True)

    task = ((f"Microsoft Teams is currently NOT open: first open it with open_app('Microsoft Teams'), then use "
             f"wait (about 8 seconds) until it has fully loaded. " if not hwnd else "") +
            f"You are working in the Microsoft Teams desktop app. You may ONLY use these chats: "
            f"{', '.join(repr(c) for c in chats)}. Never open, read or message any other chat (including group "
            f"chats), and never start calls or meetings. To open a chat that is not visible in the list, use the "
            f"search box at the top of Teams: click it, type the person's name, then pick that person from the "
            f"results. Each chat has a message box at the bottom ('Type a message'); click it, type, then press "
            f"Enter or click Send. Task: {a.task}")

    import signal
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from rbot.desktop.actions import Executor
    from rbot.desktop.cursor_overlay import AgentCursor

    app = QApplication(sys.argv)
    cur = AgentCursor()
    cur.show()
    rec = None
    if a.record:
        # Visible to the recorder, hidden only while the agent itself takes a screenshot.
        hw = int(cur.winId())
        aff = ctypes.windll.user32.SetWindowDisplayAffinity
        aff(hw, 0)
        perception.BEFORE_CAPTURE.append(lambda: (aff(hw, 0x11), time.sleep(0.06)))
        perception.AFTER_CAPTURE.append(lambda: aff(hw, 0))
        Path(a.record).parent.mkdir(parents=True, exist_ok=True)
        rec = start_recording(a.record)
        print(f"recording -> {a.record}", flush=True)
        time.sleep(1.0)
    if hwnd:
        print("focused:", focus(hwnd), flush=True)
    ex = Executor(cursor=cur)
    stop = threading.Event()
    out = {}

    def work():
        out["result"] = agent_graph.get_runner()(task, ex, cur, a.max_steps, stop=stop, guard=make_guard(chats))

    worker = threading.Thread(target=work, daemon=True)
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    poll = QTimer()
    poll.timeout.connect(lambda: worker.is_alive() or app.quit())
    poll.start(200)
    worker.start()
    app.exec()
    ex.stop()
    if rec:
        time.sleep(1.0)
        stop_recording(rec)
        print(f"recording saved: {a.record}", flush=True)
    print("RESULT:", out.get("result"), flush=True)


if __name__ == "__main__":
    main()
