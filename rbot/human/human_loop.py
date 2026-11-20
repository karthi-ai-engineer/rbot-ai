"""Human-in-the-loop glue between the agent loop (agent.run_task), blocker detection / session recording
(hitl.py) and the buddy UI (hitl_ui.py).

  loop = HumanLoop(cursor, HumanUI(cursor), executor, run_dir)
  agent.run_task(task, executor, cursor, hitl=loop, ...)

What it does for run_task:
- check_blocker(obs, ...)   every step BEFORE the model is asked: login / otp / captcha / payment -> hand the
                            screen to the human; consent popups -> ask the user what to do.
- handle_stuck(...)         every ladder level failed -> "I'll do it" / "Try again" (+hint) / "Stop".
- handover(...)             the human's turn: buddy parks, executor stops watching the mouse, a HumanSession
                            records what the human does, hitl.wait_for_done decides when they are finished,
                            then a HANDOVER REVIEW (one LLM call with screenshots) decides whether the
                            agent's step is now done / partial / unrelated, so the agent does not redo it.
- ask(...)                  the ask_human tool.
Every method returns a dict for run_task: {"type": "human", "kind", "reason", "outcome", "how", "waited_s",
"summary", "review", "answer", "history" (line for the prompt), "feedback" (for the very next model call)}.
"""
import base64
import ctypes
import importlib
import io
import json
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

TITLES = {"login": "Please sign in", "otp": "Verification code needed", "captcha": "Robot check",
          "payment": "Payment — your turn", "consent": "Your choice", "stuck": "Can you help?",
          "takeover": "You're in control"}
# What the HUMAN sees (plain language). The technical evidence only goes to the log.
DETAILS = {"login": "This page needs your login. Sign in and I'll carry on by myself",
           "otp": "Enter the code from your phone or app. I'll wait, take your time",
           "captcha": "Please tick 'I'm not a robot' or solve the puzzle. I'm not allowed to do these",
           "payment": "Enter your card details and pay yourself. Click me when you've finished",
           "consent": "Handle the popup the way you like, then click me",
           "stuck": "Please do this step for me, then click me: {goal}",
           "takeover": "I'm paused. Click me when you want me to continue"}
PARK = {"login": "Your turn: sign in", "otp": "Waiting for your code…", "captcha": "Your turn: robot check",
        "payment": "Pay yourself, then click me", "stuck": "Your turn: click me when done",
        "takeover": "Paused: click me to continue", "consent": "Your turn: click me when done"}


def plain_goal(step_goal):
    """'Click the Download button (click #12 'Download')' -> 'click the Download button'"""
    g = re.sub(r"\s*\((?:click|double_click|right_click|type_text|press_key|scroll|drag|move|hotkey|open_app|open_url|wait)\b[^)]*\)\s*$",
               "", str(step_goal or "")).strip()
    g = g or "the next step"
    g = g[0].lower() + g[1:] if len(g) > 1 and not g[:2].isupper() else g
    return g if len(g) <= 80 else g[:77] + "…"


def plain_reason(fail_reason):
    r = str(fail_reason or "").lower()
    if "did not change the screen" in r:
        return "Nothing happened when I tried."
    if r.startswith("blocked"):
        return "That isn't allowed for me."
    if "same action 3 times" in r or "going in circles" in r:
        return "I keep trying the same thing and it isn't working."
    if "gave up" in r:
        return "I couldn't find a way to do it."
    return ""


def consent_question(evidence):
    e = str(evidence or "").lower()
    if "save your password" in e or "password save" in e:
        return "The browser wants to save your password. Save it or not now?"
    if "cookie" in e:
        return "This website is asking about cookies. What should I do?"
    if "notification" in e:
        return "This website wants to send you notifications. Allow or block?"
    if "location" in e:
        return "This website wants to know your location. Allow or block?"
    if "allow" in e and ("block" in e or "deny" in e):
        return "This website is asking for a permission. Allow or block?"
    return "A popup is asking for a decision. What should I do?"
VERDICTS = ("done", "partial", "unrelated", "unsure")
VERDICT_ALIASES = {"complete": "done", "completed": "done", "finished": "done", "success": "done", "yes": "done",
                   "incomplete": "partial", "in_progress": "partial", "progress": "partial",
                   "irrelevant": "unrelated", "other": "unrelated", "different": "unrelated", "no": "unrelated",
                   "unknown": "unsure", "unclear": "unsure", "uncertain": "unsure"}
MAX_STUCK_ASKS = 3
IDLE_TAKEOVER = 5.0          # takeover / stuck: no blocker to disappear -> need a longer quiet period
IDLE_BLOCKER = 3.0
STABLE = 2.0
SUPPRESS_CHECKS = 2          # same blocker right after the human said "done": probably a false positive
REVIEW_IMG_W = 960
REVIEW_TIMEOUT_S = 60          # a slow/busy gateway must not keep the user waiting: fall back to asking them
REVIEW_THINKING = False        # thinking-off review: ~5-10 s; unclear answers are settled by asking the user
CONSENT_BTN = re.compile(r"\b(accept|agree|allow|block|don't allow|not now|reject|decline|deny|refuse|necessary|essential|only|ok|okay|got it|"
                         r"consent|manage|customi[sz]e|preferences|settings|close|dismiss|continue)\b", re.I)

REVIEW_SYSTEM = """You check the work of a HUMAN who briefly took over a Windows PC from an automation agent.
Decide whether the human completed the agent's CURRENT STEP GOAL, so the agent does not redo it.
You get the task, the step goal, the agent's recent actions, a log of what the human did (clicks with element
names, how many keys were typed per field - never the text, which windows were used) and screenshots in time
order (first = before the human started, last = the screen now).
verdict:
- "done": the step goal is completed (e.g. the sign-in went through and the app/site is now usable, the popup
  is gone, the element the agent needed is now in the state the goal wanted).
- "partial": the human worked on the goal and made real progress, but it is not finished yet.
- "unrelated": the human did something else (another app/page/task); the step goal is NOT done.
- "unsure": the log and screenshots do not let you decide.
Judge mainly by comparing the LAST screenshot with the FIRST one. Clicks and keys alone prove nothing.
Answer with ONLY this JSON object, no other text:
{"verdict": "done|partial|unrelated|unsure", "explanation": "<one short sentence about what the human did>",
 "next": "<the agent's next action, in a few words>"}"""


# ---------------------------------------------------------------- small helpers
def strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return text.split("</think>")[-1].strip()


def _json_objects(text):
    dec, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
            yield obj
            i = end
        except json.JSONDecodeError:
            i += 1


def _norm_verdict(v):
    v = re.sub(r"[^a-z_]", "", str(v or "").strip().lower().replace(" ", "_"))
    return v if v in VERDICTS else VERDICT_ALIASES.get(v, "unsure")


def parse_review(text):
    """Model reply -> {"verdict", "explanation", "next"}. Tolerates <think>, ```json fences, prose around the
    JSON, single quotes and a missing object (keyword fallback). Never raises."""
    raw = text or ""
    body = strip_think(raw) or raw
    cands = re.findall(r"```(?:json)?\s*(.*?)```", body, flags=re.S) + [body, raw]
    for chunk in cands:
        for obj in _json_objects(chunk):
            if isinstance(obj, dict) and "verdict" in obj:
                return {"verdict": _norm_verdict(obj.get("verdict")),
                        "explanation": str(obj.get("explanation") or obj.get("reason") or "")[:300],
                        "next": str(obj.get("next") or obj.get("next_action") or "")[:200]}
        try:                                               # python-ish dict with single quotes
            m = re.search(r"\{[^{}]*verdict[^{}]*\}", chunk, flags=re.S)
            if m:
                obj = json.loads(re.sub(r"'", '"', m.group(0)))
                return {"verdict": _norm_verdict(obj.get("verdict")),
                        "explanation": str(obj.get("explanation", ""))[:300], "next": str(obj.get("next", ""))[:200]}
        except (json.JSONDecodeError, AttributeError):
            pass
    m = re.search(r"verdict[\"']?\s*[:=]\s*[\"']?([A-Za-z_]+)", body, flags=re.I)
    if m:
        return {"verdict": _norm_verdict(m.group(1)), "explanation": body[:200], "next": ""}
    return {"verdict": "unsure", "explanation": f"could not read the review: {body[:120]!r}", "next": ""}


def img_data_url(img, max_w=REVIEW_IMG_W):
    img = img.convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def make_review_llm(model=None):
    """-> llm(messages, thinking) -> reply text. Plain chat (no tools), thinking on by default."""
    from rbot.llm import MODEL, get_client
    client = get_client(timeout=REVIEW_TIMEOUT_S)
    model = model or MODEL

    def llm(messages, thinking=True):
        r = client.chat.completions.create(
            model=model, messages=messages, max_tokens=6144 if thinking else 600, temperature=0.2,
            extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
        return r.choices[0].message.content or ""
    return llm


def foreground_window():
    """(hwnd, title) of the current foreground window."""
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, buf, 512)
    return h, buf.value


def focus_window(hwnd):
    """Bring a window to the front despite focus-stealing protection (same tricks as teams_agent.focus)."""
    from pynput.keyboard import Controller, Key
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    if not hwnd or not u.IsWindow(hwnd):
        return False
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
        time.sleep(0.5)
        if u.GetForegroundWindow() == hwnd:
            return True
    return False


def _same(answer, option):
    return (answer or "").strip().lower() == option.strip().lower()


# ---------------------------------------------------------------- the loop
class HumanLoop:
    def __init__(self, cursor, ui, executor, run_dir, llm=None, timeout_s=300, *, hitl_mod=None,
                 observe=None, stop=None, get_foreground=None, focus=None):
        """llm: callable(messages, thinking) -> reply text for the handover review (default: gateway, no tools).
        hitl_mod / observe / get_foreground / focus are injectable for tests."""
        self.cursor, self.ui, self.ex = cursor, ui, executor
        self.run_dir = Path(run_dir) if run_dir else None
        self.timeout_s = timeout_s
        self._llm = llm
        self._h = hitl_mod
        self.observe = observe
        self.stop = stop
        self.get_foreground = get_foreground or foreground_window
        self.focus = focus or focus_window
        self.stuck_asks = 0
        self.turns = 0
        self.agent_win = (None, "")            # the window the agent was working in (for refocus)
        self._suppress = None                  # {"key": (kind, evidence), "left": n}
        self.takeover_at = None

    # ---- wiring --------------------------------------------------------------
    def bind(self, observe=None, stop=None, run_dir=None):
        """Called by run_task at the start of a task."""
        if observe is not None:
            self.observe = observe
        if stop is not None:
            self.stop = stop
        if run_dir is not None:
            self.run_dir = Path(run_dir)
        self.stuck_asks, self._suppress, self.takeover_at = 0, None, None
        if stop is not None and hasattr(self.ui, "should_stop") and self.ui.should_stop is None:
            self.ui.should_stop = stop.is_set               # the run's stop also closes an open question
        self._ui("reset")

    def stop_requested(self):
        """The user clicked Stop on the question card / chip (HumanUI.stop_clicked)."""
        ev = getattr(self.ui, "stop_clicked", None)
        return bool(ev is not None and ev.is_set())

    def on_takeover(self):
        """executor.takeover_handler (pynput listener thread: keep it tiny)."""
        self.takeover_at = time.time()

    @property
    def hitl(self):
        if self._h is None:
            self._h = importlib.import_module("rbot.human.hitl")
        return self._h

    @property
    def llm(self):
        if self._llm is None:
            self._llm = make_review_llm()
        return self._llm

    def note_agent_window(self, obs=None):
        try:
            hwnd, title = self.get_foreground()
        except Exception:  # noqa: BLE001
            hwnd, title = None, ""
        self.agent_win = (hwnd, (obs or {}).get("window") or title)

    @contextmanager
    def _human_owns_mouse(self):
        """While the human answers a question / takes a turn the executor must not treat their mouse as a
        takeover."""
        prev = getattr(self.ex, "human_turn", False) is True
        self.ex.human_turn = True
        try:
            yield
        finally:
            self.ex.human_turn = prev

    def _refocus_agent_window(self, force=False):
        hwnd, title = self.agent_win
        if not hwnd:
            return False
        try:
            if not force and self.get_foreground()[0] == hwnd:
                return True
            ok = bool(self.focus(hwnd))
        except Exception:  # noqa: BLE001
            ok = False
        print(f"refocus '{title[:50]}': {'ok' if ok else 'FAILED'}", flush=True)
        return ok

    def _ui(self, name, *a, **kw):
        fn = getattr(self.ui, name, None)
        if fn is None:
            return None
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001  (UI trouble must never kill the run)
            print(f"ui.{name} error: {e}", flush=True)
            return None

    def _ask_ui(self, question, options=None, allow_text=True, mood="calling"):
        try:
            before = self.get_foreground()
        except Exception:  # noqa: BLE001
            before = (None, "")
        with self._human_owns_mouse():
            try:
                ans = self.ui.ask(question, options=options, allow_text=allow_text, timeout_s=self.timeout_s,
                                  mood=mood)
            except Exception as e:  # noqa: BLE001
                print(f"ui.ask error: {e}", flush=True)
                ans = None
        try:                                    # the question card may still hold the keyboard focus
            if before[0] and self.get_foreground()[0] != before[0]:
                time.sleep(0.3)                 # HumanUI gives it back by itself; only step in if it didn't
            if before[0] and self.get_foreground()[0] != before[0]:
                ok = bool(self.focus(before[0]))
                print(f"refocus after question '{before[1][:50]}': {'ok' if ok else 'FAILED'}", flush=True)
        except Exception:  # noqa: BLE001
            pass
        return ans.strip() if isinstance(ans, str) and ans.strip() else None

    # ---- 1. blockers -----------------------------------------------------------
    def check_blocker(self, obs, task, history):
        try:
            b = self.hitl.detect_blocker(obs)
            if b is not None and getattr(b, "hwnd", 0):
                self.agent_win = (b.hwnd, getattr(b, "window", "") or "")
        except Exception as e:  # noqa: BLE001
            print(f"detect_blocker error: {e}", flush=True)
            return None
        if b is None:
            self._suppress = None
            return None
        key = (b.kind, b.evidence)
        if self._suppress and self._suppress["key"] == key and self._suppress["left"] > 0:
            self._suppress["left"] -= 1
            return None
        self._suppress = None
        if b.kind == "consent":
            return self.handle_consent(b, obs, task, history)
        last = next((h for h in reversed(history) if "HUMAN TURN" not in h), "")
        goal = (f"get past the {b.kind} screen ({b.evidence}) so the agent can continue the task"
                + (f"; the agent's last action was: {last}" if last else ""))
        res = self.handover(f"{b.kind} needed: {b.evidence}", b.kind, obs, task, goal, blocker=b,
                            history=history)
        if res.get("outcome") == "done":
            self._suppress = {"key": key, "left": SUPPRESS_CHECKS}
        return res

    # ---- 4. consent popups -------------------------------------------------------
    CONSENT_RANK = [(re.compile(r"\b(reject|decline|deny|block|don't allow|refuse|necessary|essential|only)\b", re.I), 0),
                    (re.compile(r"\b(accept|agree|allow|ok|okay|got it|consent)\b", re.I), 1),
                    (re.compile(r"\b(customi[sz]e|manage|preferences|settings)\b", re.I), 2),
                    (re.compile(r"\b(close|dismiss|not now|continue)\b", re.I), 3)]
    BROWSER_CHROME = re.compile(r"\b(tab|window|ctrl\+|alt\+|minimi[sz]e|maximi[sz]e|restore|sidebar|panel)\b", re.I)

    def consent_options(self, obs):
        """Buttons of the popup, privacy-friendly choice first: Reject > Accept > Customize > Close."""
        seen, out = set(), []
        for m in (obs or {}).get("marks", []):
            name = str(m.get("name", "")).strip()
            if m.get("type") in ("Button", "Hyperlink") and 1 < len(name) <= 40 and CONSENT_BTN.search(name) \
                    and not self.BROWSER_CHROME.search(name) and name.lower() not in seen:
                seen.add(name.lower())
                rank = next((r for rx, r in self.CONSENT_RANK if rx.search(name)), 4)
                out.append((rank, len(out), name))
        names = [n for _, _, n in sorted(out)][:4]
        return (names or ["Reject all", "Accept all"]) + ["I'll do it"]

    def handle_consent(self, blocker, obs, task, history):
        options = self.consent_options(obs)
        self._ui("reset")
        self._ui("status", "A popup needs your decision")
        t0 = time.time()
        ans = self._ask_ui(consent_question(blocker.evidence), options, allow_text=True)
        base = {"type": "human", "kind": "consent", "reason": blocker.evidence, "answer": ans,
                "waited_s": round(time.time() - t0, 1), "how": "asked", "summary": "", "review": None}
        if not ans and self.stop_requested():
            return {**base, "outcome": "stopped", "how": "stop clicked"}
        if ans and _same(ans, "I'll do it"):
            res = self.handover(f"consent popup: {blocker.evidence}", "consent", obs, task,
                                f"deal with the popup ({blocker.evidence})", blocker=blocker, history=history)
            res["answer"] = ans
            if res.get("outcome") == "done":
                self._suppress = {"key": (blocker.kind, blocker.evidence), "left": SUPPRESS_CHECKS}
            return res
        # the popup is still on screen while the model acts on the answer -> don't ask again right away
        self._suppress = {"key": (blocker.kind, blocker.evidence), "left": 3}
        if not ans:
            text = ("the user did not answer; close the popup in the most privacy-preserving way "
                    "(Reject / Necessary only / Close), then continue the task")
            return {**base, "outcome": "no_answer", "feedback": f"USER INSTRUCTION for the popup: {text}",
                    "history": f"POPUP ({blocker.evidence}): {text}"}
        return {**base, "outcome": "instructed", "feedback": f"USER INSTRUCTION for the popup: {ans}",
                "history": f"USER INSTRUCTION for the popup ({blocker.evidence}): {ans}"}

    # ---- 5. stuck ----------------------------------------------------------------
    def handle_stuck(self, step_goal, fail_reason, obs, task, history):
        self.stuck_asks += 1
        base = {"type": "human", "kind": "stuck", "reason": f"stuck on: {step_goal} ({fail_reason})",
                "how": "asked", "summary": "", "review": None}
        if self.stuck_asks > MAX_STUCK_ASKS:
            return {**base, "outcome": "no_answer", "answer": None, "waited_s": 0.0,
                    "history": "", "note": "stuck-ask limit reached"}
        self._ui("reset")
        self._ui("stuck", "I'm stuck")
        t0 = time.time()
        why = plain_reason(fail_reason)
        ans = self._ask_ui(f"I couldn't manage this step: {plain_goal(step_goal)}." + (f" {why}" if why else "")
                           + " Can you help? \"I'll do it\" = you do this step, then click me. "
                             "\"Try again\" = I try another way. Or type a tip.",
                           ["I'll do it", "Try again", "Stop"], allow_text=True, mood="stuck")
        base.update(answer=ans, waited_s=round(time.time() - t0, 1))
        if ans is None and self.stop_requested():
            return {**base, "outcome": "stopped", "how": "stop clicked", "history": ""}
        if ans is None:
            return {**base, "outcome": "no_answer", "history": ""}
        if _same(ans, "Stop"):
            return {**base, "outcome": "stopped", "how": "user said stop", "history": ""}
        if _same(ans, "I'll do it"):
            res = self.handover(f"stuck: {step_goal}", "stuck", obs, task, step_goal, history=history)
            res["answer"] = ans
            return res
        hint = "" if _same(ans, "Try again") else ans
        return {**base, "outcome": "retry", "hint": hint,
                "feedback": ("The user asked you to try this step again" +
                             (f" and said: \"{hint}\". Follow that." if hint else " with a different approach.")),
                "history": (f"USER HINT (stuck on '{step_goal}'): {hint}" if hint
                            else f"USER: try '{step_goal}' again with a different approach")}

    # ---- 6. ask_human -------------------------------------------------------------
    def ask(self, question, options=None):
        if isinstance(options, str):
            options = [o.strip() for o in re.split(r"[|,;/]", options) if o.strip()]
        options = [str(o)[:60] for o in (options or [])][:6] or None
        self._ui("reset")
        return self._ask_ui(str(question), options, allow_text=True)

    # ---- 2. the human's turn ---------------------------------------------------------
    def _pseudo_blocker(self, kind, reason):
        try:
            return self.hitl.Blocker(kind=kind, evidence=reason, auto_resume=True, window=self.agent_win[1])
        except Exception:  # noqa: BLE001
            return SimpleNamespace(kind=kind, evidence=reason, auto_resume=True, window=self.agent_win[1])

    def _stop_watch(self, done):
        """Event set by the UI's Stop, the run's stop or the kill switch."""
        ev = threading.Event()

        def watch():
            while not done.is_set():
                if (getattr(self.ui, "stop_clicked", None) is not None and self.ui.stop_clicked.is_set()) \
                        or (self.stop is not None and self.stop.is_set()) \
                        or getattr(self.ex, "killed", False) is True:
                    ev.set()
                    return
                time.sleep(0.1)
        threading.Thread(target=watch, daemon=True).start()
        return ev

    def _wait_first_activity(self, session, clicked, stop, t0, deadline):
        """For turns without a blocker: don't let 'idle' count before the human even started."""
        while time.time() < deadline:
            if stop.is_set():
                return "stopped", "stopped"
            if clicked.is_set():
                return "done", "done clicked"
            try:
                if session.activity_since(t0):
                    return None
            except Exception:  # noqa: BLE001
                return None
            time.sleep(0.2)
        return "timeout", "no activity"

    def handover(self, reason, kind, obs, task, step_goal, blocker=None, history=None):
        self.turns += 1
        h = self.hitl
        self._ui("reset")
        title = TITLES.get(kind, "Your turn")
        detail = DETAILS.get(kind, "Click me when you're done").format(goal=plain_goal(step_goal))
        park = PARK.get(kind, "Click me when you're done")
        self._ui("handover", title, detail, park_text=park)
        pseudo = blocker is None
        blk = blocker or self._pseudo_blocker(kind, reason)
        clicked = getattr(self.ui, "done_clicked", None) or threading.Event()
        finished = threading.Event()
        stop_ev = self._stop_watch(finished)
        t0 = time.time()
        session, summary, outcome, how = None, {}, "stopped", "error"
        self.ex.human_turn = True
        try:
            session = h.HumanSession(self.run_dir or Path("."), reason, fps=1.0)
            session.start()
            early = None
            if pseudo and kind != "takeover":            # takeover: the human is already moving
                early = self._wait_first_activity(session, clicked, stop_ev, t0, t0 + self.timeout_s)
            if early:
                outcome, how = early
            else:                                         # timeout_s = how long the human may stay idle
                outcome, how = h.wait_for_done(
                    session, blk, self.observe, clicked, stop_ev, on_status=getattr(self.ui, "status", None),
                    idle_needed=IDLE_TAKEOVER if pseudo else IDLE_BLOCKER, stable_needed=STABLE, countdown=3.0,
                    timeout_s=self.timeout_s)
        except Exception as e:  # noqa: BLE001
            outcome, how = "stopped", f"error: {e}"
            print(f"handover error: {e}", flush=True)
        finally:
            finished.set()
            if session is not None:
                try:
                    summary = session.stop() or {}
                except Exception as e:  # noqa: BLE001
                    summary = {"text": f"(session summary unavailable: {e})"}
        waited = round(time.time() - t0, 1)
        text = str(summary.get("text", "") or "")
        res = {"type": "human", "kind": kind, "reason": reason, "outcome": outcome, "how": how,
               "summary": text, "review": None, "waited_s": waited, "answer": None, "history": "",
               "screen_changed": True}
        try:
            if outcome == "done":
                self._ui("status", "Let me check what you did…")
                review = self.review(kind, reason, task, step_goal, history or [], summary, session, obs,
                                     outcome, how, waited)
                review = self._settle_review(review, step_goal)
                res["review"] = review
                if review.get("verdict") == "stop":
                    res["outcome"], res["how"] = "stopped", "user said stop after the turn"
                else:
                    if review["verdict"] == "unrelated":
                        review["refocused"] = self._refocus_agent_window(force=True)
                    res["history"] = self.history_line(kind, waited, text, review, step_goal)
                    res["feedback"] = self.feedback_line(kind, reason, review, step_goal)   # next call must see it
            elif outcome == "timeout":
                res["history"] = f"HUMAN TURN ({kind}): no answer from the human after {waited:.0f} s"
        finally:
            self._ui("end_handover", thanks=res["outcome"] == "done")
            self.ex.human_turn = False
        return res

    # ---- 3. handover review -----------------------------------------------------------
    def _frames(self, session, obs):
        """Up to 4 screenshots in time order: before, after important clicks, now. None for private sessions."""
        if session is not None and getattr(session, "private", False):
            return []                                     # payment: nothing leaves the screen
        frames = []
        try:
            frames = list(session.keyframes(n=4) or []) if session is not None else []
        except Exception as e:  # noqa: BLE001
            print(f"keyframes error: {e}", flush=True)
        if len(frames) < 2:                               # fall back to: agent's last view + the screen now
            frames = []
            if obs is not None and obs.get("image") is not None:
                frames.append(obs["image"])
            try:
                if self.observe:
                    frames.append(self.observe()["image"])
            except Exception:  # noqa: BLE001
                pass
        if len(frames) > 4:
            frames = frames[:1] + frames[-3:]
        return frames

    @staticmethod
    def human_log(summary, max_clicks=12):
        """Readable log of the session: clicks with element names, key COUNTS per field, window timeline."""
        lines = []
        clicks = summary.get("clicks") or []
        for c in clicks[:max_clicks]:
            if not isinstance(c, dict):
                continue
            el = str(c.get("element") or "?")[:50]
            ty = f" ({c['type']})" if c.get("type") else ""
            win = f" in '{str(c['window'])[:50]}'" if c.get("window") else ""
            t = f"{float(c['t']):.0f}s " if isinstance(c.get("t"), (int, float)) else ""
            lines.append(f"- {t}click '{el}'{ty}{win}")
        if len(clicks) > max_clicks:
            lines.append(f"- ... {len(clicks) - max_clicks} more clicks")
        keys = summary.get("keys") or {}
        if isinstance(keys, dict) and keys.get("total"):
            fields = ", ".join(f"{str(f)[:40]}: {n}" for f, n in list((keys.get("fields") or {}).items())[:5])
            lines.append(f"- typed {keys['total']} keys" + (f" ({fields})" if fields else "") + " [text not recorded]")
        titles = []
        for w in summary.get("windows") or []:
            t = str(w[1] if isinstance(w, (list, tuple)) and len(w) > 1 else w)
            if t and (not titles or titles[-1] != t):
                titles.append(t)
        if titles:
            lines.append("- windows: " + " -> ".join(f"'{t[:50]}'" for t in titles[-6:]))
        return "\n".join(lines)

    def review_messages(self, kind, reason, task, step_goal, history, summary, frames, outcome, how, waited):
        hist = "\n".join(history[-8:]) or "(none)"
        detail = self.human_log(summary)
        text = (f"TASK: {task}\n\n"
                f"STEP GOAL the human was asked to handle: {step_goal}\n"
                f"Why the human took over: {kind} — {reason}\n\n"
                f"Agent's recent actions:\n{hist}\n\n"
                f"What the human did ({waited:.0f} s, ended by: {how}): {summary.get('text') or '(no log)'}\n"
                + (f"{detail}\n" if detail else "")
                + (f"\n{len(frames)} screenshots follow in time order (first = before, last = now)." if frames
                   else "\nNo screenshots are available (private session)."))
        content = [{"type": "text", "text": text}]
        for i, im in enumerate(frames):
            label = "BEFORE" if i == 0 else ("NOW (final)" if i == len(frames) - 1 else f"during #{i}")
            content.append({"type": "text", "text": f"Screenshot {i + 1}: {label}"})
            content.append({"type": "image_url", "image_url": {"url": img_data_url(im)}})
        return [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": content}]

    def review(self, kind, reason, task, step_goal, history, summary, session, obs, outcome, how, waited):
        frames = self._frames(session, obs)
        if not frames:                                    # nothing to judge by (e.g. payment) -> ask the human
            print("review: no screenshots -> asking the user", flush=True)
            return {"verdict": "unsure", "explanation": "no screenshots to check (private session)", "next": "",
                    "source": "none", "latency_s": 0.0, "images": 0}
        msgs = self.review_messages(kind, reason, task, step_goal, history, summary, frames, outcome, how, waited)
        t0 = time.time()
        try:
            reply = self.llm(msgs, REVIEW_THINKING)
            rv = parse_review(reply)
        except Exception as e:  # noqa: BLE001
            rv = {"verdict": "unsure", "explanation": f"review call failed: {e}"[:200], "next": ""}
        rv["source"] = "llm"
        rv["latency_s"] = round(time.time() - t0, 1)
        rv["images"] = len(frames)
        print(f"review: {rv['verdict']} — {rv['explanation'][:100]} ({rv['latency_s']}s)", flush=True)
        return rv

    def _settle_review(self, rv, step_goal):
        if rv.get("verdict") != "unsure":
            return rv
        ans = self._ask_ui(f"Did you finish this: {plain_goal(step_goal)}?", ["Yes, continue", "No, I did something else", "Stop"],
                           allow_text=True)
        rv = dict(rv, source="user", answer=ans)
        if ans is None:
            rv["explanation"] = (rv.get("explanation") or "") + " (the user did not confirm)"
        elif _same(ans, "Yes, continue"):
            rv.update(verdict="done", explanation="the user confirmed they finished it")
        elif _same(ans, "No, I did something else"):
            rv.update(verdict="unrelated", explanation="the user said they did something else")
        elif _same(ans, "Stop"):
            rv.update(verdict="stop", explanation="the user asked to stop")
        else:
            rv["explanation"] = f"the user said: {ans}"
        return rv

    @staticmethod
    def advice(review, step_goal):
        v = review.get("verdict", "unsure")
        return {
            "done": "Already done by the human: do NOT redo it; continue with the rest of the task from the "
                    "current screen.",
            "partial": "Partly done by the human: finish only what is missing, do not repeat what is done.",
            "unrelated": f"The human did something else; '{step_goal[:80]}' is NOT done yet: continue it"
                         + (" (your window was brought back to the front)." if review.get("refocused")
                            else " (check which window is in front first)."),
            "unsure": "Unclear whether it is done: look at the screen carefully first and do not repeat work "
                      "that is already finished.",
        }.get(v, "")

    @classmethod
    def history_line(cls, kind, waited, summary_text, review, step_goal):
        v = review.get("verdict", "unsure")
        s = (summary_text or "no actions recorded").strip().rstrip(".")
        if not re.match(r"^\d+(\.\d+)? ?s\b", s):           # hitl's summary text already starts with "38 s"
            s = f"{waited:.0f} s, {s}"
        if len(s) > 300:
            s = s[:297] + "…"
        nxt = f"; next: {review['next']}" if review.get("next") else ""
        return (f"HUMAN TURN ({kind}): {s}. Review: {v} — {review.get('explanation', '')}{nxt}. "
                f"{cls.advice(review, step_goal)}")

    @classmethod
    def feedback_line(cls, kind, reason, review, step_goal):
        """Short version for the very next model call (the full line is in the history)."""
        return (f"The human just took a turn ({kind}: {reason[:80]}). Review: {review.get('verdict', 'unsure')}. "
                f"{cls.advice(review, step_goal)}")
