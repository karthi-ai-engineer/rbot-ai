"""Offline tests for human_loop.py + the human-in-the-loop paths of agent.run_task and actions.Executor.

  python tests/test_human_loop.py            (or: python -m pytest tests/test_human_loop.py)
  HITL_LIVE=1 python tests/test_human_loop.py    also runs one real handover review on the gateway

Fake UI, fake hitl module (detect_blocker / HumanSession / wait_for_done), fake model, fake executor, fake
screen. Nothing here moves the mouse, presses keys or opens apps.
"""
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS

import conftest  # noqa: F401  (project root on sys.path when run as a script)
import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from rbot.core import agent  # noqa: E402
from rbot.core import agent_graph  # noqa: E402
from rbot.human import human_loop  # noqa: E402
from rbot.desktop.actions import HumanTakeover  # noqa: E402
from rbot.human.human_loop import HumanLoop, parse_review  # noqa: E402


# ============================================================================ engines
# Every end-to-end test runs on both agent loops: the legacy agent.run_task and the LangGraph agent_graph.run_task.
ENGINES = {"legacy": agent.run_task, "graph": agent_graph.run_task}
ENGINE = "legacy"


@pytest.fixture(params=sorted(ENGINES))
def engine(request):
    global ENGINE
    ENGINE = request.param
    yield request.param
    ENGINE = "legacy"


# ============================================================================ fakes
@dataclass
class Blocker:
    kind: str
    evidence: str
    auto_resume: bool
    window: str
    hwnd: int = 0


class Screen:
    """Fake desktop. state bumps when the agent does something visible; blocker = what detect_blocker sees."""
    def __init__(self, live=(), blocker=None, buttons=()):
        self.state, self.live, self.calls = 0, set(live), []
        self.blocker, self.buttons = blocker, list(buttons)

    def observe(self):
        im = Image.new("RGB", (1000, 500), (self.state * 40 % 255, 0, 0))
        marks = [{"id": i, "name": f"el{i}", "type": "Button", "box": [i * 10, 0, i * 10 + 10, 10]} for i in range(10)]
        marks += [{"id": 10 + i, "name": b, "type": "Button", "box": [100 + i * 50, 100, 140 + i * 50, 120]}
                  for i, b in enumerate(self.buttons)]
        return {"image": im, "marked": im, "size": (1000, 500), "window": "Fake App", "marks": marks}

    @staticmethod
    def diff(a, b):
        return 0.05 if a.tobytes() != b.tobytes() else 0.0


class FakeEx:
    """Executor stand-in with the HITL attributes. raise_on = {method_name: n} -> HumanTakeover on the n-th call."""
    def __init__(self, screen, raise_on=None):
        self.s, self.raise_on, self.n = screen, dict(raise_on or {}), {}
        self.killed = False
        self.takeover_handler = None
        self.turns = []                              # human_turn values in order
        self._human_turn = False

    @property
    def human_turn(self):
        return self._human_turn

    @human_turn.setter
    def human_turn(self, v):
        self._human_turn = v
        self.turns.append(v)

    def _do(self, name, *a):
        self.n[name] = self.n.get(name, 0) + 1
        if self.raise_on.get(name) == self.n[name]:
            raise HumanTakeover()
        self.s.calls.append((name, a))
        if name == "click" and a in self.s.live or name in ("type_text", "open_app", "press_key"):
            self.s.state += 1

    def _check(self):
        self._do("_check")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a: self._do(name, *a)


class FakeSession:
    def __init__(self, run_dir, reason, fps=1.0):
        self.run_dir, self.reason = run_dir, reason
        self.private = "payment" in reason.lower()
        self.active = True
        FakeHitl.sessions.append(self)

    def start(self):
        self.t0 = time.time()

    def stop(self):
        return {"text": "38 s, 5 clicks (Email field, Next button, Password field, Sign in button), "
                        "21 keys into Email/Password, window 'Sign in - Chrome'",
                "clicks": [{"t": 2.0, "element": "Email", "type": "Edit", "window": "Sign in - Chrome"},
                           {"t": 30.0, "element": "Sign in", "type": "Button", "window": "Sign in - Chrome"}],
                "keys": {"total": 21, "fields": {"Email": 12, "Password (password field)": 9}},
                "windows": [[0, "Sign in - Chrome"], [35, "Inbox - Chrome"]], "frames": []}

    def idle_seconds(self):
        return 0.0

    def stable_seconds(self):
        return 0.0

    def activity_since(self, t):
        return self.active

    def keyframes(self, n=4):
        if self.private:
            return []
        a = Image.new("RGB", (1920, 1080), "white")
        b = Image.new("RGB", (1920, 1080), "lightblue")
        return [a, b]


class FakeHitl:
    """Stands in for the hitl module."""
    sessions = []

    def __init__(self, screen, outcomes=(("done", "blocker gone + idle 3s"),), on_wait=None):
        self.screen, self.outcomes, self.on_wait = screen, list(outcomes), on_wait
        self.waits = []
        self.Blocker = Blocker
        self.HumanSession = FakeSession
        FakeHitl.sessions = []

    def detect_blocker(self, obs):
        return self.screen.blocker

    def blocker_present(self, kind, obs):
        return bool(self.screen.blocker and self.screen.blocker.kind == kind)

    def wait_for_done(self, session, blocker, observe, clicked, stop, on_status=None, idle_needed=3.0,
                      stable_needed=2.0, countdown=3.0, timeout_s=300):
        self.waits.append({"kind": blocker.kind, "auto_resume": blocker.auto_resume, "idle": idle_needed,
                           "timeout": timeout_s, "observe": observe is not None})
        if on_status:
            on_status("continuing in 3…")
        if self.on_wait:
            self.on_wait()
        else:
            self.screen.blocker = None                   # the human dealt with it
            self.screen.state += 7
        return self.outcomes.pop(0) if self.outcomes else ("done", "clicked buddy")


class FakeUI:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []
        self.done_clicked = threading.Event()
        self.stop_clicked = threading.Event()
        self.should_stop = None

    def ask(self, question, options=None, allow_text=True, timeout_s=300, mood="calling"):
        self.calls.append(("ask", question, options))
        a = self.answers.pop(0) if self.answers else None
        if a == "<STOP>":
            self.stop_clicked.set()
            return None
        return a

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **kw: self.calls.append((name,) + a + tuple(sorted(kw.items())))

    def names(self):
        return [c[0] for c in self.calls]


def review_llm(*replies):
    seen = []

    def llm(messages, thinking=True):
        seen.append((messages, thinking))
        r = replies[min(len(seen) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    llm.seen = seen
    return llm


def msg(content="", calls=None):
    tc = [NS(function=NS(name=n, arguments=json.dumps(a))) for n, a in (calls or [])]
    return NS(content=content, tool_calls=tc or None)


def C(tool, **a):
    return msg(calls=[(tool, {"reason": f"r-{tool}", **a})])


def model(script):
    seen = []

    def llm(m, messages, thinking):
        seen.append((m, thinking, messages[1]["content"][0]["text"]))
        r = script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, NS(prompt_tokens=10, completion_tokens=2)
    llm.seen = seen
    return llm


DONE = 'Looking at the screenshots: ```json\n{"verdict": "done", "explanation": "the human signed in", "next": "continue with the task"}\n```'


def setup(screen, answers=(), reviews=(DONE,), outcomes=(("done", "blocker gone + idle 3s"),), ex=None, on_wait=None,
          timeout_s=300):
    ui = FakeUI(answers)
    h = FakeHitl(screen, outcomes, on_wait)
    ex = ex or FakeEx(screen)
    focused = []
    loop = HumanLoop(None, ui, ex, None, llm=review_llm(*reviews), timeout_s=timeout_s, hitl_mod=h,
                     get_foreground=lambda: (111, "Fake App"), focus=lambda hw: focused.append(hw) or True)
    return loop, ui, h, ex, focused


def run(script, screen, loop, ex, max_steps=10, levels=(("m", False), ("m", True))):
    d = Path(tempfile.mkdtemp())
    llm = model(list(script))
    res = ENGINES[ENGINE]("test task", ex, None, max_steps, observe=screen.observe, diff_ratio=screen.diff, llm=llm,
                          levels=list(levels), run_dir=d, settle=False, hitl=loop, keep_images=True)
    nodes = [json.loads(x) for x in (d / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
    summ = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    return res, nodes, summ, llm.seen, d


def humans(nodes):
    return [n for n in nodes if n.get("type") == "human"]


# ============================================================================ tests
def test_parse_review():
    assert parse_review(DONE)["verdict"] == "done"
    assert parse_review('<think>maybe {"verdict":"unrelated"}</think>{"verdict": "partial", "explanation": "x", '
                        '"next": "y"}') == {"verdict": "partial", "explanation": "x", "next": "y"}
    assert parse_review('Here you go: {"verdict": "Completed", "explanation": "ok"} hope it helps')["verdict"] == "done"
    assert parse_review("{'verdict': 'unrelated', 'explanation': 'opened mail', 'next': 'go back'}")["verdict"] == "unrelated"
    assert parse_review('verdict: partial because only the email was typed')["verdict"] == "partial"
    assert parse_review("I cannot tell from these images.")["verdict"] == "unsure"
    assert parse_review("")["verdict"] == "unsure"
    assert parse_review(None)["verdict"] == "unsure"
    assert parse_review('{"verdict": "banana"}')["verdict"] == "unsure"


def test_blocker_handover_review_done(engine):
    s = Screen(live={(15, 5)}, blocker=Blocker("login", "sign in + password field", True, "Sign in - Chrome"))
    loop, ui, h, ex, focused = setup(s)
    res, nodes, summ, seen, d = run([C("click", id=1), C("done", summary="ok")], s, loop, ex)
    assert res == ("done", "ok"), res
    hn = humans(nodes)
    assert len(hn) == 1 and hn[0]["kind"] == "login" and hn[0]["outcome"] == "done", hn
    assert hn[0]["review"]["verdict"] == "done" and hn[0]["review"]["source"] == "llm"
    assert hn[0]["how"] == "blocker gone + idle 3s" and hn[0]["step"] == 1 and "38 s, 5 clicks" in hn[0]["summary"]
    assert set(hn[0]) >= {"step", "type", "reason", "kind", "outcome", "how", "waited_s", "summary", "review", "answer"}
    # the very first model call already knows the human did it and must not redo it
    p = seen[0][2]
    assert "HUMAN TURN (login)" in p and "Review: done — the human signed in" in p and "do NOT redo" in p, p
    assert "1. HUMAN TURN (login): 38 s, 5 clicks" in p                       # no duplicated duration
    assert "IMPORTANT: The human just took a turn (login: login needed" in p and "Review: done. Already done" in p
    # UI + executor choreography
    names = ui.names()
    assert names.index("handover") < names.index("end_handover")
    hand = next(c for c in ui.calls if c[0] == "handover")
    assert hand[1] == "Please sign in" and "sign in" in hand[2].lower()      # plain language for the human
    assert ("end_handover", ("thanks", True)) in ui.calls
    assert ex.turns[:2] == [True, False] and ex.human_turn is False
    assert ex.takeover_handler is None                   # disarmed at the end of the run
    assert h.waits == [{"kind": "login", "auto_resume": True, "idle": 3.0, "timeout": 300, "observe": True}]
    # review prompt: thinking OFF (fast; unclear answers are settled by asking the user), text log + images, no typed text
    msgs, thinking = loop.llm.seen[0]
    assert thinking is False
    user = msgs[1]["content"]
    txt = user[0]["text"]
    assert "STEP GOAL" in txt and "click 'Sign in' (Button)" in txt and "typed 21 keys" in txt and "[text not recorded]" in txt
    imgs = [c for c in user if c["type"] == "image_url"]
    assert len(imgs) == 2 and imgs[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert summ["human_turns"][0]["verdict"] == "done"


def test_same_blocker_after_done_is_suppressed(engine):
    """Detector still reports the login after the human said done -> no second handover right away."""
    b = Blocker("login", "sign in", True, "x")
    s = Screen(live={(15, 5), (25, 5)}, blocker=b)
    loop, ui, h, ex, _ = setup(s, on_wait=lambda: None, outcomes=[("done", "clicked buddy")])
    res, nodes, summ, seen, d = run([C("click", id=1), C("click", id=2), C("done", summary="k")], s, loop, ex)
    assert res[0] == "done" and len(humans(nodes)) == 1, humans(nodes)


def test_consent_answer_becomes_feedback(engine):
    s = Screen(live={(170, 110)}, blocker=Blocker("consent", "cookie banner: Accept all / Reject all", False, "x"),
               buttons=["Accept all", "Reject all", "Manage settings", "Sign in"])
    loop, ui, h, ex, _ = setup(s, answers=["Reject all"])
    res, nodes, summ, seen, d = run([C("click", id=11), C("done", summary="k")], s, loop, ex)
    assert res[0] == "done"
    ask = [c for c in ui.calls if c[0] == "ask"]
    assert len(ask) == 1, ask                             # popup still visible next step: not asked again
    assert ask[0][1] == "This website is asking about cookies. What should I do?"
    assert ask[0][2] == ["Reject all", "Accept all", "Manage settings", "I'll do it"], ask[0][2]   # privacy-first order
    assert "IMPORTANT: USER INSTRUCTION for the popup: Reject all" in seen[0][2]
    assert "USER INSTRUCTION for the popup (cookie banner" in seen[0][2]
    assert "handover" not in ui.names() and not h.waits
    hn = humans(nodes)
    assert hn[0]["kind"] == "consent" and hn[0]["outcome"] == "instructed" and hn[0]["answer"] == "Reject all"


def test_consent_ill_do_it_hands_over(engine):
    s = Screen(blocker=Blocker("consent", "allow notifications?", False, "x"))
    loop, ui, h, ex, _ = setup(s, answers=["I'll do it"])
    res, nodes, summ, seen, d = run([C("done", summary="k")], s, loop, ex)
    assert res[0] == "done"
    assert h.waits[0]["kind"] == "consent" and h.waits[0]["auto_resume"] is False
    assert humans(nodes)[0]["answer"] == "I'll do it" and humans(nodes)[0]["review"]["verdict"] == "done"


def test_stuck_ill_do_it_hands_over(engine):
    s = Screen(live={(15, 5)})
    loop, ui, h, ex, _ = setup(s, answers=["I'll do it"])
    res, nodes, summ, seen, d = run([C("click", id=42), C("click", id=43), C("done", summary="human did it")], s,
                                    loop, ex)
    assert res == ("done", "human did it"), res
    assert "stuck" in ui.names()
    ask = next(c for c in ui.calls if c[0] == "ask")
    assert ask[1].startswith("I couldn't manage this step: r-click") and \
        ask[2] == ["I'll do it", "Try again", "Stop"], ask
    assert h.waits[0]["kind"] == "stuck" and h.waits[0]["auto_resume"] is True and h.waits[0]["idle"] == 5.0
    hn = humans(nodes)
    assert hn[0]["kind"] == "stuck" and hn[0]["outcome"] == "done" and hn[0]["answer"] == "I'll do it"
    assert "HUMAN TURN (stuck)" in seen[2][2]


def test_stuck_try_again_with_hint(engine):
    s = Screen(live={(25, 5)})
    loop, ui, h, ex, _ = setup(s, answers=["use the search box at the top"])
    res, nodes, summ, seen, d = run([C("click", id=42), C("click", id=43), C("click", id=2), C("done", summary="k")],
                                    s, loop, ex)
    assert res[0] == "done", res
    assert [(n["step"], n["attempt"], n["level"], n["ok"]) for n in nodes if "attempt" in n] == \
        [(1, 1, 1, False), (1, 2, 2, False), (1, 3, 1, True), (2, 1, 1, True)], nodes
    assert seen[2][1] is False                            # retried from L1 (thinking off)
    assert 'said: "use the search box at the top"' in seen[2][2] and "USER HINT" in seen[2][2]
    assert (d / "step_01_a3.jpg").exists() and not h.waits
    assert humans(nodes)[0]["outcome"] == "retry"


def test_stuck_plain_try_again_stop_and_limit(engine):
    s = Screen(live={(25, 5)})
    loop, ui, h, ex, _ = setup(s, answers=["Try again"])
    res, nodes, *_ , seen, d = run([C("click", id=42), C("click", id=43), C("click", id=2), C("done", summary="k")],
                                   s, loop, ex)
    assert res[0] == "done" and "try this step again with a different approach" in seen[2][2]

    loop, ui, h, ex, _ = setup(Screen(), answers=["Stop"])
    res, nodes, summ, seen, d = run([C("click", id=42), C("click", id=43)], Screen(), loop, ex)
    assert res[0] == "stopped" and "stuck" in res[1], res

    loop, ui, h, ex, _ = setup(Screen(), answers=["<STOP>"])          # Stop button on the question card
    res, *_ = run([C("click", id=42), C("click", id=43)], Screen(), loop, ex)
    assert res[0] == "stopped", res

    loop, ui, h, ex, _ = setup(Screen(), answers=[None])               # no answer -> old behaviour: fail
    res, *_ = run([C("click", id=42), C("click", id=43)], Screen(), loop, ex)
    assert res[0] == "fail" and "every level failed (unknown mark id 43 " in res[1], res

    loop, ui, h, ex, _ = setup(Screen(), answers=["Try again"] * 5)    # at most 3 asks per task
    res, nodes, *_ = run([C("click", id=42), C("click", id=43)] * 4, Screen(), loop, ex)
    assert res[0] == "fail" and "limit" in res[1], res
    assert len([c for c in ui.calls if c[0] == "ask"]) == 3


def test_takeover_during_action(engine):
    s = Screen(live={(15, 5)})
    ex = FakeEx(s, raise_on={"click": 1})
    loop, ui, h, ex, _ = setup(s, ex=ex)
    res, nodes, summ, seen, d = run([C("click", id=1), C("done", summary="k")], s, loop, ex)
    assert res[0] == "done", res
    assert nodes[0]["fail_reason"] == "interrupted: the user took over the mouse"
    hn = humans(nodes)
    assert hn[0]["kind"] == "takeover" and hn[0]["step"] == 1 and hn[0]["review"]["verdict"] == "done"
    assert h.waits[0]["kind"] == "takeover" and h.waits[0]["auto_resume"] is True and h.waits[0]["idle"] == 5.0
    hand = next(c for c in ui.calls if c[0] == "handover")
    assert hand[1] == "You're in control"
    assert "HUMAN TURN (takeover)" in seen[1][2] and "r-click" in loop.llm.seen[0][0][1]["content"][0]["text"]
    assert ex.takeover_handler is None and ex.human_turn is False
    assert not [c for c in s.calls if c[0] == "click"]   # the interrupted click never happened


def test_takeover_before_model_call(engine):
    s = Screen()
    ex = FakeEx(s, raise_on={"_check": 1})
    loop, ui, h, ex, _ = setup(s, ex=ex)
    res, nodes, summ, seen, d = run([C("done", summary="k")], s, loop, ex)
    assert res[0] == "done" and humans(nodes)[0]["kind"] == "takeover" and "HUMAN TURN (takeover)" in seen[0][2]


def test_unsure_asks_user(engine):
    s = Screen(blocker=Blocker("otp", "enter code", True, "x"))
    loop, ui, h, ex, focused = setup(s, answers=["Yes, continue"], reviews=["I can't really tell, sorry."])
    res, nodes, *_ , seen, d = run([C("done", summary="k")], s, loop, ex)
    ask = [c for c in ui.calls if c[0] == "ask"]
    assert len(ask) == 1 and ask[0][1].startswith("Did you finish this: get past the otp screen"), ask
    assert ask[0][2] == ["Yes, continue", "No, I did something else", "Stop"]
    rv = humans(nodes)[0]["review"]
    assert rv["verdict"] == "done" and rv["source"] == "user" and not focused

    s = Screen(blocker=Blocker("otp", "enter code", True, "x", 111))     # the blocker's window = the agent's window
    loop, ui, h, ex, focused = setup(s, answers=["No, I did something else"], reviews=[RuntimeError("504")])
    res, nodes, *_ , seen, d = run([C("done", summary="k")], s, loop, ex)
    assert humans(nodes)[0]["review"]["verdict"] == "unrelated" and focused == [111]
    assert "NOT done yet" in seen[0][2]

    s = Screen(blocker=Blocker("otp", "enter code", True, "x"))
    loop, ui, h, ex, focused = setup(s, answers=["Stop"], reviews=["?"])
    res, nodes, *_ = run([C("done", summary="k")], s, loop, ex)
    assert res[0] == "stopped", res


def test_unrelated_refocuses_agent_window(engine):
    s = Screen(blocker=Blocker("captcha", "I'm not a robot", True, "x", 111))
    rv = '{"verdict": "unrelated", "explanation": "the human read email instead", "next": "solve the captcha step"}'
    loop, ui, h, ex, focused = setup(s, reviews=[rv])
    res, nodes, *_ , seen, d = run([C("done", summary="k")], s, loop, ex)
    assert focused == [111], focused                     # the blocker's window (where the agent works)
    assert humans(nodes)[0]["review"]["refocused"] is True
    p = seen[0][2]
    assert "Review: unrelated — the human read email instead" in p and "NOT done yet" in p and "brought back" in p


def test_timeout_ends_task_cleanly(engine):
    s = Screen(blocker=Blocker("login", "sign in", True, "x"))
    loop, ui, h, ex, _ = setup(s, outcomes=[("timeout", "timeout 300s idle")], on_wait=lambda: None)
    res, nodes, summ, seen, d = run([C("done", summary="never")], s, loop, ex)
    assert res == ("stopped", "timeout waiting for human (login)"), res
    assert not seen and not loop.llm.seen               # no model call, no review
    assert ("end_handover", ("thanks", False)) in ui.calls
    assert summ["result"] == "stopped" and summ["human_turns"][0]["outcome"] == "timeout"
    assert ex.human_turn is False and ex.takeover_handler is None


def test_payment_private_asks_instead_of_review(engine):
    s = Screen(blocker=Blocker("payment", "card number + cvv", False, "x"))
    loop, ui, h, ex, _ = setup(s, answers=["Yes, continue"], outcomes=[("done", "clicked buddy")])
    res, nodes, *_ = run([C("done", summary="k")], s, loop, ex)
    assert not loop.llm.seen                             # no screenshots of a payment form go anywhere
    assert h.waits[0]["auto_resume"] is False
    hand = next(c for c in ui.calls if c[0] == "handover")
    assert "click me" in dict(hand[3:])["park_text"] and "pay yourself" in hand[2].lower()
    assert humans(nodes)[0]["review"]["verdict"] == "done"


def test_ask_human_tool(engine):
    s = Screen()
    loop, ui, h, ex, _ = setup(s, answers=["Tharun Appireddy (Sales)"])
    res, nodes, *_ , seen, d = run([C("ask_human", question="Which Tharun?", options=["Sales", "IT"]),
                                    C("done", summary="k")], s, loop, ex)
    assert res[0] == "done"
    assert ("ask", "Which Tharun?", ["Sales", "IT"]) in ui.calls
    assert "1. ask_human \"Which Tharun?\" -> USER ANSWERED: Tharun Appireddy (Sales)" in seen[1][2], seen[1][2]
    hn = humans(nodes)
    assert hn[0]["kind"] == "ask" and hn[0]["answer"] == "Tharun Appireddy (Sales)"
    assert ex.turns == [True, False]                     # mouse belongs to the human while the card is open


def test_hitl_none_unchanged(engine):
    s = Screen(live={(15, 5)})
    ex = FakeEx(s)
    res, nodes, summ, seen, d = run([C("ask_human", question="Which one?"), C("click", id=1), C("click", id=42),
                                     C("click", id=43)], s, None, ex)
    assert res[0] == "fail" and "every level failed (unknown mark id 43 " in res[1], res   # no stuck-ask
    assert "no human available" in seen[1][2]
    assert not humans(nodes) and "human_turns" not in summ
    assert ex.turns == [] and ex.takeover_handler is None
    assert not [c for c in s.calls if c[0] == "_check"]  # executor untouched by HITL code


def test_contract_with_real_hitl():
    """Pseudo-blockers are real hitl.Blocker objects and the real wait_for_done ends on click / stop."""
    from rbot.human import hitl
    loop = HumanLoop(None, FakeUI(), FakeEx(Screen()), None, hitl_mod=hitl)
    b = loop._pseudo_blocker("takeover", "user took over")
    assert isinstance(b, hitl.Blocker) and b.auto_resume is True and b.hwnd == 0
    sess = NS(idle_seconds=lambda: 0.0, stable_seconds=lambda: 0.0, activity_since=lambda t: False)
    clicked, stop = threading.Event(), threading.Event()
    clicked.set()
    assert hitl.wait_for_done(sess, b, None, clicked, stop, timeout_s=5)[0] == "done"
    clicked.clear()
    stop.set()
    assert hitl.wait_for_done(sess, b, None, clicked, stop, timeout_s=5)[0] == "stopped"


def test_executor_takeover():
    """actions.Executor: real-input watcher logic, called directly (listeners stubbed, no input is sent)."""
    from rbot.desktop import actions

    class Dummy:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    orig = actions.mouse.Listener, actions.keyboard.GlobalHotKeys
    actions.mouse.Listener = actions.keyboard.GlobalHotKeys = Dummy
    try:
        ex = actions.Executor()
    finally:
        actions.mouse.Listener, actions.keyboard.GlobalHotKeys = orig
    x0, y0 = ex._last
    # no handler: old behaviour (3 s pause), clicks ignored
    ex._on_move(x0 + 40, y0, False)
    assert ex.paused_until > time.time() and not ex.takeover_pending
    ex.paused_until = 0.0
    # handler armed: grace period first (moving away from the chat window / question card)
    calls = []
    ex.takeover_handler = lambda: calls.append(1)
    ex._on_move(x0 + 100, y0, False)
    assert not ex.takeover_pending and ex._last == (x0 + 100, y0)
    ex._grace_until = 0.0
    ex._on_move(x0 + 105, y0, False)                     # < 15 px: jitter, ignored
    ex._on_move(x0 + 300, y0, True)                      # injected (our own input): ignored
    assert not ex.takeover_pending and not calls
    ex._on_move(x0 + 200, y0, False)                     # moving the mouse (even far) is NOT a takeover
    ex._on_move(x0 + 900, y0, False)
    assert not ex.takeover_pending and not calls
    ex._on_click(x0 + 900, y0, None, True, False)        # a real click IS a takeover
    assert ex.takeover_pending and calls == [1] and ex.paused_until == 0.0
    try:
        ex._check()
        raise AssertionError("expected HumanTakeover")
    except actions.HumanTakeover:
        pass
    assert not ex.takeover_pending
    # a real click is a takeover too; wait() notices it
    ex._on_click(5, 5, None, True, False)
    try:
        ex.wait(5)
        raise AssertionError("expected HumanTakeover")
    except actions.HumanTakeover:
        pass
    # during a human turn everything is ignored; ending it re-bases + grace
    ex.human_turn = True
    ex._on_move(x0 + 900, y0, False)
    ex._on_click(5, 5, None, True, False)
    assert not ex.takeover_pending
    ex.human_turn = False
    assert ex._grace_until > time.time()
    ex._on_click(5, 5, None, True, False)                # within grace: ignored
    assert not ex.takeover_pending
    ex.takeover_handler = None                           # disarmed -> back to pause
    ex._grace_until = 0.0
    ex._last = (0, 0)
    ex._on_click(5, 5, None, True, False)
    assert not ex.takeover_pending and ex.paused_until == 0.0
    # kill switch still wins
    ex.killed = True
    try:
        ex._check()
        raise AssertionError("expected KillSwitch")
    except actions.KillSwitch:
        pass


# ============================================================================ optional live review
def _form(title, lines, color):
    im = Image.new("RGB", (1600, 900), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1600, 60], fill=(230, 230, 230))
    d.text((20, 20), "Chrome  -  " + title, fill="black")
    d.rectangle([500, 160, 1100, 700], outline=color, width=4)
    y = 200
    for kind, text in lines:
        if kind == "h":
            d.text((540, y), text, fill="black", font_size=40)
            y += 80
        elif kind == "field":
            d.text((540, y), text, fill="black", font_size=22)
            d.rectangle([540, y + 30, 1060, y + 80], outline="gray", width=2)
            y += 110
        elif kind == "btn":
            d.rectangle([540, y, 1060, y + 60], fill=color)
            d.text((740, y + 15), text, fill="white", font_size=26)
            y += 90
        else:
            d.text((540, y), text, fill="black", font_size=24)
            y += 50
    return im


def live_review(verbose=True):
    """One real gateway call: a 'Sign in' form before, a 'Welcome' page after."""
    before = _form("Sign in - Contoso", [("h", "Sign in"), ("field", "Email"), ("field", "Password"),
                                         ("btn", "Sign in")], (37, 99, 235))
    after = _form("Contoso - Home", [("h", "Welcome back, Karthi!"), ("t", "You are signed in."),
                                     ("t", "Inbox (3)   Projects   Settings"), ("btn", "Open Inbox")], (22, 163, 74))
    loop = HumanLoop(None, FakeUI(), FakeEx(Screen()), None)
    summary = FakeSession(".", "login").stop()
    t0 = time.time()
    msgs = loop.review_messages("login", "login needed: sign in + password field",
                                "open my Contoso inbox and count the unread mails",
                                "get past the login screen (sign in + password field) so the agent can continue",
                                ["1. open_url \"contoso.com\" -> ok"], summary, [before, after], "done",
                                "blocker gone + idle 3s", 38)
    reply = loop.llm(msgs, True)
    rv = parse_review(reply)
    if verbose:
        print(f"LIVE review ({time.time() - t0:.1f}s): {rv}\n  raw tail: {strip(reply)[-300:]!r}")
    return rv


def strip(t):
    return human_loop.strip_think(t)


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    import inspect
    for k, fn in tests:
        if "engine" in inspect.signature(fn).parameters:
            for e in sorted(ENGINES):
                ENGINE = e
                fn(e)
                print("ok  ", k, f"[{e}]")
            ENGINE = "legacy"
        else:
            fn()
            print("ok  ", k)
    if os.getenv("HITL_LIVE"):
        rv = live_review()
        assert rv["verdict"] == "done", rv
    print(f"\nALL {len(tests)} HUMAN-LOOP TESTS PASSED")
