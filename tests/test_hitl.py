"""Tests for hitl.py.   python -m pytest tests/test_hitl.py -q      (unit tests only; no input, no screen)
                        set HITL_LIVE=1 to also run the live tests (3 s HumanSession + one real detect_blocker),
                        which only READ the screen / install passive input listeners — they never move or type.
"""
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from rbot.human import hitl  # noqa: E402

LIVE = os.environ.get("HITL_LIVE") == "1"


# ============================================================================ detect_blocker (fake obs)
@pytest.fixture
def fake_screen(monkeypatch):
    """No real UIA / window: password box answer is controlled by the test."""
    state = {"pw": None, "rect": None}
    monkeypatch.setattr(hitl, "_password_field", lambda budget_s=0.25: state["pw"])
    monkeypatch.setattr(hitl, "_fg_rect", lambda: state["rect"])
    monkeypatch.setattr(hitl, "_fg_hwnd", lambda: 0)
    hitl._cache = None
    return state


def obs(window, *marks, box=(10, 10, 50, 30)):
    return {"window": window, "origin": (0, 0), "size": (1920, 1080),
            "marks": [{"id": i, "name": n, "type": t, "box": list(box)} for i, (n, t) in enumerate(marks)]}


def kind(o):
    b = hitl.detect_blocker(o)
    return b.kind if b else None


def test_login_password_field_uia(fake_screen):
    fake_screen["pw"] = "Enter your password"
    b = hitl.detect_blocker(obs("Sign in - Google Accounts - Google Chrome", ("Next", "Button")))
    assert b.kind == "login" and b.auto_resume and "password field 'Enter your password'" in b.evidence
    assert b.window.startswith("Sign in")


def test_login_text_cues(fake_screen):
    assert kind(obs("Sign in - Google Accounts", ("Email or phone", "Edit"), ("Next", "Button"),
                    ("Forgot email?", "Button"))) == "login"
    assert kind(obs("Example", ("Username", "Edit"), ("Password", "Edit"), ("Log in", "Button"))) == "login"
    assert kind(obs("Log into Facebook", ("Email address or phone number", "Edit"))) == "login"
    assert kind(obs("ログイン - サービス", ("メールアドレス", "Edit"))) == "login"


def test_login_false_positives(fake_screen):
    # an ordinary page with a 'Sign in' link / button is NOT a login wall
    assert kind(obs("The Daily News - Home", ("Sign in", "Hyperlink"), ("Subscribe", "Button"),
                    ("Search", "Edit"))) is None
    assert kind(obs("The Daily News - Home", ("Sign in", "Button"), ("Email address", "Edit"),
                    ("Sign up for our newsletter", "Button"))) is None
    assert kind(obs("Sign in", ("OK", "Button"))) is None          # single cue only
    assert kind(obs("Help Center", ("Sign in", "Hyperlink"), ("Reset your password", "Hyperlink"))) is None
    assert kind(obs("Notepad", ("File", "MenuItem"), ("Edit", "MenuItem"))) is None


def test_otp(fake_screen):
    assert kind(obs("Google", ("Enter code", "Edit"), ("2-Step Verification", "Hyperlink"))) == "otp"
    assert kind(obs("Verify it's you", ("Didn't get a code?", "Hyperlink"), ("Code", "Edit"))) == "otp"
    assert kind(obs("Microsoft account", ("Enter the code we sent", "Edit"))) == "otp"
    assert kind(obs("ログイン", ("確認コード", "Edit"))) == "otp"       # otp outranks the login title
    # settings page merely listing 2-step options is not an OTP prompt
    assert kind(obs("Security - Google Account", ("2-Step Verification", "Hyperlink"),
                    ("Authenticator", "Hyperlink"))) is None


def test_captcha(fake_screen):
    assert kind(obs("Some site", ("I'm not a robot", "CheckBox"))) == "captcha"
    assert kind(obs("Just a moment...", ("Verify you are human", "CheckBox"))) == "captcha"
    assert kind(obs("Site", ("recaptcha challenge expires in two minutes", "Document"))) == "captcha"
    assert kind(obs("Site", ("I’m not a robot", "CheckBox"))) == "captcha"   # curly apostrophe
    # invisible-reCAPTCHA badge on an ordinary page is not a challenge
    assert kind(obs("Contact us", ("reCAPTCHA", "Document"), ("Privacy", "Hyperlink"),
                    ("Terms", "Hyperlink"), ("Send", "Button"))) is None


def test_payment(fake_screen):
    b = hitl.detect_blocker(obs("Checkout", ("Card number", "Edit"), ("Pay", "Button")))
    assert b.kind == "payment" and not b.auto_resume
    assert kind(obs("Amazon.com Checkout", ("Place your order", "Button"))) == "payment"
    assert kind(obs("Shop", ("Billing address", "Edit"), ("Expiration date", "Edit"))) == "payment"
    # cart / product pages are not payment walls
    assert kind(obs("Cart - Shop", ("Proceed to checkout", "Button"), ("Payment methods", "Hyperlink"))) is None
    assert kind(obs("Shop", ("Buy now", "Button"), ("Add to cart", "Button"))) is None
    # a masked CVC box is a payment field, not a login
    fake_screen["pw"] = "Security code (CVC)"
    assert kind(obs("Checkout")) == "payment"


def test_consent(fake_screen):
    b = hitl.detect_blocker(obs("News", ("Accept all", "Button"), ("Reject all", "Button")))
    assert b.kind == "consent" and not b.auto_resume
    assert kind(obs("News", ("Accept all cookies", "Button"))) == "consent"
    assert kind(obs("Maps", ("Allow", "Button"), ("Block", "Button"))) == "consent"
    assert kind(obs("site.com wants to show notifications", ("Allow", "Button"))) == "consent"
    assert kind(obs("News", ("Cookie settings", "Hyperlink"), ("Contact", "Hyperlink"))) is None   # footer link
    assert kind(obs("Settings", ("Allow", "Button"))) is None


def test_priority(fake_screen):
    fake_screen["pw"] = "Password"
    assert kind(obs("Sign in", ("I'm not a robot", "CheckBox"))) == "captcha"
    assert kind(obs("Sign in", ("Verification code", "Edit"))) == "otp"
    assert kind(obs("Sign in", ("Accept all cookies", "Button"))) == "login"
    fake_screen["pw"] = None
    assert kind(obs("Checkout", ("Card number", "Edit"), ("Accept all cookies", "Button"))) == "payment"


def test_marks_outside_foreground_window_ignored(fake_screen):
    fake_screen["rect"] = (0, 0, 800, 600)
    o = obs("Notepad", ("Sign in - Google Accounts - Google Chrome", "Button"), ("Password", "Edit"),
            box=(900, 1040, 1000, 1080))                         # e.g. taskbar button of another window
    assert kind(o) is None
    o = obs("Notepad", ("Username", "Edit"), ("Password", "Edit"), box=(100, 100, 300, 130))
    assert kind(o) == "login"


def test_blocker_present(fake_screen):
    o = obs("Google", ("Enter code", "Edit"))
    assert hitl.blocker_present("otp", o) and not hitl.blocker_present("login", o)
    assert not hitl.blocker_present("otp", obs("Inbox - Outlook", ("New mail", "Button")))


def test_detect_fast_on_text(fake_screen):
    o = obs("Some page", *[(f"Item number {i} sign up today", "Hyperlink") for i in range(100)])
    t = time.perf_counter()
    for _ in range(20):
        hitl._cache = None
        hitl.detect_blocker(o)
    assert (time.perf_counter() - t) / 20 < 0.05


# ============================================================================ wait_for_done (fake clock)
class Clock:
    def __init__(self):
        self.t = 1000.0
        self.at = []                                   # [(time, fn)] fired while sleeping

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        for item in list(self.at):
            if item[0] <= self.t:
                self.at.remove(item)
                item[1]()


class FakeSession:
    def __init__(self, clock, inputs=(), changes=()):
        self.c, self.t0 = clock, clock.t
        self.inputs = sorted(self.t0 + x for x in inputs)
        self.changes = sorted(self.t0 + x for x in changes)

    def _last(self, xs):
        return max([x for x in xs if x <= self.c.t] or [self.t0])

    def idle_seconds(self):
        return self.c.t - self._last(self.inputs)

    def stable_seconds(self):
        return self.c.t - self._last(self.changes)

    def activity_since(self, t):
        return any(t < x <= self.c.t for x in self.inputs)


def ok_to_observe(sess, at, idle_needed=3.0, stable_needed=2.0):
    """idle/stable conditions held at time t0+at."""
    t = sess.t0 + at
    last_in = max([x for x in sess.inputs if x <= t] or [sess.t0])
    last_ch = max([x for x in sess.changes if x <= t] or [sess.t0])
    return t - last_in >= idle_needed - 1e-6 and t - last_ch >= stable_needed - 1e-6


class Rig:
    def __init__(self, monkeypatch, kind="otp", auto=True, inputs=(), changes=(), gone_at=None,
                 click_at=None, stop_at=None):
        self.clock = Clock()
        monkeypatch.setattr(hitl, "_now", self.clock.now)
        monkeypatch.setattr(hitl, "_sleep", self.clock.sleep)
        monkeypatch.setattr(hitl, "detect_blocker", lambda o: None)
        monkeypatch.setattr(hitl, "blocker_present", lambda k, o: o["present"])
        self.t0 = self.clock.t
        self.session = FakeSession(self.clock, inputs, changes)
        self.blocker = hitl.Blocker(kind, "test", auto, "Win")
        self.clicked, self.stop = threading.Event(), threading.Event()
        if click_at is not None:
            self.clock.at.append((self.t0 + click_at, self.clicked.set))
        if stop_at is not None:
            self.clock.at.append((self.t0 + stop_at, self.stop.set))
        self.gone_at = gone_at
        self.obs_times, self.status = [], []

    def observe(self):
        self.obs_times.append(self.clock.t - self.t0)
        self.clock.t += 0.5                            # observe() is expensive
        return {"present": self.gone_at is None or self.clock.t - self.t0 < self.gone_at}

    def run(self, **kw):
        res = hitl.wait_for_done(self.session, self.blocker, self.observe, self.clicked, self.stop,
                                 on_status=lambda s: self.status.append((round(self.clock.t - self.t0, 1), s)),
                                 **kw)
        return res, self.clock.t - self.t0


def test_wait_clicked(monkeypatch):
    r = Rig(monkeypatch, click_at=5, inputs=[1, 2, 3, 4])
    (out, how), t = r.run()
    assert (out, how) == ("done", "clicked buddy") and 5 <= t < 5.3


def test_wait_auto_resume_with_countdown(monkeypatch):
    r = Rig(monkeypatch, inputs=[2, 4, 8], changes=[9], gone_at=10)
    (out, how), t = r.run()
    assert out == "done" and how.startswith("blocker gone + idle")
    msgs = [s for _, s in r.status]
    assert msgs == ["Looks like you're done — continuing in 3…", "Looks like you're done — continuing in 2…",
                    "Looks like you're done — continuing in 1…"]
    assert 13.5 <= t <= 15.5
    # observe() only once idle>=3 (t>=11) and stable>=2, at most once per second
    assert r.obs_times and all(ok_to_observe(r.session, x) for x in r.obs_times)
    assert all(b - a >= 1.0 for a, b in zip(r.obs_times, r.obs_times[1:]))


def test_wait_countdown_cancelled_by_activity(monkeypatch):
    r = Rig(monkeypatch, inputs=[2, 4, 8, 13], changes=[9], gone_at=10)
    (out, how), t = r.run()
    msgs = [s for _, s in r.status]
    assert "OK, still waiting…" in msgs
    i = msgs.index("OK, still waiting…")
    assert msgs[:i] and all("continuing in" in m for m in msgs[:i])
    assert msgs[i + 1:] == ["Looks like you're done — continuing in 3…",
                            "Looks like you're done — continuing in 2…",
                            "Looks like you're done — continuing in 1…"]
    assert out == "done" and t >= 16 + 3


def test_wait_blocker_still_present_keeps_waiting(monkeypatch):
    r = Rig(monkeypatch, inputs=[1], gone_at=None)            # human idle, waiting for an SMS
    (out, how), t = r.run(timeout_s=60)
    assert out == "timeout" and "60" in how and 60 <= t < 61.2
    assert [s for _, s in r.status] == ["Are you still there?"]
    assert len(r.obs_times) > 20                              # kept re-checking ~1/s
    assert not any("continuing" in s for _, s in r.status)


def test_wait_other_human_blocker_keeps_waiting(monkeypatch):
    r = Rig(monkeypatch, kind="login", gone_at=0)              # login gone ... but an OTP page appeared
    monkeypatch.setattr(hitl, "detect_blocker", lambda o: hitl.Blocker("otp", "x", True, "W"))
    (out, _), t = r.run(timeout_s=20)
    assert out == "timeout"


def test_wait_payment_needs_click(monkeypatch):
    r = Rig(monkeypatch, kind="payment", auto=False, inputs=[1, 2], gone_at=0, click_at=40)
    (out, how), t = r.run()
    assert (out, how) == ("done", "clicked buddy") and 40 <= t < 40.3
    assert r.obs_times == []                                   # never auto-checks
    r = Rig(monkeypatch, kind="payment", auto=False, gone_at=0)
    (out, how), t = r.run(timeout_s=30)
    assert out == "timeout" and r.obs_times == []


def test_wait_timeout_and_still_there_once(monkeypatch):
    r = Rig(monkeypatch, kind="consent", auto=False)
    (out, how), t = r.run(timeout_s=300)
    assert out == "timeout" and 300 <= t < 300.3
    assert len(r.status) == 1 and r.status[0][1] == "Are you still there?" and abs(r.status[0][0] - 150) < 0.2


def test_wait_stop(monkeypatch):
    r = Rig(monkeypatch, stop_at=7)
    (out, how), t = r.run()
    assert (out, how) == ("stopped", "stopped") and 7 <= t < 7.3


def test_wait_busy_human_no_observe(monkeypatch):
    r = Rig(monkeypatch, inputs=[x * 0.5 for x in range(1, 60)], gone_at=0, stop_at=29)
    (out, _), _ = r.run()
    assert out == "stopped" and r.obs_times == []


def test_wait_human_in_other_window(monkeypatch):
    r = Rig(monkeypatch, gone_at=0, stop_at=20)
    r.blocker.hwnd = 1234
    monkeypatch.setattr(hitl, "_window_alive", lambda h: True)
    monkeypatch.setattr(hitl, "_fg_hwnd", lambda: 999)         # e.g. reading the code in a mail app
    (out, _), _ = r.run()
    assert out == "stopped" and r.obs_times == []


def test_summary_text():
    s = {"duration_s": 38.2, "windows": [[0, "Sign in - Microsoft"], [20, "Outlook - Inbox"]],
         "clicks": [{"element": "Sign in", "type": "Button"}, {"element": "(password field)", "type": "Edit"}],
         "keys": {"total": 24, "fields": {"(password field)": 12, "Email": 12}}}
    txt = hitl._summary_text(s)
    assert txt == ("38 s, 2 clicks (Sign in button, (password field)), 24 keys into (password field)/Email, "
                   "window 'Sign in - Microsoft' -> 'Outlook - Inbox'")


# ============================================================================ live (read-only) tests
@pytest.mark.skipif(not LIVE, reason="set HITL_LIVE=1")
def test_live_human_session():
    from pynput.keyboard import KeyCode
    tmp = Path(tempfile.mkdtemp(prefix="hitl_test_"))
    try:
        s = hitl.HumanSession(tmp, "test session", fps=2.0)
        s.start()
        s.start()                                              # idempotent
        time.sleep(1.2)
        # synthetic callbacks only (no OS input is generated): 5 'keys' and one click resolution
        for _ in range(5):
            s._on_press(KeyCode.from_char("s"))
        s._on_click(5, 5, type("B", (), {"name": "left"})(), True)
        t_mid = time.time()
        time.sleep(1.8)
        assert s.idle_seconds() >= 0 and s.stable_seconds() >= 0
        summ = s.stop()
        assert s.stop() is summ                                # idempotent
        d = Path(summ["dir"])
        assert d.parent == tmp and d.name == "human_1"
        assert (d / "session.json").exists()
        frames = list(d.glob("frame_*.jpg"))
        assert len(frames) >= 4 and len(summ["frames"]) == len(frames)
        for k in ("reason", "started", "ended", "duration_s", "clicks", "keys", "windows", "frames",
                  "active_s", "idle_at_end_s", "text"):
            assert k in summ, k
        assert summ["keys"]["total"] >= 5 and sum(summ["keys"]["fields"].values()) == summ["keys"]["total"]
        assert any(c["x"] == 5 and c["y"] == 5 for c in summ["clicks"])
        c = next(c for c in summ["clicks"] if c["x"] == 5)
        assert set(c) >= {"t", "x", "y", "element", "type", "window"}
        raw = (d / "session.json").read_text(encoding="utf-8")
        assert '"s"' not in raw                                # no key characters stored
        kf = s.keyframes(4)
        assert 1 <= len(kf) <= 4 and all(hasattr(im, "size") for im in kf)
        assert s.activity_since(t_mid - 0.5) and not s.activity_since(time.time() + 1)
        print("\nsession:", {k: summ[k] for k in ("duration_s", "active_s", "idle_at_end_s")},
              "frames", len(frames), "clicks", len(summ["clicks"]), "keys", summ["keys"]["total"],
              "keyframes", len(kf), "errors", summ.get("errors"))
        s2 = hitl.HumanSession(tmp, "payment: card", fps=2.0)
        assert s2.private and s2.dir.name == "human_2"
        s2.start()
        time.sleep(1.2)
        summ2 = s2.stop()
        assert summ2["frames"] == [] and not list(s2.dir.glob("*.jpg")) and s2.keyframes() == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert not tmp.exists()


@pytest.mark.skipif(not LIVE, reason="set HITL_LIVE=1")
def test_live_detect_on_current_screen():
    from rbot.desktop import perception
    o = perception.observe()
    hitl._cache = None
    t = time.perf_counter()
    b = hitl.detect_blocker(o)
    dt = time.perf_counter() - t
    hitl._cache = None
    t = time.perf_counter()
    hitl.detect_blocker(o)
    dt2 = time.perf_counter() - t
    # report only the kind (evidence may contain private element names)
    print(f"\nlive detect_blocker: {b.kind if b else None}  first {dt * 1000:.0f} ms, warm {dt2 * 1000:.0f} ms, "
          f"{len(o['marks'])} marks")
    assert dt2 < 0.3


def test_stability_ignores_buddy_and_tracks_changes(monkeypatch, tmp_path):
    """Frame diff logic with synthetic frames: changes inside our own overlay window are masked out."""
    from PIL import Image, ImageDraw
    mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}

    def frame(buddy_y=0, big=False):
        im = Image.new("RGB", (1920, 1080), (240, 240, 240))
        d = ImageDraw.Draw(im)
        d.rectangle((20, 900 + buddy_y, 220, 1060 + buddy_y), fill=(255, 80, 160))   # animating buddy
        if big:
            d.rectangle((400, 200, 1500, 800), fill=(20, 20, 20))                       # page navigated
        return im

    seq = iter([frame(), frame(buddy_y=-20), frame(buddy_y=0), frame(big=True), frame(big=True, buddy_y=-20)])
    clock = Clock()
    monkeypatch.setattr(hitl, "_now", clock.now)
    monkeypatch.setattr(hitl, "_grab_fg_monitor", lambda sct: (next(seq), mon))
    monkeypatch.setattr(hitl, "_own_overlay_rects", lambda: [(0, 860, 600, 1080)])
    s = hitl.HumanSession(tmp_path, "t")
    s.started = s.last_input = s._last_change = clock.t
    changes = []
    for _ in range(5):
        clock.t += 1
        s._shoot(None)
        changes.append(s._last_change - s.started)
    assert changes == [0, 0, 0, 4, 4]          # only the real page change (4th frame) counts
    assert s.stable_seconds() == 1
    assert len(list(s.dir.glob("frame_*.jpg"))) == 5 and s._mem.keys() == {0, 4}
