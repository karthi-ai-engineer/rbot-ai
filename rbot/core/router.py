"""Method router (PLAN §3): the model proposes one tool call, the router picks HOW it is executed.

  1. command  file tools (commands.py) + web_search / wait            no UI at all, buddy shows a card
  2. browser  background mode, the step is on the buddy browser tab    browser_exec.BrowserExecutor (tab pixels,
                                                                       ghost click, the real mouse never moves)
  3. uia      click / double_click / right_click on a box, type_text / press_key into the box clicked last, in a
              normal (non-browser-page) window: uia_exec.try_invisible (UI Automation patterns, ghost click)
  4. mouse    everything else: actions.Executor (real mouse + keyboard), wrapped in ex.step() = one MouseGate
              burst per step; the gate does the idle/ask waiting itself. mouse_gate.MouseDenied (it waited 300 s or
              the user pressed Stop on its card) ends the task as "stopped" in both loops (agent.end_result).
  "none"      ask_human / done / fail

Fallback:
- uia returns None ("this element has no pattern I can use") -> the real mouse does it in the same attempt. Nothing
  was executed, so this is not a retry and is allowed for risky steps too.
- A method whose executed attempts failed twice in a row (no visible change / executor error) is skipped until an
  attempt succeeds again: uia -> mouse; browser -> the buddy browser window is brought to the front and the next
  attempts look at the real screen and use the real mouse.
- Risky steps (send / delete / buy / pay ...) never fall back silently after such failures: the human is asked
  first (Yes / No); without a human they are refused (BadAction "BLOCKED: ...").
Both agent loops (agent.run_task and agent_graph.run_task) use one Router per run, so the method choice, the
steps.jsonl node["method"] and the action events' "method" are the same on both engines.
"""
import os
import functools
import re
from collections import Counter

from rbot.core import agent
from rbot.core import commands
from rbot.core.agent import BadAction

NONE_TOOLS = {"ask_human", "done", "fail"}
COMMAND_TOOLS = commands.TOOLS | {"web_search", "wait"}
BROWSER_TOOLS = {"click", "double_click", "right_click", "move", "drag", "scroll", "type_text", "press_key", "hotkey",
                 "press_and_hold"}
INVISIBLE_TOOLS = {"click", "double_click", "right_click", "type_text", "press_key", "scroll"}
POINTER_ID_TOOLS = {"click", "double_click", "right_click"}
ID_TOOLS = POINTER_ID_TOOLS | {"scroll"}
METHODS = ("command", "browser", "uia", "mouse", "none")
MAX_FAILS = 2
RISKY = re.compile(r"\b(send|delete|remove|buy|pay|purchase|place (an )?order|check ?out|submit|transfer|"
                   r"confirm (the )?(order|payment|purchase))\b", re.I)
WEB_TASK = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(com|org|net|io|in|de|co|ai|dev|app)\b|\b(web ?site|web ?page|"
                      r"browser|online|internet|google|bing|search the web|web search|url|log ?in|sign ?in|"
                      r"download|shop|amazon|youtube|gmail|outlook\.com|form)\b", re.I)
YES = re.compile(r"^\s*(yes|y|ok|okay|sure|go ahead|do it)\b", re.I)


def invisible_on():
    """BUDDY_INVISIBLE=0 turns invisible (UI Automation) clicks off; default on."""
    return os.getenv("BUDDY_INVISIBLE", "1").strip().lower() not in ("0", "false", "no", "off")


def static_method(name, obs):
    """The method without run context (no UIA knowledge): none / command / browser / mouse."""
    if name in NONE_TOOLS:
        return "none"
    if name in COMMAND_TOOLS:
        return "command"
    if name == "open_url" or ((obs or {}).get("background") and name in BROWSER_TOOLS):
        return "browser"
    return "mouse"


def is_risky(name, args, mark=None):
    """send / delete / buy / pay ... in the target's name, the model's target description or its reason."""
    if name in NONE_TOOLS or name in COMMAND_TOOLS:
        return False
    a = args or {}
    text = " ".join(str(x) for x in ((mark or {}).get("name", ""), a.get("target", ""), a.get("reason", "")) if x)
    return bool(RISKY.search(text))


def is_mouse_denied(e):
    return type(e).__name__ == "MouseDenied"


def _has(obj, name):
    """A real method on the object's class (fakes with a catch-all __getattr__ don't count)."""
    return obj is not None and callable(getattr(type(obj), name, None))


def _url(raw):
    url = str(raw or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    if not re.match(r"^https?://[\w.-]+", url):
        raise BadAction(f"not a web address: {url!r}")
    return url


def _what(name, args, mark):
    t = str((mark or {}).get("name") or (args or {}).get("target") or (args or {}).get("reason") or name).strip()
    return t[:50] or name


class Router:
    """One per run. observe() is the run's observe (desktop screen, or the buddy browser tab in background mode);
    execute() runs one tool call with the first method that can do it and sets .method."""

    def __init__(self, executor, cursor=None, *, task="", mode="live", observe=None, real_screen=False,
                 invisible=None, browser_ex=None, observe_tab=None, front=None, back=None, ask=None, say=None,
                 hitl=None, allowed=None, stop=None):
        self.ex, self.cursor, self.task = executor, cursor, task
        self.desktop_observe = observe
        self.hitl, self._ask, self._say, self.allowed = hitl, ask, say or (lambda text: None), allowed
        self.mode = mode if mode in ("live", "background") else "live"
        self.fails = Counter()                 # executed-and-failed attempts per method since the last success
        self.method = None                     # method of the last execute()
        self.confirms = []                     # questions asked by the router / commands since take_confirms()
        self.last_mark = None                  # the box clicked last (type_text / press_key land there)
        self.desktop_fallback = False          # background: the tab failed twice -> real screen + real mouse
        self._own_browser = False
        gate = getattr(executor, "__dict__", {}).get("gate")     # actions.Executor(gate=mouse_gate.make_gate(...))
        if gate is not None and callable(getattr(gate, "new_task", None)):
            try:
                gate.new_task()                                     # ask_once_per_task / continuity start fresh
            except Exception as e:  # noqa: BLE001
                print(f"mouse gate new_task failed ({type(e).__name__}: {e})", flush=True)
        self.try_invisible = None
        if callable(invisible):
            self.try_invisible = invisible
        elif invisible or (invisible is None and real_screen and invisible_on()):
            try:
                import rbot.desktop.uia_exec as uia_exec    # (this form fails on a None sys.modules entry; tests use that)
                # live: the agent works in front, so the app it clicks should STAY in front (next it may need the
                # real keyboard there); background: give the user's window its focus back after each action
                keep = self.mode == "background"
                self.try_invisible = functools.partial(uia_exec.try_invisible, restore=keep)
            except Exception as e:  # noqa: BLE001  (module not there yet / broken -> real mouse only)
                print(f"invisible clicks unavailable ({type(e).__name__}: {e})", flush=True)
        self.browser_ex = self.observe_tab = None
        if self.mode == "background":
            try:
                if browser_ex is None:
                    import rbot.browser.browser_exec as browser_exec
                    browser_ex = browser_exec.BrowserExecutor(target_id=None, cursor=cursor, stop=stop)
                    self._own_browser = True
                if observe_tab is None:
                    if _has(browser_ex, "observe"):             # the executor's own tab (same target id)
                        observe_tab = browser_ex.observe
                    else:
                        from rbot.browser import browser_dom
                        observe_tab = lambda: browser_dom.observe_tab(getattr(browser_ex, "target_id", None))  # noqa: E731
                self.browser_ex, self.observe_tab = browser_ex, observe_tab
            except Exception as e:  # noqa: BLE001  (no background browser -> live mode)
                print(f"background browser unavailable ({type(e).__name__}: {e}); running live", flush=True)
                self.mode = "live"
        bx = self.browser_ex
        self.front = front or (bx.bring_to_front if _has(bx, "bring_to_front") else None)
        self.back = back or (bx.send_to_back if _has(bx, "send_to_back") else None)
        self.surface = "browser" if self.mode == "background" and WEB_TASK.search(task or "") else "desktop"

    # ---- seeing ---------------------------------------------------------------------------------------------
    def observe(self):
        """Background + browser step: the buddy browser tab (no real window needed); else the real screen."""
        if self.mode == "background" and self.surface == "browser" and not self.desktop_fallback:
            try:
                obs = self.observe_tab()
                if obs is not None:
                    obs.setdefault("background", True)
                    obs.setdefault("origin", (0, 0))
                    return obs
            except Exception as e:  # noqa: BLE001
                print(f"looking at the browser tab failed ({type(e).__name__}: {e}); looking at the screen",
                      flush=True)
        return self.desktop_observe()

    # ---- choosing ---------------------------------------------------------------------------------------------
    def _mark(self, name, args, marks):
        if name in ID_TOOLS:
            mid = (args or {}).get("id")
            return marks.get(int(mid)) if str(mid).isdigit() else None
        if name in ("type_text", "press_key"):
            return self.last_mark
        return None

    def _uia_possible(self, name, mark, obs):
        return (self.try_invisible is not None and name in INVISIBLE_TOOLS and mark is not None
                and not obs.get("background") and obs.get("page_source") != "dom")

    def plan(self, name, args, marks, obs):
        """First-choice method for this call (no side effects)."""
        if name in NONE_TOOLS:
            return "none"
        if name in COMMAND_TOOLS:
            return "command"
        if name == "open_url":
            return "browser"
        if obs.get("background") and self.browser_ex is not None and name in BROWSER_TOOLS:
            return "browser"
        if self._uia_possible(name, self._mark(name, args, marks), obs) and self.fails["uia"] < MAX_FAILS:
            return "uia"
        return "mouse"

    # ---- asking -----------------------------------------------------------------------------------------------
    def ask(self, question, options=("Yes", "No")):
        """A confirmation for the human (logged in node["confirm"]). -> answer or None."""
        ans = self._ask(question, list(options)) if self._ask is not None else None
        if not ans and self.hitl is not None and self.hitl.stop_requested():
            raise KeyboardInterrupt("the user clicked Stop")
        self.confirms.append({"question": str(question), "answer": ans})
        return ans

    def take_confirms(self):
        out, self.confirms = self.confirms, []
        return out

    def _allow_fallback(self, name, args, mark, how):
        """Risky step after its method failed twice: ask before doing it another way. -> True if allowed."""
        if not is_risky(name, args, mark):
            return True
        if self._ask is None:
            return False
        what = _what(name, args, mark)
        ans = self.ask(f"I couldn't do \"{what}\" the quiet way. May I {how} to do it?", ["Yes", "No"])
        return bool(ans) and bool(YES.match(str(ans)))

    # ---- doing ------------------------------------------------------------------------------------------------
    def execute(self, name, args, marks, obs, info=None):
        """Run one tool call. -> outcome text (raises like agent.execute). Sets self.method to what really did it."""
        self.method = m = self.plan(name, args, marks, obs)
        size, origin = obs["size"], obs.get("origin", (0, 0))
        if m == "command":
            if name in commands.TOOLS:
                return commands.run(name, args, ask=self.ask if self._ask is not None else None, say=self._say,
                                    allowed=self.allowed)
            return agent.execute(name, args, self.ex, marks, size, origin)
        self.last_mark = self.last_mark if name in ID_TOOLS | {"type_text", "press_key"} else None
        if name == "open_url":
            if self.mode == "background" and _has(self.browser_ex, "open_url"):
                self.browser_ex.open_url(_url(args.get("url")))   # our tab navigates; browser starts behind
                out = "ok"
            else:
                out = agent.execute(name, args, self.ex, marks, size, origin)
            if self.mode == "background":
                self.surface = "browser"
                self.desktop_fallback = False
            return out
        if m == "browser":
            out = agent.execute(name, args, self.browser_ex, marks, size, (0, 0), info=info, hover=False)
            self._remember(name, self._mark(name, args, marks))
            return out
        if name == "open_app" and self.mode == "background":
            self.surface = "desktop"
        mark = self._mark(name, args, marks)
        if m == "uia":
            out = self._uia(name, args, mark, obs, origin)
            if out is not None:
                self._remember(name, mark)
                return out
            self.method = "mouse"                # it had no way to do it: nothing happened -> the real mouse
        elif self._uia_possible(name, mark, obs) and self.fails["uia"] >= MAX_FAILS:
            if not self._allow_fallback(name, args, mark, "use the mouse"):
                raise BadAction(f"BLOCKED: \"{_what(name, args, mark)}\" is a risky step and the quiet way failed "
                                "twice; I won't redo it with the mouse without the user's OK")
        with self._burst():
            out = agent.execute(name, args, self.ex, marks, size, origin, roots=obs.get("roots"), info=info)
        self._remember(name, mark)
        return out

    def _burst(self):
        """One agent step = one real-mouse burst (actions.Executor.step() with the MouseGate), if available."""
        if _has(self.ex, "step"):
            try:
                return self.ex.step()
            except Exception:  # noqa: BLE001
                pass
        import contextlib
        return contextlib.nullcontext()

    def wrap_handover(self, fn):
        """Background mode: a human turn (sign-in, CAPTCHA, takeover...) on the buddy browser tab brings its window to
        the front first and sends it back afterwards (PLAN §8). fn = HumanLoop.handover."""
        def handover(*a, **kw):
            fronted = False
            if self.mode == "background" and self.surface == "browser" and not self.desktop_fallback \
                    and self.front is not None:
                try:
                    self.front()
                    fronted = True
                except Exception as e:  # noqa: BLE001
                    print(f"could not bring the browser to the front ({type(e).__name__}: {e})", flush=True)
            try:
                return fn(*a, **kw)
            finally:
                if fronted and self.back is not None:
                    try:
                        self.back()
                    except Exception:  # noqa: BLE001
                        pass
        return handover

    def _uia(self, name, args, mark, obs, origin):
        try:
            out = self.try_invisible(name, args, mark, obs, origin, cursor=self.cursor)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("KillSwitch", "HumanTakeover", "MouseDenied"):
                raise
            print(f"invisible {name} failed ({type(e).__name__}: {e}); using the mouse", flush=True)
            self.fails["uia"] += 1
            return None
        return None if out in (None, "") else str(out)

    def _remember(self, name, mark):
        if name in POINTER_ID_TOOLS:
            self.last_mark = mark
        elif name not in ("type_text", "press_key"):
            self.last_mark = None

    def after_attempt(self, node, executed, name=None, args=None, marks=None):
        """Called once per attempt before its node is written. Counts method failures; in background mode the
        second browser failure in a row switches to the real screen. -> True if the screen source changed (the
        caller must drop its cached screenshot)."""
        m = node.get("method")
        if node.get("ok"):
            self.fails.clear()
            changed, self.desktop_fallback = self.desktop_fallback, False
            return changed
        if not executed or m not in ("uia", "browser"):
            return False
        self.fails[m] += 1
        if m == "browser" and self.fails[m] >= MAX_FAILS and not self.desktop_fallback:
            mark = self._mark(name, args, marks or {})
            if not self._allow_fallback(name, args, mark, "bring the browser to the front and use the mouse"):
                return False
            self.desktop_fallback = True
            if self.front is not None:
                try:
                    self.front()
                except Exception as e:  # noqa: BLE001
                    print(f"could not bring the browser to the front ({type(e).__name__}: {e})", flush=True)
            return True
        return False

    @staticmethod
    def keeps_screen(name):
        """File commands (except open_file) never change the screen: the cached screenshot stays valid."""
        return name in commands.KEEPS_SCREEN

    def close(self):
        if self._own_browser and self.browser_ex is not None:
            try:
                self.browser_ex.stop()
            except Exception:  # noqa: BLE001
                pass
