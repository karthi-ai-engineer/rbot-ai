"""The agent loop of agent.run_task as a LangGraph StateGraph (same behaviour, explicit structure, Hub events).

  from rbot.core.agent_graph import get_runner
  run = get_runner(on_event=print)          # BUDDY_ENGINE=legacy -> agent.run_task (on_event dropped)
  kind, text = run(task, executor, cursor, 25, stop=stop, run_dir=run_dir, guard=..., hitl=...)

Graph (one step = begin_step .. end_step; one attempt = observe .. record). Every node can also go to END
(kill switch -> "killed", stop / Stop button -> "stopped", unexpected exception -> "error", re-raised).

  START -> begin_step --(max_steps reached)-----------------------------------------------> END
             |  hitl: check_blocker --(blocker turn ended the task)------------------------> END
             |        check_blocker --(mouse grabbed)--> takeover[pre] --+
             v        check_blocker --(no blocker / human done)----------+
   +------> observe <----------------------------------------------------+
   |          |
   |        decide ---(model call error)---------------------------+
   |          |                                                    |
   |        validate --(bad reply / args / same 3x / circles)------+
   |          |                                                    |
   |        aim (zoom refine) ---(BadAction)-----------------------+
   |          |                                                    |
   |        guard ---(BLOCKED)-------------------------------------+
   |          |                                                    v
   |        act --(ask_human / done / fail / no-verify action)--> record
   |          |  (executor error, covered, user grabbed mouse) -->   ^ |
   |        verify --(changed, soft-ok, or "did not change")---------+ |
   |                                                                   |
   +-- escalate <--(failed, a higher level is left)--------------------+
   +-- stuck <-----(every level failed / circles; hitl, not 1st soft)--+  stuck: Try again -> observe (L1)
                                                                       |
       takeover[act] <--(user grabbed the mouse)-----------------------+
            |                                                          |
            +---------> end_step <--(ok / ladder done w/o human)-------+  <-- stuck: I'll do it / Stop / limit
                           |
                           +--> begin_step (next step) | END (done / fail / human ended it)

Nodes:
  begin_step     new step: adaptive start level (L2 needed twice -> start at L2), step-level fields reset
  check_blocker  (hitl only) stop / kill switch / takeover check, then HumanLoop.check_blocker: login, otp,
                 captcha, payment -> human handover; consent -> question. Blocks while the human works.
  takeover       the human's turn after a real mouse click (HumanTakeover): HumanLoop.handover("takeover")
  observe        one ATTEMPT at ladder level li: screenshot (cached), window roots for the hover check,
                 step_NN[_aK].jpg, fresh steps.jsonl node
  decide         the LLM at the current level (L1 thinking off, L2 thinking on, L3 = ESCALATION_MODEL)
  validate       parse the reply, check args, same-action-3x / going-in-circles, "fail" below the top level
  aim            zoom refine of x,y guesses (snap / in-box / zoomed LLM look)
  guard          caller's veto (chat_app.general_guard, teams_agent.make_guard); sees the REFINED args,
                 exactly like the legacy loop (that is why aim comes before guard)
  act            emits the "action" event (method = the router's first choice), then ask_human (HumanLoop.ask) or
                 Router.execute: command / browser tab / invisible UIA / real mouse (router.py); node["method"] =
                 what really did it. mouse_gate.MouseDenied (gate waited 300 s / user pressed Stop) -> "stopped"
  verify         screen diff after a visible action; soft rules ("nothing moved", "text box is now active")
  record         per-level counters, router.after_attempt (method failures / background fallback), steps.jsonl
                 node, history line, "step_result" event (+ "method"), routing
  escalate       failed attempt -> feedback for the model, next ladder level
  stuck          every level failed (or going in circles) with hitl -> HumanLoop.handle_stuck
                 (I'll do it / Try again + hint / Stop / limit)
  end_step       the step's outcome: human turn result, soft "no visible change" streak, done / fail, next step

State is plain JSON (see AgentState). PIL images, obs dicts and the raw model message live in a per-run side
cache (Run.cache) and the state only holds their keys (obs_ref / attempt_ref / msg_ref). A missing key reads
as "no current screenshot", so nodes simply re-observe.

Checkpointing / resume: run_task(checkpointer=...) (default: a fresh MemorySaver per run) checkpoints the state
after every node under thread_id = str(run_dir). A future Hub can pass a persistent saver (e.g.
langgraph-checkpoint-sqlite's SqliteSaver) and, after a crash, rebuild a Run with a fresh executor / hitl /
llm and call build_graph(run).invoke(None, {"configurable": {"thread_id": ...}, "recursion_limit": ...})
to continue from the last checkpoint. The side cache is empty after a restart: resuming at a node boundary
before observe (begin_step, check_blocker, observe, stuck, end_step) re-observes by itself; for a checkpoint
taken mid-attempt (decide..verify need the attempt screenshot) first rewind with
graph.update_state(config, {"next": "observe", "att": att - 1}, as_node="escalate").

Human in the loop stays BLOCKING inside nodes (HumanLoop waits on real human activity). Every human
interaction goes through Run.human_call(kind, fn) in check_blocker, takeover, act (ask_human, and the router's /
file commands' Yes-No confirmations: delete, overwrite, risky fallback), record (risky background fallback) and
stuck; switching one of them to langgraph's interrupt() later is a local change there: emit
needs_you, interrupt(), and feed the resumed value through the same post-processing (_apply_pre / hres).

Events (on_event(dict), called from the worker thread, exceptions swallowed): task_started, thinking, action,
step_result, needs_you, human_done, say, task_finished (always exactly once, also on errors).
needs_you / human_done come from a thin proxy around hitl.ui that notices the first handover() / ask() of a
human interaction, so the title/detail are exactly what the human sees.
"""
import copy
import functools
import json
import os
import time
from pathlib import Path
from typing import Optional, TypedDict

from rbot.core import agent
from rbot.core import router as router_mod
from rbot.core.agent import BadAction

try:
    from rbot.human.human_loop import MAX_STUCK_ASKS, TITLES
except Exception:  # noqa: BLE001  (human_loop needs Windows bits; the graph must still import)
    MAX_STUCK_ASKS, TITLES = 3, {}

END_ = "__end__"                       # == langgraph.graph.END
RESULTS = agent.RESULT_KINDS          # exceptions that end a run (MouseDenied too: the gate already waited)
ASK_TITLE = "Quick question"


class AgentState(TypedDict, total=False):
    task: str
    max_steps: int
    n_levels: int
    has_hitl: bool
    # run-level (legacy locals of run_task)
    step: int
    steps: int
    history: list            # prompt history lines
    keys: list               # executed action keys (same action 3x check)
    loops: list              # loop keys of executed clicks/moves (going in circles)
    last_box: Optional[list]
    per_level: dict
    humans: list             # summary.json human_turns
    carry: str               # feedback for the next step
    goal: str                # the agent's current goal (for handover / stuck texts)
    streak: int              # steps in a row that needed a higher level than the start level
    soft_streak: int         # steps in a row with "no visible change" at every level
    obs_ref: Optional[str]   # side-cache key of the current screenshot (None = observe again)
    # step-level
    start: int
    feedback: str
    final: Optional[dict]    # {"li", "name", "args"} of the accepted action
    soft: bool               # every failure of this step was only "no visible change"
    att: int
    hres: Optional[dict]     # a human turn that took the rest of this step
    # attempt-level
    li: int
    attempt_ref: Optional[str]
    msg_ref: Optional[str]
    node: dict               # the steps.jsonl node of this attempt
    name: Optional[str]
    args: dict               # (refined) args the executor uses; node["args"] keeps what the model said
    desc: str
    outcome: str
    key: Optional[str]
    lk: Optional[list]
    executed: bool
    took_over: bool
    asked: Optional[dict]
    # control
    next: str
    takeover_from: str       # "pre" (before the model call) | "act" (during an action)
    result: list             # [kind, text]


def _jsonable(x):
    return json.loads(json.dumps(x, ensure_ascii=False, default=str))


def _method(name, obs):
    """Method without run context (kept for callers); the run itself asks its Router (router.py)."""
    return router_mod.static_method(name, obs)


class _EventUI:
    """Proxy around HumanLoop.ui: the first handover()/ask() inside Run.human_call emits needs_you."""

    def __init__(self, inner, run):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_run", run)

    def __getattr__(self, name):
        val = getattr(self._inner, name)
        if name in ("handover", "ask") and callable(val):
            run = self._run

            def wrapped(*a, **kw):
                run._ui_prompt(name, a, kw)
                return val(*a, **kw)
            return wrapped
        return val

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


class Run:
    """Everything of one task run that is not JSON: injected callables, executor, hitl, log, side cache."""

    def __init__(self, task, executor, cursor, max_steps, observe, diff_ratio, llm, levels, run_dir, stop, settle,
                 guard, hitl, on_event):
        self.task, self.ex, self.cursor, self.max_steps = task, executor, cursor, max_steps
        self.observe, self.diff_ratio, self.llm, self.levels = observe, diff_ratio, llm, levels
        self.run_dir, self.stop, self.settle, self.guard, self.hitl = run_dir, stop, settle, guard, hitl
        self.on_event = on_event
        self.cache, self._seq = {}, 0
        self.router = None                # router.Router: picks the method per tool call (set by run_task)
        self.log = None
        self.error = None                 # unexpected exception inside a node -> re-raised after the graph
        self.last_state = None
        self._ctx = None                  # current human interaction {"kind", "emitted"}
        self._ui_ok = False               # proxy installed on hitl.ui

    # ---- events ----------------------------------------------------------------------------------------
    def emit(self, ev):
        if self.on_event is None:
            return
        try:
            self.on_event(ev)
        except Exception as e:  # noqa: BLE001  (a broken Hub must never break the run)
            print(f"on_event error: {type(e).__name__}: {e}", flush=True)

    def say(self, text, thinking=False):
        self.emit({"type": "say", "text": str(text)})
        if self.cursor:
            self.cursor.post("say", text, thinking)

    def _ui_prompt(self, name, a, kw):
        ctx = self._ctx
        if ctx is None or ctx["emitted"]:
            return
        if name == "handover":
            title = str(a[0] if a else kw.get("title", ""))
            detail = str(a[1] if len(a) > 1 else kw.get("detail", ""))
            kind = {v: k for k, v in TITLES.items()}.get(title) or {"blocker": "login"}.get(ctx["kind"], ctx["kind"])
        else:
            q = str(a[0] if a else kw.get("question", ""))
            kind = {"blocker": "consent"}.get(ctx["kind"], ctx["kind"])
            title, detail = (ASK_TITLE if kind == "ask" else TITLES.get(kind, ASK_TITLE)), q
        ctx["emitted"] = kind
        self.emit({"type": "needs_you", "kind": kind, "title": title, "detail": detail})

    def human_call(self, kind, fn):
        """Run one blocking human interaction (HumanLoop method) with needs_you / human_done events."""
        ctx = self._ctx = {"kind": kind, "emitted": None}
        if not self._ui_ok and kind in ("ask", "stuck", "takeover"):    # hitl without a ui: announce up front
            k = kind
            self.emit({"type": "needs_you", "kind": k, "title": ASK_TITLE if k == "ask" else TITLES.get(k, "Your turn"),
                       "detail": ""})
            ctx["emitted"] = k
        try:
            res = fn()
        except BaseException as e:
            if ctx["emitted"]:
                self.emit({"type": "human_done", "kind": ctx["emitted"], "outcome": "interrupted",
                           "note": f"{type(e).__name__}: {e}"[:200]})
            raise
        finally:
            self._ctx = None
        if kind == "ask":
            out = {"outcome": "answered" if res else "no_answer", "answer": res}
        else:
            out = res or {}
        if not ctx["emitted"] and res and not self._ui_ok:               # hitl without a ui: late but paired
            k = out.get("kind", kind)
            self.emit({"type": "needs_you", "kind": k, "title": TITLES.get(k, "Your turn"), "detail": ""})
            ctx["emitted"] = k
        if ctx["emitted"]:
            rv = out.get("review") or {}
            note = out.get("answer") or rv.get("explanation") or out.get("note") or out.get("how") or ""
            self.emit({"type": "human_done", "kind": ctx["emitted"], "outcome": str(out.get("outcome", "")),
                       "note": str(note)[:300]})
        return res

    # ---- side cache ------------------------------------------------------------------------------------
    def get(self, ref):
        return self.cache.get(ref) if ref else None

    def put(self, obj):
        self._seq += 1
        ref = f"obj{self._seq}"
        self.cache[ref] = obj
        return ref

    def prune(self, s):
        keep = {s.get("obs_ref"), s.get("attempt_ref"), s.get("msg_ref")}
        for k in [k for k in self.cache if k not in keep]:
            del self.cache[k]

    def cur_obs(self, s, fresh=False):
        """The current screenshot; observes a new one when there is none (legacy: `if obs is None: obs = observe()`)."""
        obs = None if fresh else self.get(s.get("obs_ref"))
        if obs is None:
            obs = self.observe()
            s["obs_ref"] = self.put(obs)
        return obs

    @staticmethod
    def drop_obs(s):
        s["obs_ref"] = None

    def marks(self, s):
        return {int(m["id"]): m for m in self.get(s["attempt_ref"])["marks"]}

    # ---- logging (same as the legacy loop) ---------------------------------------------------------------
    def human(self, s, step, res):
        """Log a human interaction (steps.jsonl node + trace + prompt history). -> final result or None."""
        rv = res.get("review") or {}
        kind, out = res.get("kind", "?"), res.get("outcome", "?")
        n = {"step": step, "type": "human", "reason": res.get("reason", ""), "kind": kind, "outcome": out,
             "how": res.get("how", ""), "waited_s": res.get("waited_s"), "summary": res.get("summary", ""),
             "review": res.get("review"), "answer": res.get("answer"),
             "action": f"human:{kind}", "args": {"reason": str(res.get("answer") or rv.get("verdict") or out)},
             "ok": out in agent.HUMAN_OK}
        self.log.write(json.dumps(n, ensure_ascii=False, default=str) + "\n")
        self.log.flush()
        s["humans"].append({"step": step, "kind": kind, "outcome": out, "waited_s": n["waited_s"],
                            "verdict": rv.get("verdict")})
        print(f"[step {step} HUMAN] {kind}: {out} ({n['how']}) {float(n['waited_s'] or 0):.0f}s"
              + (f" — review: {rv.get('verdict')}" if rv else "")
              + (f" — answer: {res['answer']!r}" if res.get("answer") else ""), flush=True)
        if res.get("history"):
            s["history"].append(f"{step}. {res['history']}")
        return agent._human_end(res)

    def reset_screen(self, s):
        s["obs_ref"], s["keys"], s["last_box"], s["loops"] = None, [], None, []

    def end(self, s, result):
        s["result"], s["next"] = list(result), END_

    # ================================================================================================ nodes
    def n_begin_step(self, s):
        if s["step"] >= self.max_steps:
            s["next"] = END_                                   # result stays "max_steps"
            return
        s["step"] += 1
        s["steps"] = s["step"]
        s["start"] = 1 if s["streak"] >= 2 and len(self.levels) > 1 else 0
        s.update(feedback=s["carry"], final=None, soft=True, carry="", att=0, hres=None, node={"fail_reason": ""},
                 li=s["start"])
        s["next"] = "check_blocker" if self.hitl is not None else "observe"

    def n_check_blocker(self, s):
        hitl = self.hitl
        if (self.stop is not None and self.stop.is_set()) or hitl.stop_requested():
            raise KeyboardInterrupt
        try:
            self.ex._check()                                   # kill switch / mouse grabbed while the model thought
            obs = self.cur_obs(s)
            pre = self.human_call("blocker", lambda: hitl.check_blocker(obs, self.task, s["history"]))
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ != "HumanTakeover":
                raise
            s["takeover_from"], s["next"] = "pre", "takeover"
            return
        self._apply_pre(s, pre)

    def _apply_pre(self, s, pre):
        """The human's turn before the model call (blocker / takeover): log it, end the task or continue the step."""
        s["next"] = "observe"
        if not pre:
            return
        end = self.human(s, s["step"], pre)
        if end:
            self.end(s, end)
            return
        s["feedback"] = pre.get("feedback") or s["feedback"]
        if pre.get("screen_changed"):
            self.reset_screen(s)

    def n_takeover(self, s):
        obs = self.get(s.get("obs_ref"))
        res = self.human_call("takeover", lambda: self.hitl.handover(
            "user took over", "takeover", obs, self.task, s["goal"] or "the agent's current step",
            history=s["history"]))
        if s["takeover_from"] == "pre":
            self._apply_pre(s, res)
        else:                                                  # the human's turn replaces the rest of this step
            s["hres"], s["next"] = _jsonable(res), "end_step"

    def n_observe(self, s):
        if self.stop is not None and self.stop.is_set():
            raise KeyboardInterrupt
        li, step = s["li"], s["step"]
        model, thinking = self.levels[li]
        s["att"] += 1
        att = s["att"]
        obs = self.cur_obs(s)
        if agent.HOVER_CHECK and "roots" not in obs and not obs.get("background"):   # which window is at each box
            obs["roots"] = agent.mark_roots(obs)
        obs["marked"].convert("RGB").save(
            self.run_dir / (f"step_{step:02d}.jpg" if att == 1 else f"step_{step:02d}_a{att}.jpg"), quality=85)
        s["attempt_ref"] = s["obs_ref"]
        s["node"] = {"step": step, "attempt": att, "level": li + 1, "model": model, "thinking": thinking,
                     "action": None, "args": None, "reason": "", "latency_s": None, "prompt_tokens": None,
                     "completion_tokens": None, "ok": False, "fail_reason": "", "diff": None, "method": None}
        s.update(name=None, args={}, desc="", outcome="", executed=False, took_over=False, asked=None, lk=None,
                 key=None, msg_ref=None)
        s["next"] = "decide"

    def n_decide(self, s):
        model, thinking = self.levels[s["li"]]
        node = s["node"]
        self.say("Thinking hard" if thinking else "Thinking", True)
        self.emit({"type": "thinking", "step": s["step"], "level": s["li"] + 1, "model": str(model),
                   "thinking": bool(thinking)})
        obs = self.get(s["attempt_ref"])
        t0 = time.time()
        try:
            message, usage = self.llm(model, agent.build_messages(self.task, s["history"], obs, s["feedback"]),
                                      thinking)
            node["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
            node["completion_tokens"] = getattr(usage, "completion_tokens", None)
        except Exception as e:  # noqa: BLE001  (network / gateway error -> try the next level)
            message = None
            node["fail_reason"] = f"model call error: {e}"[:300]
        node["latency_s"] = round(time.time() - t0, 2)
        s["msg_ref"] = self.put(message) if message is not None else None
        s["next"] = "validate" if message is not None else "record"

    def _fail(self, s, e):
        s["node"]["fail_reason"] = str(e)
        s["next"] = "record"

    def n_validate(self, s):
        node, li = s["node"], s["li"]
        try:
            name, args = agent.parse_reply(self.get(s["msg_ref"]))
            agent.check_args(name, args)
            s["name"], s["args"] = name, args
            node.update(action=name, args=copy.deepcopy(args), reason=str(args.get("reason", ""))[:100])
            desc = s["desc"] = agent.describe(name, args, self.marks(s))
            if name not in ("done", "fail"):
                s["goal"] = f"{node['reason']} ({desc})" if node["reason"] else desc
            key = s["key"] = agent.action_key(name, args)
            if name not in ("done", "fail", "wait") and s["keys"][-2:] == [key, key]:
                raise BadAction("same action 3 times in a row")
            lk = agent.loop_key(name, args)
            s["lk"] = list(lk) if lk else None
            if lk and agent.loop_count(lk, s["loops"][-agent.LOOP_WINDOW:]) >= agent.LOOP_REPEATS - 1:
                raise BadAction("going in circles: you already did this "
                                f"{agent.LOOP_REPEATS - 1} times in the last steps and the task "
                                "did not move forward. Try a DIFFERENT kind of action (press_and_hold if the page mentions holding, double_click, the keyboard) or ask_human")
            if name == "fail" and (li < len(self.levels) - 1 or self.hitl is not None):
                raise BadAction(f"gave up ({args.get('reason', '')}); a stronger level will retry")
        except BadAction as e:
            return self._fail(s, e)
        s["next"] = "aim"

    def n_aim(self, s):
        name, args, step, att, li = s["name"], s["args"], s["step"], s["att"], s["li"]
        s["next"] = "guard"
        if not (agent.ZOOM_REFINE and name in agent.REFINE_ACTIONS):
            return
        model = self.levels[li][0]
        tag = f"step_{step:02d}" + ("" if att == 1 else f"_a{att}")

        def save_zoom(suffix, im, tag=tag):
            fn = f"{tag}_zoom{suffix}.jpg"
            im.save(self.run_dir / fn, quality=85)
            return fn
        try:
            if any(args.get(p + "id") in (None, "") and args.get(p + "x") is not None
                   for p in (("from_", "to_") if name == "drag" else ("",))):
                self.say("Taking a closer look", True)
            new, rinfo, rnote = agent.refine_args(name, args, self.get(s["attempt_ref"]), self.marks(s), self.llm,
                                                  model, save_zoom)
        except BadAction as e:
            return self._fail(s, e)
        s["args"] = _jsonable(new)
        if rinfo:
            s["node"]["refine"] = _jsonable(rinfo)
            for r in rinfo:
                if r["status"] not in ("in_box",):
                    print(f"[step {step} L{li + 1}] refine {r['end']}: {r['status']} "
                          f"{r['orig']} -> {r.get('refined', r['orig'])}"
                          + (f" ({r['latency_s']:.1f}s)" if "latency_s" in r else ""), flush=True)
        if rnote:
            s["desc"] += f" [{rnote}]"

    def n_guard(self, s):
        name = s["name"]
        try:
            if self.guard and name not in ("done", "fail", "ask_human"):
                self.guard(name, s["args"], self.get(s["attempt_ref"]), self.marks(s))   # BadAction("BLOCKED")
        except BadAction as e:
            return self._fail(s, e)
        s["next"] = "act"

    def _succeed(self, s):
        s["node"]["ok"] = True
        if self.hitl is not None and s["executed"] and s["name"] != "ask_human":
            self.hitl.note_agent_window(self.get(s.get("obs_ref")))     # the window the agent really works in
        s["next"] = "record"

    def n_act(self, s):
        name, args, node, hitl = s["name"], s["args"], s["node"], self.hitl
        obs = self.get(s["attempt_ref"])
        marks = self.marks(s)
        self.emit({"type": "action", "step": s["step"], "level": s["li"] + 1, "name": name, "desc": s["desc"],
                   "reason": node["reason"], "method": self.router.plan(name, args, marks, obs)})
        if name in ("ask_human", "done", "fail"):
            node["method"] = "none"
        if name == "ask_human":
            q, ta = str(args.get("question", "")), time.time()
            ans = self.human_call("ask", lambda: hitl.ask(q, args.get("options"))) if hitl is not None else None
            if not ans and hitl is not None and hitl.stop_requested():
                raise KeyboardInterrupt("the user clicked Stop")
            if ans:
                s["outcome"] = f"USER ANSWERED: {ans}"
            elif hitl is None:
                s["outcome"] = "no human available - decide yourself, or call fail if you cannot"
            else:
                s["outcome"] = ("the user did not answer - decide yourself (choose the safest "
                                "option), or call fail if you cannot")
            if hitl is not None:
                s["asked"] = {"type": "human", "kind": "ask", "reason": q, "answer": ans,
                              "outcome": "answered" if ans else "no_answer", "how": "ask_human",
                              "waited_s": round(time.time() - ta, 1)}
            s["executed"] = True
            s["keys"].append(s["key"])
            return self._succeed(s)
        if name in ("done", "fail"):
            return self._succeed(s)
        self.say(node["reason"] or name)
        hov = {}
        try:                                                   # the router picks the method
            out = self.router.execute(name, args, marks, obs, info=hov)
        except BadAction as e:
            if hov:
                node["hover"] = _jsonable(hov)
            if isinstance(e, agent.Covered):
                self.drop_obs(s)                               # something popped up: look again
            return self._fail(s, e)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "KillSwitch":
                raise
            if type(e).__name__ == "HumanTakeover" and hitl is not None:
                s["took_over"] = True                          # nothing was executed
                return self._fail(s, "interrupted: the user took over the mouse")
            if router_mod.is_mouse_denied(e):                  # the gate already waited / user pressed Stop:
                raise                                          # the task ends ("stopped")
            s["executed"] = True
            self.drop_obs(s)
            return self._fail(s, f"executor error: {e}")
        finally:
            node["method"] = self.router.method
        s["executed"], s["outcome"] = True, out
        if hov:
            node["hover"] = _jsonable(hov)
        s["keys"].append(s["key"])
        if s["lk"]:
            s["loops"].append(s["lk"])
        if not self.router.keeps_screen(name):
            self.drop_obs(s)                                   # screen changed (or might have)
        if name in agent.VERIFY or name == "scroll":
            s["next"] = "verify"
            return
        self._succeed(s)

    def n_verify(self, s):
        name, args, node = s["name"], s["args"], s["node"]
        marks = self.marks(s)
        before = self.get(s["attempt_ref"])["image"]
        if self.settle:
            time.sleep(agent.SETTLE.get(name, agent.SETTLE_DEFAULT))
        obs = self.cur_obs(s, fresh=True)
        d = self.diff_ratio(before, obs["image"])
        tid = args.get("id")                                   # small changes get lost in a full-screen diff
        box = marks[int(tid)]["box"] if str(tid).isdigit() and int(tid) in marks else s["last_box"]
        if box and before.size == obs["image"].size:
            W, H = before.size
            c = (max(0, box[0] - 120), max(0, box[1] - 120), min(W, box[2] + 120), min(H, box[3] + 120))
            d = max(d, self.diff_ratio(before.crop(c), obs["image"].crop(c)))
            if str(tid).isdigit() and marks.get(int(tid), {}).get("type") in agent.TEXT_TYPES:
                s["last_box"] = list(box)
        node["diff"] = round(d, 4)
        if node["diff"] < agent.DIFF_MIN:
            tgt = marks.get(int(tid)) if str(tid).isdigit() else None
            if name == "scroll":
                s["outcome"] = "nothing moved (maybe already at the end)"
            elif name == "click" and ((tgt and tgt.get("type") in ("Edit", "Document"))
                                      or (not tgt and not obs.get("background")
                                          and agent._is_textbox_at(args, marks, obs))):
                s["outcome"] = ("ok: the text box is now active (no visible change is normal) - next, use type_text, "
                                "do NOT click it again")
            else:
                return self._fail(s, "it did not change the screen")
        self._succeed(s)

    def n_record(self, s):
        node, li, step = s["node"], s["li"], s["step"]
        lv = s["per_level"][f"L{li + 1}"]
        lv["attempts"] += 1
        lv["ok"] += node["ok"]
        marks = self.marks(s) if self.get(s.get("attempt_ref")) is not None else {}
        if self.router.after_attempt(node, s["executed"], s["name"], s["args"], marks):
            self.drop_obs(s)                                   # background: switched between tab and real screen
        confirms = self.router.take_confirms()
        if confirms:
            node["confirm"] = _jsonable(confirms)
        self.log.write(json.dumps(node, ensure_ascii=False, default=str) + "\n")
        self.log.flush()
        diff = f", diff {node['diff']:.3f}" if node["diff"] is not None else ""
        status = "ok" if node["ok"] else f"FAIL: {node['fail_reason']}"
        print(f"[step {step} L{li + 1}] {s['desc'] or '(no action)'} {status} ({node['latency_s']:.1f}s{diff})",
              flush=True)
        self.emit({"type": "step_result", "step": step, "level": li + 1, "ok": bool(node["ok"]), "desc": s["desc"],
                   "detail": s["outcome"] if node["ok"] else node["fail_reason"],
                   "latency_s": float(node["latency_s"] or 0.0), "diff": node["diff"],
                   "method": str(node.get("method") or "none")})
        if s["executed"]:                                      # the prompt history only shows what really happened
            s["history"].append(f"{step}. {s['desc']} -> "
                                + (s["outcome"] if node["ok"] else f"FAILED: {node['fail_reason']}"))
        if s["asked"]:
            self.human(s, step, s["asked"])
        if s["took_over"]:
            s["takeover_from"], s["next"] = "act", "takeover"
            return
        if node["ok"]:
            s["final"], s["next"] = {"li": li, "name": s["name"], "args": s["args"]}, "end_step"
            return
        s["soft"] = s["soft"] and node["fail_reason"] == "it did not change the screen"
        if self.hitl is not None and node["fail_reason"].startswith("going in circles"):
            s["next"] = self._ladder_done(s)                   # a loop: don't burn more levels, ask the human now
        elif li + 1 < len(self.levels):
            s["next"] = "escalate"
        else:
            s["next"] = self._ladder_done(s)

    def _ladder_done(self, s):
        """Every level failed: ask the human (stuck) unless there is none or it was only a first soft no-change."""
        if self.hitl is None or (s["soft"] and s["soft_streak"] == 0):
            return "end_step"
        return "stuck"

    def n_escalate(self, s):
        s["feedback"] = (f"Your last attempt ({s['desc'] or 'reply'}) failed: {s['node']['fail_reason']}. "
                         "Try something else.")
        s["li"] += 1
        s["next"] = "observe"

    def n_stuck(self, s):
        obs = self.cur_obs(s)
        hres = self.human_call("stuck", lambda: self.hitl.handle_stuck(
            s["goal"] or "the next step of the task", s["node"]["fail_reason"], obs, self.task, s["history"]))
        if hres.get("outcome") == "retry":                     # "Try again" / a free-text hint -> L1 again
            self.human(s, s["step"], hres)
            s["feedback"] = hres.get("feedback") or "The user asked you to try again."
            if str((s.get("node") or {}).get("fail_reason", "") or s.get("fail_reason", "")).startswith("going in circles"):
                s["loops"], s["keys"] = [], []
                s["feedback"] += (" Last time the same clicks led nowhere. Redo the steps, but do the one that "
                                  "failed in a different way: press_and_hold if the page mentions holding, "
                                  "double_click, or the keyboard.")
            s.update(start=0, soft=True, hres=None, li=0)
            s["next"] = "observe"
            return
        s["hres"], s["next"] = _jsonable(hres), "end_step"

    def n_end_step(self, s):
        step, hres, final = s["step"], s["hres"], s["final"]
        s["next"] = "begin_step"
        if hres is not None:                                   # a human turn took the rest of this step
            end = self.human(s, step, hres)
            if end:
                return self.end(s, end)
            if hres.get("kind") == "stuck" and hres.get("outcome") != "done":
                return self.end(s, ("fail", f"step {step}: every level failed ({s['node']['fail_reason']}); "
                                            f"{hres.get('note') or 'no answer from the user'}"))
            s["carry"] = hres.get("feedback", "")
            if hres.get("screen_changed", True):
                self.reset_screen(s)
            return
        if final is None and s["soft"]:                        # nothing broke, the screen just didn't change
            s["soft_streak"] += 1
            s["history"].append(f"{step}. (no visible effect at any level; continuing)")
            print(f"[step {step}] no visible change at any level -> continuing", flush=True)
            return
        if final is None:
            return self.end(s, ("fail", f"step {step}: every level failed ({s['node']['fail_reason']})"))
        li, name, args = final["li"], final["name"], final["args"]
        s["soft_streak"] = 0
        s["streak"] = s["streak"] + 1 if li > s["start"] else 0
        if name == "done":
            self.end(s, ("done", str(args.get("summary", ""))))
            self.say("Done!")
        elif name == "fail":
            self.end(s, ("fail", str(args.get("reason", ""))))
            self.say("I couldn't do it")

    # ---- wiring ------------------------------------------------------------------------------------------
    def wrap(self, fn):
        """Node = copy of the state, mutated by fn. Ends of the run (kill switch, stop, takeover without hitl)
        become result + END; any other exception ends the graph and is re-raised by run_task (legacy: "error")."""
        @functools.wraps(fn)
        def node(state):
            s = copy.deepcopy(dict(state))
            try:
                fn(s)
            except BaseException as e:  # noqa: BLE001
                res, text = agent.end_result(e)
                s["result"], s["next"] = [res, text], END_
                if res == "error":
                    self.error = e
                else:
                    self.say("Stopped")
            self.prune(s)
            self.last_state = s
            return s
        return node


# every node and where it can go (also the drawn graph)
EDGES = {
    "begin_step": ["check_blocker", "observe", END_],
    "check_blocker": ["observe", "takeover", END_],
    "takeover": ["observe", "end_step", END_],
    "observe": ["decide", END_],
    "decide": ["validate", "record", END_],
    "validate": ["aim", "record", END_],
    "aim": ["guard", "record", END_],
    "guard": ["act", "record", END_],
    "act": ["verify", "record", END_],
    "verify": ["record", END_],
    "record": ["takeover", "end_step", "escalate", "stuck", END_],
    "escalate": ["observe", END_],
    "stuck": ["observe", "end_step", END_],
    "end_step": ["begin_step", END_],
}


def build_graph(run, checkpointer=None):
    from langgraph.graph import END, START, StateGraph
    g = StateGraph(AgentState)
    for name in EDGES:
        g.add_node(name, run.wrap(getattr(run, "n_" + name)))
    g.add_edge(START, "begin_step")

    def route(s):
        return END if s.get("next", END_) == END_ else s["next"]
    for name, dests in EDGES.items():
        g.add_conditional_edges(name, route, {d: (END if d == END_ else d) for d in dests})
    return g.compile(checkpointer=checkpointer)


def recursion_limit(max_steps, n_levels):
    """Node visits a run may need: per step 5 bookkeeping nodes + 9 per attempt, every level, plus the stuck
    retries ("try again" restarts the ladder; HumanLoop allows MAX_STUCK_ASKS per task)."""
    per_step = 6 + 9 * max(n_levels, 1) * (MAX_STUCK_ASKS + 2)
    return max(1000, max_steps * per_step + 100)


def run_task(task, executor, cursor=None, max_steps=25, *, observe=None, diff_ratio=None, llm=None,
             levels=None, run_dir=None, stop=None, settle=True, guard=None, hitl=None, on_event=None,
             keep_images=False, checkpointer=None, mode="live", invisible=None, browser_ex=None, observe_tab=None,
             allowed_folders=None):
    """Same contract as agent.run_task -> ("done"|"fail"|"max_steps"|"stopped"|"killed"|"error", text),
    plus on_event(dict) for the Hub and keep_images (False: screenshots deleted when the task ends).
    checkpointer: a langgraph checkpointer (default: a new MemorySaver); thread_id = str(run_dir).
    mode / invisible / browser_ex / observe_tab / allowed_folders: the method router, see agent.run_task."""
    real_screen = observe is None
    if observe is None or diff_ratio is None:
        from rbot.desktop import perception
        observe, diff_ratio = observe or perception.observe, diff_ratio or perception.diff_ratio
    llm = llm or agent.make_llm()
    levels = list(levels or agent.make_levels())
    run_dir = Path(run_dir or agent.RUNS / f"agent_{time.strftime('%Y%m%d_%H%M%S')}")
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(task, executor, cursor, max_steps, observe, diff_ratio, llm, levels, run_dir, stop, settle, guard,
              hitl, on_event)
    run.router = router_mod.Router(
        executor, cursor, task=task, mode=mode, observe=observe, real_screen=real_screen, invisible=invisible,
        browser_ex=browser_ex, observe_tab=observe_tab, hitl=hitl, allowed=allowed_folders, say=run.say, stop=stop,
        ask=(lambda q, o: run.human_call("ask", lambda: hitl.ask(q, o))) if hitl is not None else None)
    run.observe = observe = run.router.observe       # the real screen, or the buddy browser tab in background mode
    t_start = time.time()
    per_level = {f"L{i + 1}": {"attempts": 0, "ok": 0} for i in range(len(levels))}
    init = AgentState(task=task, max_steps=max_steps, n_levels=len(levels), has_hitl=hitl is not None, step=0,
                      steps=0, history=[], keys=[], loops=[], last_box=None, per_level=per_level, humans=[],
                      carry="", goal="", streak=0, soft_streak=0, obs_ref=None, start=0,
                      feedback="", final=None,
                      soft=True, att=0, hres=None, li=0, attempt_ref=None, msg_ref=None, node={"fail_reason": ""},
                      name=None, args={}, desc="", outcome="", key=None, lk=None, executed=False, took_over=False,
                      asked=None, next="begin_step", takeover_from="", result=["max_steps",
                                                                               f"stopped after {max_steps} steps"])
    run.last_state = init
    result, raise_exc = tuple(init["result"]), None
    print(f"task: {task}\nlog:  {run_dir}", flush=True)
    run.emit({"type": "task_started", "task": task, "run_dir": str(run_dir), "t": t_start})
    ui_orig = None
    try:
        run.log = open(run_dir / "steps.jsonl", "a", encoding="utf-8")
        if hitl is not None:
            if hasattr(hitl, "ui"):                            # needs_you / human_done from what the human sees
                ui_orig = hitl.ui
                hitl.ui = _EventUI(ui_orig, run)
                run._ui_ok = True
            if run.router.mode == "background":     # human turns on the tab: its window comes to the front
                hitl.handover = run.router.wrap_handover(hitl.handover)
            hitl.bind(observe=observe, stop=stop, run_dir=run_dir)
            executor.takeover_handler = hitl.on_takeover
        if checkpointer is None:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
        graph = build_graph(run, checkpointer)
        config = {"configurable": {"thread_id": str(run_dir)},
                  "recursion_limit": recursion_limit(max_steps, len(levels))}
        final = graph.invoke(init, config)
        result = tuple(final["result"])
        if run.error is not None:
            raise_exc = run.error
    except BaseException as e:  # noqa: BLE001  (outside the nodes: graph machinery, log file, hitl.bind ...)
        result = agent.end_result(e)
        if result[0] == "error":
            raise_exc = e
        else:
            run.say("Stopped")
    finally:
        if run.log is not None:
            run.log.close()
        run.router.close()
        if hitl is not None:
            if ui_orig is not None:
                try:
                    hitl.ui = ui_orig
                except Exception:  # noqa: BLE001
                    pass
            try:
                executor.takeover_handler = None
                if run.router.mode == "background":
                    del hitl.handover
            except Exception:  # noqa: BLE001
                pass
        st = run.last_state or init
        steps = st.get("steps", 0)
        summary = {"task": task, "result": result[0], "message": result[1], "steps": steps,
                   "attempts_per_level": st.get("per_level", per_level),
                   "levels": [{"model": m, "thinking": t} for m, t in levels],
                   "total_time_s": round(time.time() - t_start, 1)}
        if st.get("humans"):
            summary["human_turns"] = st["humans"]
        summary["engine"] = "graph"
        try:
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                                  encoding="utf-8")
            print(f"result: {result[0]} - {result[1]} ({steps} steps, {summary['total_time_s']}s)", flush=True)
        finally:
            if not keep_images:
                agent.cleanup_images(run_dir)
            run.emit({"type": "task_finished", "result": result[0], "text": result[1], "steps": steps,
                      "seconds": round(time.time() - t_start, 2), "run_dir": str(run_dir)})
    if raise_exc is not None:
        raise raise_exc
    if cursor and result[0] == "done":
        time.sleep(1.5)                                        # let the user see "Done!"
    return result


def get_runner(on_event=None):
    """The agent loop to use: BUDDY_ENGINE=graph (default) -> agent_graph.run_task (with on_event),
    BUDDY_ENGINE=legacy -> agent.run_task (on_event is not passed)."""
    if os.getenv("BUDDY_ENGINE", "graph").strip().lower() == "legacy":
        return agent.run_task
    if on_event is None:
        return run_task
    return functools.partial(run_task, on_event=on_event)
