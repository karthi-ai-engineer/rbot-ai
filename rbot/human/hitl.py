"""Human-in-the-loop: notice when the agent must hand the screen to the human, watch what the human does,
and decide when they are finished (research prototype).

  detect_blocker(obs) -> Blocker | None        runs every agent step (<300 ms; text cues + one UIA query)
  blocker_present(kind, obs) -> bool           re-check while waiting
  HumanSession(run_dir, reason)                listeners + 1 fps frames while the human is in control (no LLM)
  wait_for_done(session, blocker, observe, clicked, stop, ...)   -> ("done"|"timeout"|"stopped", how)

Privacy: keystrokes are COUNTED only (never which key); typing into a password field is logged as
"(password field)". Sessions whose reason mentions payment keep no frames on disk and return no keyframes.
Everything here is safe to call from non-GUI worker threads (UIA work runs in threads with their own COM init).
"""
import concurrent.futures
import ctypes
import json
import math
import os
import queue
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from rbot.desktop import perception            # sets per-monitor DPI awareness on import; foreground_title(), diff_ratio()

_now = time.time             # patched by tests (fake clock)
_sleep = time.sleep

KINDS = ("captcha", "otp", "login", "payment", "consent")   # priority order, most specific first
HUMAN_KINDS = {"captcha", "otp", "login"}                    # the human types/solves these themselves
STABLE_DIFF = 0.002          # perception.diff_ratio below this = "screen unchanged"


@dataclass
class Blocker:
    kind: str                # "login" | "otp" | "captcha" | "payment" | "consent"
    evidence: str            # short human-readable why
    auto_resume: bool        # login/otp/captcha True; payment/consent False
    window: str              # foreground window title when detected
    hwnd: int = 0            # foreground window handle when detected (extra; 0 = unknown)


# ============================================================================ detection: text cues
# Each kind has cue groups: (group, weight, [phrases]). A group counts once. Phrases are lower-case;
# ascii phrases match on word boundaries, CJK phrases as substrings.
CUES = {
    "captcha": [
        ("robot", 2, ["i'm not a robot", "i am not a robot", "not a robot", "verify you are human",
                      "verify you're human", "are you a robot", "are you human", "confirm you are human",
                      "select all images", "select all squares", "ロボットではありません", "人間であることを確認"]),
        ("captcha", 1, ["captcha", "recaptcha", "hcaptcha", "turnstile"]),
        ("challenge", 1, ["challenge", "security check", "just a moment", "checking your browser",
                          "checking if the site connection is secure", "cloudflare"]),
    ],
    "otp": [
        ("code", 2, ["verification code", "enter code", "enter the code", "enter your code", "one-time",
                     "one time code", "one time password", "otp", "passcode", "6-digit", "6 digit",
                     "確認コード", "認証コード", "ワンタイム"]),
        ("sent", 1, ["we sent", "sent a code", "sent you a code", "code was sent", "texted", "text message",
                     "sms", "didn't get a code", "resend code", "コードを送信"]),
        ("factor", 1, ["authenticator", "2-step", "two-step", "2fa", "two-factor", "multi-factor", "mfa",
                       "2段階", "二段階"]),
        ("verify", 1, ["verify it's you", "verify your identity", "confirm it's you", "本人確認"]),
    ],
    "login": [
        ("signin", 1, ["sign in", "sign-in", "signin", "sign into", "log in", "log-in", "login", "log into",
                       "logon", "ログイン", "サインイン"]),
        ("password", 1, ["password", "パスワード"]),
        ("user", 1, ["email or phone", "username", "user name", "user id", "email address", "e-mail address",
                     "account name", "ユーザー名", "メールアドレス"]),
        ("stay", 1, ["keep me signed in", "stay signed in", "remember me", "ログインしたままにする",
                     "ログイン状態を保持"]),
    ],
    "payment": [
        ("card", 1, ["card number", "credit card number", "debit card number", "name on card", "cardholder",
                     "カード番号", "カード名義"]),
        ("cvv", 1, ["cvv", "cvc", "cvv2", "security code", "セキュリティコード", "セキュリティーコード"]),
        ("expiry", 1, ["expiry", "expiration date", "expiration", "exp date", "exp. date", "mm/yy", "mm / yy",
                       "有効期限"]),
        ("billing", 1, ["billing address", "請求先住所", "請求先"]),
        ("order", 1, ["pay now", "place order", "place your order", "complete purchase", "complete order",
                      "confirm purchase", "submit order", "confirm and pay", "注文を確定", "注文を確定する",
                      "購入を確定"]),
        ("checkout", 1, ["checkout", "check out", "レジに進む", "ご購入手続き"]),
        ("method", 1, ["payment method", "payment details", "支払い方法", "お支払い方法"]),
    ],
    "consent": [
        ("cookie_accept", 2, ["accept cookies", "accept all cookies", "allow all cookies", "allow cookies",
                              "accept cookie", "すべてのcookieを受け入れる"]),
        ("accept_all", 1, ["accept all", "allow all", "i agree", "agree and continue", "同意する", "すべて受け入れる",
                           "すべて許可"]),
        ("reject_all", 1, ["reject all", "decline all", "only necessary", "necessary only", "essential only",
                           "use necessary cookies only", "すべて拒否"]),
        ("cookie", 1, ["cookie", "cookies", "クッキー"]),
        ("notify", 2, ["allow notifications", "show notifications", "send you notifications",
                       "wants to send you notifications", "通知の表示"]),
        ("location", 2, ["allow location", "know your location", "use your location", "access your location",
                         "位置情報"]),
        ("permission", 1, ["permission", "permissions", "権限"]),
    ],
}
PAYMENT_HARD = {"card", "cvv", "expiry", "billing", "order"}
PAYMENT_FIELD = {"card", "cvv"}                     # one of these as an Edit name = card-like field
ALLOW_NAMES = {"allow", "許可"}
DENY_NAMES = {"block", "deny", "don't allow", "dont allow", "never allow", "ブロック", "許可しない"}
SAVE_PW_TAG = "\x00savepw:"
SAVE_PW_RE = re.compile(r"\b(save|update) (your |this )?password\??|パスワードを保存")   # browser's save-password bubble


def _norm(s):
    return (s or "").lower().replace("’", "'").replace("‘", "'").replace("\n", " ").strip()


def _phrase_re(p):
    if re.fullmatch(r"[\x00-\x7f]+", p):
        return re.compile(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])")
    return re.compile(re.escape(p))


_CUE_RE = {k: [(g, w, [(p, _phrase_re(p)) for p in ps]) for g, w, ps in groups] for k, groups in CUES.items()}


def _texts(obs):
    """[(text, where)] where = 'title' | mark type ('Edit', 'Button', ...). Marks outside the foreground
    window (taskbar buttons carry other windows' titles!) are ignored when the foreground rect is known."""
    out = [(_norm(obs.get("window", "")), "title")]
    rect = _fg_rect()
    ox, oy = obs.get("origin", (0, 0)) or (0, 0)
    for m in obs.get("marks", []) or []:
        name = _norm(m.get("name"))
        if not name:
            continue
        if rect and m.get("box"):
            b = m["box"]
            cx, cy = (b[0] + b[2]) / 2 + ox, (b[1] + b[3]) / 2 + oy
            if not (rect[0] <= cx < rect[2] and rect[1] <= cy < rect[3]):
                continue
        out.append((name, m.get("type", "")))
    return out


def _score(kind, texts):
    """-> {group: (weight_with_bonus, phrase, where, text)} best hit per cue group."""
    hits = {}
    for g, w, phrases in _CUE_RE[kind]:
        best = None
        for text, where in texts:
            for p, rx in phrases:
                if rx.search(text):
                    bonus = 1 if where in ("title", "Edit", "CheckBox") else 0   # not Document: iframes
                    # like the invisible-reCAPTCHA badge are Documents named "reCAPTCHA" on ordinary pages
                    cand = (w + bonus, p, where, text)
                    if best is None or cand[0] > best[0]:
                        best = cand
                    break
        if best:
            hits[g] = best
    return hits


def _clip(s, n=40):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _cue_evidence(hits, window, n=3):
    parts = []
    for _, (_, p, where, text) in sorted(hits.items(), key=lambda kv: -kv[1][0])[:n]:
        parts.append(f"'{p}' in title" if where == "title" else f"'{p}' in {where or 'element'} '{_clip(text, 30)}'")
    return f"{', '.join(parts)}; window '{_clip(window, 50)}'"


def _analyze(obs):
    """All blocker kinds visible in obs -> {kind: evidence}. Cached for the same obs for 1 s."""
    global _cache
    c = _cache
    if c and c[0] is obs and _now() - c[1] < 1.0:      # (holding obs keeps its id from being reused)
        return c[2]
    window = obs.get("window", "") or ""
    texts = _texts(obs)
    found = {}
    sc = {k: _score(k, texts) for k in KINDS}

    # captcha
    h = sc["captcha"]
    if sum(v[0] for v in h.values()) >= 2:
        found["captcha"] = "captcha: " + _cue_evidence(h, window)
    # otp
    h = sc["otp"]
    if sum(v[0] for v in h.values()) >= 2:
        found["otp"] = "one-time code: " + _cue_evidence(h, window)
    # payment (text) — needs >=2 groups incl. a hard one, or a card-number/cvv-like Edit field
    h = sc["payment"]
    card_field = next((v for g, v in h.items() if g in PAYMENT_FIELD and v[2] == "Edit"), None)
    if card_field:
        found["payment"] = f"card field '{_clip(card_field[3], 30)}' in window '{_clip(window, 50)}'"
    elif len(h) >= 2 and PAYMENT_HARD & set(h):
        found["payment"] = "payment: " + _cue_evidence(h, window)
    # login: a real password box (UIA) or >=2 text groups incl. password, or 'sign in' in the title/page
    pw = _password_field()
    save_pw = pw[len(SAVE_PW_TAG):] if pw and pw.startswith(SAVE_PW_TAG) else \
        next((t for t, _ in texts if SAVE_PW_RE.search(_norm(t))), None)
    if save_pw:                                  # the browser's own "Save your password?" bubble, not a login page
        found["consent"] = f"browser password save prompt '{_clip(save_pw, 40)}' (save your password); window '{_clip(window, 50)}'"
        pw = None                                # (its Password/Username fields must not count as a login)
    if pw is not None:
        nm = _norm(pw)
        if any(rx.search(nm) for g, _, ps in _CUE_RE["payment"] if g in PAYMENT_FIELD for _, rx in ps):
            found.setdefault("payment", f"card field '{_clip(pw, 30)}' (masked) in window '{_clip(window, 50)}'")
        else:
            found["login"] = f"password field '{_clip(pw, 30) or '(unnamed)'}' in window '{_clip(window, 50)}'"
    if "login" not in found and not save_pw:
        h = sc["login"]
        strong = ("password" in h and h["password"][2] in ("title", "Edit")) or \
                 ("signin" in h and h["signin"][2] in ("title", "Document"))
        if len(h) >= 2 and strong:
            found["login"] = "sign-in page: " + _cue_evidence(h, window)
    # consent — cue score >= 2, or an Allow + Block/Deny button pair
    h = sc["consent"]
    btns = {_norm(t) for t, where in texts if where in ("Button", "SplitButton", "Hyperlink")}
    if "consent" in found:
        pass
    elif btns & ALLOW_NAMES and btns & DENY_NAMES:
        found["consent"] = f"permission prompt with 'Allow'/'{sorted(btns & DENY_NAMES)[0]}' buttons; window '{_clip(window, 50)}'"
    elif sum(v[0] for v in h.values()) >= 2:
        found["consent"] = "consent banner: " + _cue_evidence(h, window)
    _cache = (obs, _now(), found)
    return found


_cache = None


def detect_blocker(obs):
    """Most specific blocker on screen (captcha > otp > login > payment > consent) or None."""
    if obs.get("background"):                    # a buddy-browser tab seen through CDP: the window is not in front,
        try:                                     # so UIA can't see it -> DOM facts (password/card inputs, captcha...)
            from rbot.browser import browser_exec
            return browser_exec.detect_blocker_tab(obs)
        except ImportError:
            pass
    found = _analyze(obs)
    for k in KINDS:
        if k in found:
            return Blocker(kind=k, evidence=found[k], auto_resume=k in HUMAN_KINDS,
                           window=obs.get("window", "") or "", hwnd=_fg_hwnd())
    return None


def blocker_present(kind, obs):
    if obs.get("background"):
        b = detect_blocker(obs)
        return b is not None and b.kind == kind
    return kind in _analyze(obs)


# ============================================================================ detection: UIA password box
_u32 = ctypes.WinDLL("user32")                  # own function objects: don't change restypes other modules rely on
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]


def _fg_hwnd():
    return int(_u32.GetForegroundWindow() or 0)


def _fg_rect():
    """(l, t, r, b) of the foreground window in physical px, or None."""
    h = _u32.GetForegroundWindow()
    r = wintypes.RECT()
    if not h or not _u32.GetWindowRect(h, ctypes.byref(r)) or r.right <= r.left:
        return None
    return r.left, r.top, r.right, r.bottom


def _window_title(h):
    buf = ctypes.create_unicode_buffer(512)
    _u32.GetWindowTextW(wintypes.HWND(h), buf, 512)
    return buf.value


class _UiaThread:
    """One long-lived thread with COM/UIA initialised; jobs return Futures (so callers can time out)."""

    def __init__(self, name):
        self.name, self.q, self.t = name, queue.Queue(), None
        self.lock = threading.Lock()

    def submit(self, fn, *a):
        with self.lock:
            if self.t is None or not self.t.is_alive():
                self.t = threading.Thread(target=self._run, name=self.name, daemon=True)
                self.t.start()
        f = concurrent.futures.Future()
        self.q.put((f, fn, a))
        return f

    def _run(self):
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            while True:
                f, fn, a = self.q.get()
                if f.set_running_or_notify_cancel():
                    try:
                        f.set_result(fn(*a))
                    except Exception as e:      # noqa: BLE001
                        f.set_exception(e)


_uia = _UiaThread("hitl-uia")
_pw_job = None               # (hwnd, future) in flight
_pw_last = {}                # hwnd -> (t, result)


def _probe_password(hwnd):
    """Name of the first visible IsPassword element in window hwnd ('' if unnamed), or None."""
    import uiautomation as auto
    ia = auto.uiautomation._AutomationClient.instance().IUIAutomation
    cond = ia.CreateAndCondition(ia.CreatePropertyCondition(auto.PropertyId.IsPasswordProperty, True),
                                 ia.CreatePropertyCondition(auto.PropertyId.IsOffscreenProperty, False))
    root = ia.ElementFromHandle(hwnd)
    if not root:
        return None
    arr = root.FindAll(4, cond)                 # 4 = TreeScope_Descendants; native search, ~30-50 ms in Chrome
    if not arr:
        return None
    wr = wintypes.RECT()
    _u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wr))
    for i in range(min(arr.Length, 8)):
        el = arr.GetElement(i)
        r = el.CurrentBoundingRectangle
        w, h = r.right - r.left, r.bottom - r.top
        cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
        if w >= 4 and h >= 4 and wr.left <= cx < wr.right and wr.top <= cy < wr.bottom:
            name = (el.CurrentName or "").strip().replace("\n", " ")
            try:                                 # inside the browser's own "Save your password?" bubble?
                walker, p = ia.ControlViewWalker, el
                for _ in range(6):
                    p = walker.GetParentElement(p)
                    if not p:
                        break
                    if SAVE_PW_RE.search(_norm(p.CurrentName)):
                        return SAVE_PW_TAG + _norm(p.CurrentName)
            except Exception:  # noqa: BLE001
                pass
            return name
    return None


def _password_field(budget_s=0.25):
    """Visible password box in the foreground window? Waits at most budget_s; a slow query keeps running and
    its answer is used on the next call (results < 5 s old for the same window)."""
    global _pw_job
    hwnd = _fg_hwnd()
    if not hwnd:
        return None
    job = _pw_job
    if job is None or job[1].done() or job[0] != hwnd:
        if job is not None and not job[1].done():          # still busy with another window: don't pile up
            fut = None
        else:
            fut = _uia.submit(_probe_password, hwnd)
            _pw_job = (hwnd, fut)
    else:
        fut = job[1]
    if fut is not None:
        try:
            res = fut.result(timeout=budget_s)
            _pw_last[hwnd] = (_now(), res)
            return res
        except concurrent.futures.TimeoutError:
            pass
        except Exception:                                    # noqa: BLE001  (window vanished etc.)
            return None
    last = _pw_last.get(hwnd)
    return last[1] if last and _now() - last[0] < 5.0 else None


# ============================================================================ human session
def _own_overlay_rects():
    """Rects of this process's visible topmost tool windows (the buddy) — masked out of the stability diff."""
    out, pid = [], os.getpid()
    GWL_EXSTYLE, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = -20, 0x8, 0x80

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        p = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and _u32.IsWindowVisible(h):
            ex = _u32.GetWindowLongPtrW(h, GWL_EXSTYLE)
            if ex & WS_EX_TOPMOST and ex & WS_EX_TOOLWINDOW:
                r = wintypes.RECT()
                _u32.GetWindowRect(h, ctypes.byref(r))
                out.append((r.left, r.top, r.right, r.bottom))
        return True

    _u32.EnumWindows(cb, 0)
    return out


def _grab_fg_monitor(sct):
    """Like perception.capture_active_monitor but without the hide-the-buddy hooks (no flicker every second)."""
    from PIL import Image
    r = _fg_rect() or (0, 0, 1, 1)
    cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    mons = sct.monitors[1:]
    mon = next((m for m in mons if m["left"] <= cx < m["left"] + m["width"]
                and m["top"] <= cy < m["top"] + m["height"]), mons[0])
    shot = sct.grab(mon)
    return Image.frombytes("RGB", shot.size, shot.rgb), mon


_MODIFIERS = None


def _is_modifier(key):
    global _MODIFIERS
    if _MODIFIERS is None:
        from pynput.keyboard import Key
        _MODIFIERS = {getattr(Key, n) for n in ("shift", "shift_l", "shift_r", "ctrl", "ctrl_l", "ctrl_r", "alt",
                                                "alt_l", "alt_r", "alt_gr", "cmd", "cmd_l", "cmd_r", "caps_lock")
                      if hasattr(Key, n)}
    return key in _MODIFIERS


def _is_focus_mover(key):
    from pynput.keyboard import Key
    return key in (Key.tab, Key.enter, Key.esc)


_DULL = {"Pane", "Window", "Document", "TitleBar", "Group", "Custom", "Text", "Image"}


def _describe(ctrl, up=3):
    """(name, type) for a control: '(password field)' for password boxes; else the first useful name walking
    up to `up` parents."""
    first = None
    c = ctrl
    for _ in range(up + 1):
        if c is None:
            break
        try:
            if c.IsPassword:
                return "(password field)", "Edit"
            kind = c.ControlTypeName.replace("Control", "")
            name = (c.Name or "").strip().replace("\n", " ")
        except Exception:                                    # noqa: BLE001
            break
        if name and kind not in _DULL:
            return _clip(name, 60), kind
        if first is None:
            first = (_clip(name, 60) if name else f"({kind.lower()})", kind)
        try:
            c = c.GetParentControl()
        except Exception:                                    # noqa: BLE001
            break
    return first or ("(unknown)", "")


class HumanSession:
    """Watches the human while they have control: real clicks (+ the element under them), key COUNTS per
    focused field, 1 fps frames, foreground-title timeline. No LLM calls."""

    MAX_MEM_FRAMES = 30

    def __init__(self, run_dir, reason, fps=1.0, private=None):
        self.run_dir = Path(run_dir)
        self.reason = reason
        self.fps = max(0.1, float(fps))
        self.private = ("payment" in (reason or "").lower()) if private is None else bool(private)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        n = 1 + max([int(m.group(1)) for p in self.run_dir.glob("human_*")
                     if (m := re.fullmatch(r"human_(\d+)", p.name))] or [0])
        self.dir = self.run_dir / f"human_{n}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.started = self.ended = None
        self.last_input = None
        self.active_buckets = set()
        self.clicks = []                 # resolved click dicts
        self.keys_total = 0
        self.fields = {}
        self.windows = []                # [[t, title]]
        self.frames = []                 # paths
        self._mem = {}                   # frame index -> PIL image (first, latest, right-after-click frames)
        self._latest = None
        self._frame_n = 0
        self._click_seq = 0
        self._unframed = []              # click seqs waiting for "the next frame"
        self._seq_frame = {}             # click seq -> index of the first frame captured after it
        self._last_change = None
        self._prev_small = self._prev_mask = None
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._threads = []
        self._mouse = self._kb = None
        self._summary = None
        self._last_key_t = 0.0
        self._focus_dirty = True
        self.errors = []

    # ---------------------------------------------------------------- public
    def start(self):
        with self.lock:
            if self.started is not None:
                return
            self.started = self.last_input = self._last_change = _now()
        from pynput import keyboard, mouse
        self._mouse = mouse.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self._kb = keyboard.Listener(on_press=self._on_press)
        self._mouse.start()
        self._kb.start()
        for fn, nm in ((self._worker, "hitl-resolve"), (self._framer, "hitl-frames")):
            t = threading.Thread(target=fn, name=nm, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        with self.lock:
            if self._summary is not None:
                return self._summary
            if self.started is None:
                self.started = self.last_input = self._last_change = _now()
            self.ended = _now()
        for lst in (self._mouse, self._kb):
            try:
                if lst:
                    lst.stop()
            except Exception:                                # noqa: BLE001
                pass
        self._stop.set()
        self._q.put(None)
        for t in self._threads:
            t.join(timeout=3.0)
        s = self._build_summary()
        try:
            (self.dir / "session.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:                               # noqa: BLE001
            self.errors.append(f"session.json: {e}")
        with self.lock:
            self._summary = s
        return s

    def idle_seconds(self):
        li = self.last_input
        return 0.0 if li is None else max(0.0, _now() - li)

    def stable_seconds(self):
        lc = self._last_change
        return 0.0 if lc is None else max(0.0, _now() - lc)

    def activity_since(self, t):
        li = self.last_input
        return li is not None and li > t and li > (self.started or 0)

    def keyframes(self, n=4):
        """first, last and frames right after important clicks (evenly sampled), as PIL images."""
        if self.private or n <= 0:
            return []
        with self.lock:
            if not self._mem:
                return []
            first = min(self._mem)
            last = max(self._mem)
            important = [c["_seq"] for c in self.clicks if c["type"] not in _DULL | {"Edit", ""}] \
                or [c["_seq"] for c in self.clicks]
            picks = list(dict.fromkeys(self._seq_frame[s] for s in important if s in self._seq_frame))
            picks = [i for i in picks if i in self._mem and i not in (first, last)]
            room = max(0, n - (2 if last != first else 1))
            if len(picks) > room:
                picks = [picks[round(k * (len(picks) - 1) / max(1, room - 1))] for k in range(room)] if room else []
            idx = sorted(dict.fromkeys([first] + picks + ([last] if last != first else [])))
            return [self._mem[i] for i in idx][:n]

    # ---------------------------------------------------------------- listeners (keep cheap!)
    def _touch(self):
        t = _now()
        self.last_input = t
        self.active_buckets.add(int(t))

    def _on_move(self, x, y, injected=False):
        if not injected:
            self._touch()

    def _on_scroll(self, x, y, dx, dy, injected=False):
        if not injected:
            self._touch()

    def _on_click(self, x, y, button, pressed, injected=False):
        if injected:
            return
        self._touch()
        if pressed:
            self._focus_dirty = True
            with self.lock:
                self._click_seq += 1
                seq = self._click_seq
                self._unframed.append(seq)
            self._q.put(("click", _now(), int(x), int(y), getattr(button, "name", str(button)), seq))

    def _on_press(self, key, injected=False):
        if injected:
            return
        self._touch()
        if _is_modifier(key):
            return
        t = _now()
        dirty = self._focus_dirty or t - self._last_key_t > 1.0
        self._focus_dirty = _is_focus_mover(key)        # the NEXT key may land in another field
        self._last_key_t = t
        self._q.put(("key", t, dirty))                  # no key identity ever leaves this function

    # ---------------------------------------------------------------- resolver thread
    def _worker(self):
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            focus = None
            while True:
                try:
                    item = self._q.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                if item is None:
                    if self._q.empty():
                        break
                    continue
                try:
                    if item[0] == "click":
                        _, t, x, y, btn, seq = item
                        try:
                            name, kind = _describe(auto.ControlFromPoint(x, y))
                        except Exception:                    # noqa: BLE001
                            name, kind = "(unknown)", ""
                        try:
                            win = _window_title(perception.gt.root_hwnd_at(x, y))
                        except Exception:                    # noqa: BLE001
                            win = ""
                        c = {"t": round(t - self.started, 2), "x": x, "y": y, "button": btn,
                             "element": name, "type": kind, "window": _clip(win, 80), "_seq": seq}
                        with self.lock:
                            self.clicks.append(c)
                        focus = None
                    else:
                        _, t, dirty = item
                        if dirty or focus is None:
                            try:
                                focus = _describe(auto.GetFocusedControl(), up=2)[0]
                            except Exception:                # noqa: BLE001
                                focus = "(unknown)"
                        with self.lock:
                            self.keys_total += 1
                            self.fields[focus] = self.fields.get(focus, 0) + 1
                except Exception as e:                       # noqa: BLE001
                    self.errors.append(f"resolve: {e}")

    # ---------------------------------------------------------------- frames + title thread
    def _framer(self):
        import mss
        period = 1.0 / self.fps
        next_shot = 0.0
        try:
            sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        except Exception as e:                               # noqa: BLE001
            self.errors.append(f"mss: {e}")
            sct = None
        try:
            while not self._stop.is_set():
                t = time.time()
                try:
                    title = perception.foreground_title()
                    with self.lock:
                        if not self.windows or self.windows[-1][1] != title:
                            self.windows.append([round(_now() - self.started, 2), title])
                except Exception:                            # noqa: BLE001
                    pass
                if sct is not None and t >= next_shot:
                    next_shot = t + period
                    self._shoot(sct)
                self._stop.wait(min(0.25, max(0.02, next_shot - time.time())))
        finally:
            if sct is not None:
                try:
                    sct.close()
                except Exception:                            # noqa: BLE001
                    pass

    def _shoot(self, sct):
        from PIL import ImageDraw
        try:
            img, mon = _grab_fg_monitor(sct)
        except Exception as e:                               # noqa: BLE001  (secure desktop / lock screen)
            if len(self.errors) < 20:
                self.errors.append(f"capture: {e}")
            return
        t = _now()
        if img.width > 960:
            img = img.resize((960, max(1, round(img.height * 960 / img.width))))
        # stability: 320-wide copy with our own buddy window(s) blacked out (it animates while parked)
        sw = 320
        sh = max(1, round(img.height * sw / img.width))
        small = img.convert("L").resize((sw, sh))
        k = sw / mon["width"]
        mask = [((l - mon["left"]) * k, (tp - mon["top"]) * k, (r - mon["left"]) * k, (b - mon["top"]) * k)
                for l, tp, r, b in _own_overlay_rects()]
        changed = True
        if self._prev_small is not None and self._prev_small.size == small.size:
            a, b2 = self._prev_small.copy(), small.copy()
            for im in (a, b2):
                d = ImageDraw.Draw(im)
                for box in mask + (self._prev_mask or []):
                    d.rectangle(box, fill=0)
            changed = perception.diff_ratio(a, b2, width=sw) >= STABLE_DIFF
        self._prev_small, self._prev_mask = small, mask
        with self.lock:
            idx = self._frame_n
            self._frame_n += 1
            if changed and idx > 0:
                self._last_change = t
            if not self.private:
                p = self.dir / f"frame_{idx:03d}.jpg"
                try:
                    img.save(p, "JPEG", quality=70)
                    self.frames.append(str(p))
                except Exception as e:                       # noqa: BLE001
                    self.errors.append(f"save: {e}")
            for seq in self._unframed:                       # first frame after each click
                self._seq_frame[seq] = idx
            after_click = bool(self._unframed)
            self._unframed = []
            self._mem[idx] = img
            prev = self._latest
            self._latest = idx
            click_frames = sorted(set(self._seq_frame.values()))
            if prev is not None and prev != 0 and prev not in click_frames:
                self._mem.pop(prev, None)                    # only first / latest / after-click frames stay in RAM
            if after_click and len(click_frames) > self.MAX_MEM_FRAMES:
                for old in click_frames[:-self.MAX_MEM_FRAMES]:
                    if old not in (0, idx):
                        self._mem.pop(old, None)

    # ---------------------------------------------------------------- summary
    def _build_summary(self):
        with self.lock:
            dur = (self.ended or _now()) - self.started
            clicks = []
            for c in self.clicks:
                d = {k: v for k, v in c.items() if k != "_seq"}
                fi = self._seq_frame.get(c["_seq"])
                d["frame"] = None if fi is None or self.private else f"frame_{fi:03d}.jpg"
                clicks.append(d)
            fields = dict(sorted(self.fields.items(), key=lambda kv: -kv[1]))
            windows = [list(w) for w in self.windows]
            s = {"reason": self.reason,
                 "started": round(self.started, 3), "ended": round(self.ended or _now(), 3),
                 "duration_s": round(dur, 1),
                 "clicks": clicks,
                 "keys": {"total": self.keys_total, "fields": fields},
                 "windows": windows,
                 "frames": list(self.frames),
                 "active_s": len(self.active_buckets),
                 "idle_at_end_s": round(self.idle_seconds(), 1),
                 "private": self.private,
                 "dir": str(self.dir)}
            if self.errors:
                s["errors"] = self.errors[:10]
        s["text"] = _summary_text(s)
        return s


def _summary_text(s):
    parts = [f"{s['duration_s']:.0f} s"]
    clicks = s["clicks"]
    if clicks:
        names = list(dict.fromkeys(c["element"] + (" field" if c["type"] == "Edit" and "field" not in c["element"]
                                                   else (" button" if c["type"] == "Button" and "button" not in
                                                         c["element"].lower() else "")) for c in clicks))
        more = "..." if len(names) > 4 else ""
        parts.append(f"{len(clicks)} click{'s' * (len(clicks) != 1)} ({', '.join(_clip(n, 30) for n in names[:4])}{more})")
    else:
        parts.append("no clicks")
    k = s["keys"]
    if k["total"]:
        parts.append(f"{k['total']} keys into {'/'.join(_clip(f, 30) for f in list(k['fields'])[:4])}")
    else:
        parts.append("no typing")
    titles = [w[1] for w in s["windows"] if w[1]]
    if titles:
        if titles[0] != titles[-1]:
            parts.append(f"window '{_clip(titles[0], 50)}' -> '{_clip(titles[-1], 50)}'")
        else:
            parts.append(f"window '{_clip(titles[0], 50)}'")
    return ", ".join(parts)


# ============================================================================ when is the human done?
def _window_alive(h):
    return bool(h) and bool(_u32.IsWindow(wintypes.HWND(h)))


def wait_for_done(session, blocker, observe, clicked, stop, on_status=None, idle_needed=3.0, stable_needed=2.0,
                  countdown=3.0, timeout_s=300):
    """Block until the human is finished. -> (outcome, how), outcome in {"done", "timeout", "stopped"}.

    - clicked (the parked buddy was clicked)           -> ("done", "clicked buddy") at once
    - auto_resume blockers: blocker gone from screen AND idle >= idle_needed AND screen stable >= stable_needed
      -> visible countdown; any real input cancels it    -> ("done", "blocker gone + idle 3s")
      (while the blocker — or another login/otp/captcha — is still on screen we wait however idle the human is;
      while the human is in a different window than the blocker's we wait too)
    - payment / consent (auto_resume False): only the click, stop or timeout ends it
    - idle for timeout_s -> ("timeout", ...); "Are you still there?" once per idle stretch at timeout_s / 2
    - stop event -> ("stopped", "stopped")
    """
    def status(msg):
        if on_status:
            try:
                on_status(msg)
            except Exception:                                # noqa: BLE001
                pass

    asked = False
    last_obs = -1e9
    cd_t0 = None
    cd_shown = None
    while True:
        if stop.is_set():
            return "stopped", "stopped"
        if clicked.is_set():
            return "done", "clicked buddy"
        now = _now()
        idle = session.idle_seconds()
        if idle >= timeout_s:
            return "timeout", f"timeout {timeout_s:g}s idle"
        if idle >= timeout_s / 2:
            if not asked:
                status("Are you still there?")
                asked = True
        elif asked and cd_t0 is None:
            asked = False                                    # they came back; re-arm for the next idle stretch

        if cd_t0 is not None:                                # ---- countdown running
            if session.activity_since(cd_t0):
                status("OK, still waiting…")
                cd_t0 = cd_shown = None
            else:
                left = countdown - (now - cd_t0)
                if left <= 0:
                    return "done", f"blocker gone + idle {idle_needed:g}s"
                k = math.ceil(left - 1e-9)
                if k != cd_shown:
                    status(f"Looks like you're done — continuing in {k}…")
                    cd_shown = k
        elif (blocker.auto_resume and idle >= idle_needed and session.stable_seconds() >= stable_needed
              and now - last_obs >= 1.0):
            last_obs = now
            hw = getattr(blocker, "hwnd", 0)
            elsewhere = _window_alive(hw) and _fg_hwnd() != hw   # human went to another app (e.g. mail for the code)
            if not elsewhere:
                t_obs = _now()
                try:
                    obs = observe()
                    still = blocker_present(blocker.kind, obs)
                    if not still:
                        other = detect_blocker(obs)
                        still = other is not None and other.kind in HUMAN_KINDS
                except Exception:                            # noqa: BLE001
                    still = True
                if stop.is_set() or clicked.is_set():
                    continue
                if not still and not session.activity_since(t_obs):
                    cd_t0 = _now()
                    cd_shown = None
                    continue                                 # announce the first number right away
        _sleep(0.1)
