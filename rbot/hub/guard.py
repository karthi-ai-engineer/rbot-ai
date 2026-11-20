"""Hub safety guard = chat_app.general_guard + Teams rules that ASK the user first:

- a call (video/audio call, Meet now, Join, Share screen, Present) -> "Start a call? ..." Yes / No
- sending a message (Enter, the Send button, or typed text ending in a new line) in a Teams chat that is
  not your own "(You)" chat and not on the Hub's "People I may message" list -> "Send a message to X?" Yes / No

A Yes counts for the rest of that task (same chat / same call button). No, no answer, or no human available
-> BadAction("BLOCKED: ...") so the agent does not do it.
"""
import re

from rbot.desktop import perception
from rbot.apps import teams_agent
from rbot.core.agent import BadAction
from rbot.apps.chat_app import general_guard

CALL = re.compile(r"\b(video call|audio call|voice call|call|meet now|join|share screen|share|present|"
                  r"start (?:a )?(?:video |audio )?call)\b", re.I)
SEND_NAMES = re.compile(r"^\s*send\b", re.I)
NOT_A_CHAT = {"chat", "chats", "microsoft teams", "teams", "activity", "calendar", "calls", "files", ""}


def current_chat(title):
    """'Chat | Muthiah Karthi (You) | Microsoft Teams' -> 'Muthiah Karthi (You)' ('' if unknown)."""
    segs = [s.strip() for s in str(title or "").replace("​", "").split("|")]
    names = [s for s in segs if s.lower() not in NOT_A_CHAT]
    return names[0] if names else ""


def _target_text(name, args, marks):
    i = args.get("id")
    m = marks.get(int(i)) if str(i).isdigit() else None
    if m is not None:
        return str(m.get("name", ""))
    return " ".join(str(args.get(k, "")) for k in ("target", "reason"))


def _is_send(name, args, marks):
    if name == "press_key" and str(args.get("key", "")).strip().lower() in ("enter", "return"):
        return True
    if name == "type_text" and str(args.get("text", "")).endswith("\n"):
        return True
    if name in ("click", "double_click"):
        return bool(SEND_NAMES.search(_target_text(name, args, marks)))
    return False


class HubGuard:
    def __init__(self, get_hitl, get_allowed, title=None):
        self.get_hitl, self.get_allowed = get_hitl, get_allowed
        self.title = title or perception.foreground_title
        self.ok_chats, self.ok_calls = set(), set()

    def new_task(self):
        self.ok_chats.clear()
        self.ok_calls.clear()

    def _confirm(self, question, what):
        hitl = self.get_hitl()
        if hitl is None:
            raise BadAction(f"BLOCKED: {what} needs the user's OK and nobody is there to ask")
        ans = hitl.ask(question, ["Yes", "No"])
        if not (ans and str(ans).strip().lower().startswith(("yes", "y", "ok", "sure"))):
            raise BadAction(f"BLOCKED: the user did not allow {what} - do not try it again; tell the user "
                            f"with done or fail if the task cannot go on without it")

    def __call__(self, name, args, obs, marks):
        general_guard(name, args, obs, marks)
        title = self.title() or (obs or {}).get("window", "")
        if "Microsoft Teams" not in title:
            return
        if name in ("click", "double_click"):
            t = _target_text(name, args, marks)
            if CALL.search(t) and not SEND_NAMES.search(t):
                key = (current_chat(title), CALL.search(t).group(0).lower())
                if key not in self.ok_calls:
                    who = current_chat(title) or "this chat"
                    self._confirm(f"Start a Teams call? I'm about to click \"{t[:50]}\" in {who}.", "the call")
                    self.ok_calls.add(key)
        if _is_send(name, args, marks):
            chat = current_chat(title)
            allowed = teams_agent.variants(list(self.get_allowed() or []))
            if "(You)" in chat or chat in self.ok_chats or teams_agent.title_ok(title, allowed):
                return
            who = chat or "the open Teams chat"
            self._confirm(f"Send this Teams message to {who}? They're not on your list.", f"a message to {who}")
            self.ok_chats.add(chat)
