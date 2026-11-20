"""Buddy Hub, the runner side: find the agent loop, run one task in a worker thread, turn everything that
happens into Hub events (dicts, see the contract in rbot/hub/app.py), record + smart-edit the video.

- agent_graph.get_runner() is used when agent_graph.py exists (it emits on_event itself and takes keep_images);
  otherwise agent.run_task is used and the events are synthesised: task_started / task_finished here, live steps
  from the cursor's speech + steps.jsonl, and screenshots are deleted afterwards unless keep_images.
- HubHumanUI is the normal HumanUI (question card / your-turn chip near the buddy, still the primary place to
  answer) that additionally mirrors every request into the Hub's "Needs you" section.
"""
import inspect
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from rbot import ROOT, RUNS


# ---------------------------------------------------------------- which loop
def accepts(fn, name):
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind == p.VAR_KEYWORD for p in ps.values())


def resolve_runner():
    """-> (run_task, engine)  engine: 'graph' | 'legacy'."""
    try:
        from rbot.core import agent_graph  # noqa: F401
    except ModuleNotFoundError as e:
        if e.name != "rbot.core.agent_graph":
            print(f"agent_graph import failed ({e}); using the legacy loop")
    except Exception as e:  # noqa: BLE001  (a half-written agent_graph.py must not take the Hub down)
        print(f"agent_graph broken ({type(e).__name__}: {e}); using the legacy loop")
    else:
        try:
            return agent_graph.get_runner(), "graph"
        except Exception as e:  # noqa: BLE001
            print(f"agent_graph.get_runner() failed ({type(e).__name__}: {e}); using the legacy loop")
    from rbot.core import agent
    return agent.run_task, "legacy"


# ---------------------------------------------------------------- HumanUI that mirrors into the Hub
def make_hub_human_ui(cursor):
    """HumanUI subclass created lazily (hitl_ui pulls in Qt widgets)."""
    from rbot.human.hitl_ui import HumanUI

    try:
        from rbot.human.human_loop import TITLES
    except Exception:  # noqa: BLE001
        TITLES = {}
    kind_of = {v: k for k, v in TITLES.items()}

    class HubHumanUI(HumanUI):
        mirror = Signal(object)                    # emitted from any thread, delivered on the Qt thread

        def ask(self, question, options=None, allow_text=True, timeout_s=300, mood="calling"):
            opts = [str(o).strip() for o in (options or []) if str(o).strip()]
            self.mirror.emit({"type": "needs_you", "kind": "ask", "title": str(question).strip(),
                              "options": opts, "allow_text": bool(allow_text) or not opts, "source": "ui"})
            try:
                return super().ask(question, options, allow_text, timeout_s, mood)
            finally:
                self.mirror.emit({"type": "needs_clear", "source": "ui"})

        def handover(self, title, detail, park_text="Your turn — click me when done"):
            self.mirror.emit({"type": "needs_you", "kind": kind_of.get(title, "takeover"), "title": title,
                              "detail": detail, "handover": True, "source": "ui"})
            super().handover(title, detail, park_text)

        def status(self, text):
            self.mirror.emit({"type": "needs_status", "text": text})
            super().status(text)

        def end_handover(self, thanks=True):
            self.mirror.emit({"type": "needs_clear", "source": "ui"})
            super().end_handover(thanks)

        def dismiss_all(self):
            self.mirror.emit({"type": "needs_clear", "source": "any"})
            super().dismiss_all()

    return HubHumanUI(cursor)


class HitlControls:
    """What the Hub's Needs-you buttons do. All called on the Qt thread, which is where HumanUI's own
    button handlers run, so we reuse exactly those (same visuals: chip says "Got it", buddy unparks)."""

    def __init__(self, ui):
        self.ui = ui

    def done(self):
        if self.ui is None:
            return
        fn = getattr(self.ui, "_on_chip_done", None)
        fn() if fn else self.ui.done_clicked.set()

    def answer(self, text):
        if self.ui is not None:
            self.ui.answer(text)                      # thread-safe public API

    def stop(self):
        if self.ui is None:
            return False
        fn = getattr(self.ui, "_on_stop", None)
        if fn:
            fn()                                      # sets stop_clicked, runs ui.on_stop, closes cards
        else:
            self.ui.stop_clicked.set()
        return True

    def focus_card(self):
        """Bring the open question card forward (it sits next to the buddy). -> True if there was one."""
        p = getattr(self.ui, "popup", None)
        if p is not None and p.isVisible():
            try:
                from rbot.human.hitl_ui import take_focus
                p.prev_fg = p.prev_fg or take_focus(p)
                p.focus_input()
            except Exception:  # noqa: BLE001
                p.raise_()
            return True
        return False


# ---------------------------------------------------------------- legacy loop: synthesise live events
class CursorTap:
    """Wraps the buddy so the legacy loop's speech ("Thinking", "Open the Reports page") becomes say events."""

    def __init__(self, cursor, emit):
        self._cur, self._emit = cursor, emit

    def post(self, name, *args):
        if name == "say" and args:
            self._emit({"type": "say", "text": str(args[0]), "legacy": True})
        if self._cur is not None:
            self._cur.post(name, *args)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __bool__(self):
        return True


class StepsTail(threading.Thread):
    """Follows run_dir/steps.jsonl (legacy loop) and turns finished attempts into action + step_result events."""

    def __init__(self, path, emit):
        super().__init__(daemon=True)
        self.path, self.emit, self.halt, self.pos = Path(path), emit, threading.Event(), 0

    def run(self):
        while not self.halt.wait(0.4):
            self.poll()
        self.poll()

    def poll(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                f.seek(self.pos)
                chunk = f.read()
                if not chunk.endswith("\n"):
                    chunk = chunk[:chunk.rfind("\n") + 1]
                self.pos += len(chunk.encode("utf-8"))
        except OSError:
            return
        for line in chunk.splitlines():
            try:
                n = json.loads(line)
            except ValueError:
                continue
            step, level, action = n.get("step"), n.get("level") or 1, n.get("action") or ""
            if n.get("type") == "human" or not action:
                continue
            args = n.get("args") or {}
            desc = f"{action} " + str(args.get("text") or args.get("url") or args.get("name") or args.get("key")
                                       or (f"#{args['id']}" if args.get("id") is not None else "")).strip()
            method = "mouse" if action in MOUSE_ACTIONS else "none"
            self.emit({"type": "action", "step": step, "level": level, "name": action, "desc": desc.strip(),
                       "reason": n.get("reason") or "", "method": method})
            self.emit({"type": "step_result", "step": step, "level": level, "ok": bool(n.get("ok")),
                       "desc": desc, "detail": n.get("fail_reason") or "", "latency_s": n.get("latency_s"),
                       "diff": n.get("diff")})


from rbot.hub.core import MOUSE_ACTIONS  # noqa: E402


def delete_images(run_dir):
    n = 0
    for p in Path(run_dir).rglob("*"):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp") and p.is_file():
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------- the worker
class Bridge(QObject):
    event = Signal(object)                 # emitted from the worker thread -> queued to the Qt thread


class TaskWorker:
    """Runs ONE task in a daemon thread. Create on the Qt thread; connect bridge.event to the Hub."""

    def __init__(self, executor, cursor=None, ui=None, runner=None, engine=None, *, guard=None,
                 human_loop_cls=None, runs_dir=RUNS, record_fns=None, smart_edit=True):
        if runner is None:
            runner, engine = resolve_runner()
        self.runner, self.engine = runner, engine or "custom"
        self.ex, self.cur, self.ui = executor, cursor, ui
        self.guard, self.HumanLoop = guard, human_loop_cls
        self.runs = Path(runs_dir)
        self.record_fns, self.smart_edit = record_fns, smart_edit
        self.bridge = Bridge()
        self.stop_event = threading.Event()
        self.thread = None
        self.native_events = accepts(runner, "on_event")
        self.takes_keep = accepts(runner, "keep_images")
        self.takes_hitl = accepts(runner, "hitl")
        self.takes_mode = accepts(runner, "mode")
        self.hitl = None
        self.last_kwargs = None

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def emit(self, ev):
        self.bridge.event.emit(dict(ev))

    def start(self, task, *, max_steps=25, record=False, keep_images=False, mode="live"):
        if self.busy:
            raise RuntimeError("a task is already running")
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._work, args=(task, max_steps, record, keep_images, mode),
                                       name="hub-task", daemon=True)
        self.thread.start()

    def stop(self):
        """Hub [Stop] (Qt thread): same effect as chat_app's stop_agent / the kill switch."""
        self.stop_event.set()
        if self.ex is not None:
            self.ex.killed = True

    # ---- worker thread ----
    def _work(self, task, max_steps, record, keep_images, mode="live"):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = self.runs / f"agent_{stamp}"
        t0 = time.time()
        if self.ex is not None:
            self.ex.killed = False
        if self.ui is not None:
            self.ui.reset()
        self.emit({"type": "task_started", "task": task, "run_dir": str(run_dir), "t": t0})
        held = {}

        def on_event(ev):
            ev = dict(ev)
            if ev.get("type") == "task_finished":         # hold it: video + cleanup first, then forward
                held.update(ev)
                return
            self.emit(ev)

        kwargs = {"stop": self.stop_event, "run_dir": run_dir}
        if self.guard is not None:
            if hasattr(self.guard, "new_task"):
                self.guard.new_task()       # approvals last one task
            kwargs["guard"] = self.guard
        if self.takes_hitl:
            hitl = None
            if self.HumanLoop is not None and self.ui is not None:
                try:
                    hitl = self.HumanLoop(self.cur, self.ui, self.ex, run_dir)
                except Exception as e:  # noqa: BLE001
                    print(f"HumanLoop init failed: {type(e).__name__}: {e}")
            kwargs["hitl"] = self.hitl = hitl
        if self.native_events:
            kwargs["on_event"] = on_event
        if self.takes_keep:
            kwargs["keep_images"] = bool(keep_images)
        if self.takes_mode:
            kwargs["mode"] = mode if mode in ("live", "background") else "live"
        cursor, tail = self.cur, None
        if not self.native_events:
            cursor = CursorTap(self.cur, self.emit)
            tail = StepsTail(run_dir / "steps.jsonl", self.emit)
            tail.start()
        self.last_kwargs = kwargs
        rec = raw = None
        if record and self.record_fns:
            raw = self.runs / f"hub_{stamp}.mp4"
            try:
                self.runs.mkdir(parents=True, exist_ok=True)
                rec = self.record_fns[0](raw)
                time.sleep(1.0)
            except Exception as e:  # noqa: BLE001
                print(f"recording failed to start: {e}")
                rec = None
        try:
            result, text = self.runner(task, self.ex, cursor, max_steps, **kwargs)
        except Exception as e:  # noqa: BLE001
            result, text = "error", f"{type(e).__name__}: {e}"
        finally:
            if tail is not None:
                tail.halt.set()
                tail.join(2)
            if self.ui is not None:
                try:
                    self.ui.dismiss_all()                  # no question card / chip left behind
                except Exception:  # noqa: BLE001
                    pass
        video = ""
        if rec is not None:
            time.sleep(1.0)
            self.record_fns[1](rec)
            video = str(raw) if raw.exists() else ""
            if self.smart_edit and video:
                smooth = raw.with_name(raw.stem + "_SMOOTH.mp4")
                subprocess.run([sys.executable, str(ROOT / "scripts" / "smart_edit.py"), str(smooth),
                                f"{raw}|{task[:60]}"], capture_output=True)
                video = str(smooth) if smooth.exists() else video
        if not self.takes_keep and not keep_images and run_dir.exists():
            delete_images(run_dir)                        # legacy loop: emulate keep_images=False
        steps, seconds = held.get("steps"), held.get("seconds")
        if steps is None:
            try:
                s = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                steps, seconds = s.get("steps"), s.get("total_time_s")
            except (OSError, ValueError):
                pass
        self.emit({"type": "task_finished", "result": held.get("result") or result,
                   "text": held.get("text") or text, "steps": steps or 0,
                   "seconds": seconds or round(time.time() - t0, 1), "run_dir": str(held.get("run_dir") or run_dir),
                   "video": video})


# ---------------------------------------------------------------- scripted fake runner (--demo, tests)
def _sleep(stop, s):
    return stop is not None and stop.wait(s)


class FakeRunner:
    """Same call shape as run_task(..., on_event=None, keep_images=False). Plays a script of events.
    script: list of event dicts and ("sleep", s) / ("handover", kind, title, detail) / ("ask", kind, q, opts)
    items; the last item may be ("finish", result, text). With hitl.ui set, handovers/asks really go through
    HumanUI and wait for the user (demo); without, they just emit events (tests)."""

    def __init__(self, script, speed=1.0, ui_timeout=25):
        self.script, self.speed, self.ui_timeout = script, speed, ui_timeout
        self.calls, self.answers = [], []

    def __call__(self, task, executor, cursor=None, max_steps=25, *, stop=None, run_dir=None, hitl=None,
                 on_event=None, keep_images=False, **kw):
        self.calls.append({"task": task, "max_steps": max_steps, "keep_images": keep_images, "kw": kw,
                           "run_dir": run_dir})
        emit = on_event or (lambda ev: None)
        ui = getattr(hitl, "ui", None)
        t0, steps, result = time.time(), 0, ("done", "finished")
        emit({"type": "task_started", "task": task, "run_dir": str(run_dir), "t": t0})
        say = (lambda text: cursor.post("say", text, False)) if cursor is not None else (lambda text: None)
        for item in self.script:
            if (stop is not None and stop.is_set()) or getattr(executor, "killed", False):
                result = ("killed" if getattr(executor, "killed", False) else "stopped", "stopped by the user")
                break
            if isinstance(item, dict):
                ev = dict(item)
                if ev.get("type") == "action" and ev.get("reason"):
                    say(ev["reason"])
                    if cursor is not None and ev.get("at"):
                        cursor.post("move_to", *ev["at"])
                if ev.get("type") == "step_result":
                    steps = max(steps, int(ev.get("step") or 0))
                    if cursor is not None and ev.get("ok") and ev.get("click"):
                        cursor.post("click")
                emit(ev)
                continue
            op = item[0]
            if op == "sleep":
                if _sleep(stop, item[1] / self.speed):
                    continue
            elif op == "handover":
                _, k, title, detail = item
                emit({"type": "needs_you", "kind": k, "title": title, "detail": detail})
                outcome = "done"
                if ui is not None:
                    ui.reset()
                    ui.handover(title, detail, park_text="Your turn: click me when done")
                    end = time.time() + self.ui_timeout
                    while not ui.done_clicked.wait(0.1):
                        if ui.stop_clicked.is_set() or (stop is not None and stop.is_set()):
                            outcome = "stopped"
                            break
                        if time.time() > end:
                            break
                    ui.end_handover(thanks=outcome == "done")
                else:
                    _sleep(stop, 1.0 / self.speed)
                emit({"type": "human_done", "kind": k, "outcome": outcome, "note": ""})
                if outcome == "stopped":
                    result = ("stopped", f"stopped by the user during the human turn ({k})")
                    break
            elif op == "ask":
                _, k, q, opts = item
                emit({"type": "needs_you", "kind": k, "title": q, "detail": ""})
                ans = opts[0] if opts else "ok"
                if ui is not None:
                    ans = ui.ask(q, opts, allow_text=True, timeout_s=self.ui_timeout) or (opts[0] if opts else None)
                else:
                    _sleep(stop, 1.0 / self.speed)
                self.answers.append(ans)
                emit({"type": "human_done", "kind": k, "outcome": "answered", "note": str(ans)})
                if str(ans).lower() == "stop" or (ui is not None and ui.stop_clicked.is_set()):
                    result = ("stopped", f"stopped by the user ({k})")
                    break
            elif op == "finish":
                result = (item[1], item[2])
        else:
            pass
        if result[0] == "done":
            say("Done!")
        emit({"type": "task_finished", "result": result[0], "text": result[1], "steps": steps,
              "seconds": round(time.time() - t0, 1), "run_dir": str(run_dir)})
        return result


def demo_scripts():
    """Three tasks: a browser task with a login handover, a consent question + a stuck ask that ends in a
    failure, and a quick command task."""
    a = lambda s, r, d, m="browser", lv=1, at=None: {"type": "action", "step": s, "level": lv, "name": "click",  # noqa: E731
                                                      "desc": d, "reason": r, "method": m, "at": at}
    ok = lambda s, d, good=True, lv=1: {"type": "step_result", "step": s, "level": lv, "ok": good, "desc": d,  # noqa: E731
                                        "detail": "", "latency_s": 2.1, "diff": 0.12, "click": True}
    th = lambda s, lv=1: {"type": "thinking", "step": s, "level": lv, "model": "qwen", "thinking": ""}  # noqa: E731
    report = [
        th(1), ("sleep", 1.2), a(1, "Open the Test Shop website", "open_url localhost:8000/shop", at=(700, 200)),
        ("sleep", 1.0), ok(1, "open_url"),
        th(2), ("sleep", 1.0), a(2, "Go to the Reports page", 'click "Reports"', at=(980, 260)),
        ("sleep", 0.8), ok(2, "click"),
        ("handover", "login", "Please sign in", "This page needs your login. Sign in and I'll carry on by myself"),
        th(3, 2), ("sleep", 1.4), a(3, "Open the November report", 'click "November 2026"', lv=2, at=(820, 420)),
        ("sleep", 0.8), ok(3, "click", lv=2),
        th(4), ("sleep", 0.9), a(4, "Download it as PDF", 'click "Download PDF"', at=(1100, 380)),
        ("sleep", 0.8), ok(4, "click"),
        ("finish", "done", "Saved November report to Downloads (report_2026_11.pdf)"),
    ]
    mug = [
        th(1), ("sleep", 1.0), a(1, "Open the mug page", "open_url localhost:8000/mug", at=(640, 300)),
        ("sleep", 0.8), ok(1, "open_url"),
        ("ask", "consent", "This site asks about cookies. What should I do?", ["Reject all", "Accept all"]),
        th(2), ("sleep", 1.0), a(2, "Read the price next to the mug", 'click "Price"', at=(900, 500)),
        ("sleep", 0.8), ok(2, "click", False),
        th(2, 2), ("sleep", 1.2), a(2, "Read the price next to the mug", 'click "Price"', lv=2, at=(905, 505)),
        ("sleep", 0.8), ok(2, "click", False, lv=2),
        ("ask", "stuck", "I couldn't manage this step: read the price next to the mug. Can you help?",
         ["I'll do it", "Try again", "Stop"]),
        ("finish", "fail", "step 2: every level failed (it did not change the screen)"),
    ]
    files = [
        th(1), ("sleep", 0.8), a(1, "Find the PDFs in Downloads", "list_files ~/Downloads *.pdf", m="command"),
        ("sleep", 0.6), ok(1, "list_files"),
        a(2, "Move 12 PDFs to Reports", "move_files 12 files -> ~/Documents/Reports", m="command"),
        ("sleep", 0.8), ok(2, "move_files"),
        ("finish", "done", "Moved 12 PDFs from Downloads to Documents\\Reports"),
    ]
    return [("Download the November report from the Test Shop", report),
            ("Check the price of the grape mug", mug),
            ("Move the PDFs in Downloads to my Reports folder", files)]


class DemoRunner:
    """Picks the scripted run for a demo task by its text (unknown text -> the files script)."""

    def __init__(self, speed=1.0, ui_timeout=25):
        self.scripts = {t: FakeRunner(s, speed, ui_timeout) for t, s in demo_scripts()}
        self.fallback = FakeRunner(demo_scripts()[2][1], speed, ui_timeout)

    def __call__(self, task, *a, **kw):
        return self.scripts.get(task, self.fallback)(task, *a, **kw)


class DemoHitl:
    """Stands in for HumanLoop in the demo: FakeRunner only needs .ui."""

    def __init__(self, cursor, ui, executor, run_dir):
        self.ui = ui
