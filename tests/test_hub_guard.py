import pytest

from rbot.hub import guard as hub_guard  # noqa: E402
from rbot.core.agent import BadAction  # noqa: E402

SELF = "Chat | Muthiah Karthi (You) | Microsoft Teams"
OTHER = "Chat | Appireddy Tharun | Microsoft Teams"


class Hitl:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def ask(self, q, options=None):
        self.asked.append(q)
        return self.answer


def make(title, answer="Yes", allowed=(), hitl=True):
    h = Hitl(answer) if hitl else None
    g = hub_guard.HubGuard(lambda: h, lambda: list(allowed), title=lambda: title)
    return g, h


def marks(*names):
    return {i: {"id": i, "name": n, "type": "Button", "box": [10, 10, 50, 30]} for i, n in enumerate(names)}


def test_current_chat():
    assert hub_guard.current_chat(SELF) == "Muthiah Karthi (You)"
    assert hub_guard.current_chat("Microsoft Teams") == ""
    assert hub_guard.current_chat("Activity | Microsoft Teams") == ""


def test_own_chat_send_never_asks():
    g, h = make(SELF)
    g("press_key", {"key": "enter"}, {}, {})
    g("type_text", {"text": "hi\n"}, {}, {})
    assert h.asked == []


def test_other_chat_send_asks_once_per_task():
    g, h = make(OTHER, "Yes")
    g("press_key", {"key": "enter"}, {}, {})
    g("press_key", {"key": "enter"}, {}, {})
    assert len(h.asked) == 1 and "Appireddy Tharun" in h.asked[0]
    g.new_task()
    g("press_key", {"key": "enter"}, {}, {})
    assert len(h.asked) == 2


def test_other_chat_send_no_blocks():
    g, h = make(OTHER, "No")
    with pytest.raises(BadAction, match="BLOCKED"):
        g("click", {"id": 0}, {}, marks("Send"))


def test_allowed_person_no_question():
    g, h = make(OTHER, allowed=["Tharun Appireddy"])          # name order varies in Teams
    g("press_key", {"key": "enter"}, {}, {})
    assert h.asked == []


def test_typing_without_send_is_free():
    g, h = make(OTHER)
    g("type_text", {"text": "draft"}, {}, {})
    g("click", {"id": 0}, {}, marks("Type a message"))
    assert h.asked == []


def test_call_always_asks_even_in_own_chat():
    g, h = make(SELF, "Yes")
    g("click", {"id": 0}, {}, marks("Video call"))
    g("click", {"id": 0}, {}, marks("Video call"))                 # approved for this task
    assert len(h.asked) == 1 and "call" in h.asked[0].lower()


def test_call_by_coordinates_uses_target_text():
    g, h = make(OTHER, "No")
    with pytest.raises(BadAction):
        g("click", {"x": 880, "y": 90, "target": "Video call icon in the chat header"}, {}, {})


def test_no_human_blocks_risky():
    g, _ = make(OTHER, hitl=False)
    with pytest.raises(BadAction, match="nobody is there"):
        g("press_key", {"key": "enter"}, {}, {})


def test_outside_teams_untouched():
    g, h = make("Notepad")
    g("press_key", {"key": "enter"}, {}, {})
    g("click", {"id": 0}, {}, marks("Call"))
    assert h.asked == []


def test_general_guard_still_applies():
    g, h = make("Notepad")
    with pytest.raises(BadAction):
        g("click", {"id": 0}, {}, marks("Delete account"))
