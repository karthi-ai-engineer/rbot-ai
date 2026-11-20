"""Cute agent cursor overlay (prototype).

  python -m rbot.desktop.cursor_overlay                    live demo on your screen
  python -m rbot.desktop.cursor_overlay --theme mint       other colours: grape | mint | sunset
  python -m rbot.desktop.cursor_overlay --preview out.png  render a sheet of states to an image

Human-in-the-loop extras (motion approved 2026-11-18):
  cur.post("mood", "calling" | "stuck" | "thanks" | "normal" [, bubble_text])
  cur.post("park" [, bubble_text])   pointer turns into a little drop and walks to the bottom-left
                                     corner of the screen your mouse is on; it becomes clickable
  cur.post("unpark")                 morph back and fly to where it was working
  cur.on_parked_click = fn           called (Qt thread) when the user clicks the parked drop;
                                     cur.parked_clicked is the same thing as a Qt signal
"""
import argparse
import math
import os
import queue
import random
import signal
import sys

# Mixed-DPI monitors (125% + 100%) make Qt rescale/misplace the overlay when it crosses screens.
# Work in plain physical pixels everywhere instead (must be set before QApplication is created).
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QTransform)
from PySide6.QtWidgets import QApplication, QWidget

THEMES = {
    "grape": ("#8B5CF6", "#EC4899"),
    "mint": ("#10B981", "#3B82F6"),
    "sunset": ("#F59E0B", "#EF4444"),
}
WIN_W, WIN_H = 600, 180                     # roomy enough for hops, spins, legs and the bubble
TIP = QPointF(64, 84)                       # pointer tip position inside the overlay window
CENTER, RADIUS = QPointF(24, 24), 17.0      # the round "head" of the pointer, relative to the tip
DROP_SCALE = 1.12                           # the standing drop is a touch bigger than the pointer
GROUND = 33.5                               # drop's feet, below the head centre (fully grown legs)
INK = QColor("#2A1240")
WHITE = QColor(255, 255, 255)
DEFAULT_SAY = {"calling": "Your turn!", "stuck": "I'm stuck \U0001F61F", "thanks": "Thanks! Continuing…"}
PARK_SAY = "Your turn — click me when done"


def P(x, y):
    return QPointF(x, y)


# ---- easing helpers ---------------------------------------------------------------
def clamp(v, a, b):
    return max(a, min(b, v))


def lerp(a, b, t):
    return a + (b - a) * t


def span(t, a, b):
    return clamp((t - a) / (b - a), 0.0, 1.0)


def ease_out(t):
    return 1 - (1 - clamp(t, 0, 1)) ** 3


def ease_in(t):
    return clamp(t, 0, 1) ** 3


def back_out(t, s=1.7):                      # overshoots a little, then settles
    t = clamp(t, 0, 1) - 1
    return 1 + t * t * ((s + 1) * t + s)


def wobble(t, amp, freq=2.2, decay=7.0):     # damped cosine: follow-through after an impact
    return amp * math.exp(-decay * t) * math.cos(2 * math.pi * freq * t) if t >= 0 else 0.0


def hop(t, h, crouch=0.12, air=0.36, land=0.30):
    """One hop at local time t -> (y, sx, sy): anticipation crouch, stretched flight, squash on landing."""
    if t < 0:
        return 0.0, 1.0, 1.0
    if t < crouch:
        a = ease_out(t / crouch)
        return 0.0, 1 + 0.12 * a, 1 - 0.18 * a
    t -= crouch
    if t < air:
        u = t / air
        sy = lerp(0.82, 1 + 0.12 * abs(1 - 2 * u), min(1.0, t / 0.05))   # snap out of the crouch in 50 ms
        return -h * 4 * u * (1 - u), 1 - (sy - 1) * 0.7, sy
    d = wobble(t - air, 0.22, 3.2, 12) if t - air < land else 0.0       # squash, tiny rebound, settle
    return 0.0, 1 + 0.8 * d, 1 - d


def hops(t, starts, heights):
    """The most recent of several hops."""
    for s, h in reversed(list(zip(starts, heights))):
        if t >= s:
            return hop(t - s, h)
    return 0.0, 1.0, 1.0


def body_path():
    """Teardrop pointer: sharp tip at (0,0) flowing into a round head."""
    d = math.hypot(CENTER.x(), CENTER.y())
    base = math.atan2(-CENTER.y(), -CENTER.x())
    off = math.acos(RADIUS / d)
    pts = [P(0, 0)] + [P(CENTER.x() + RADIUS * math.cos(a), CENTER.y() + RADIUS * math.sin(a))
                       for a in (base - off, base + off)]
    tri = QPainterPath()
    tri.addPolygon(QPolygonF(pts))
    tri.closeSubpath()
    head = QPainterPath()
    head.addEllipse(CENTER, RADIUS, RADIUS)
    return tri.united(head).simplified()


class Pose:
    """Everything the painter needs for one frame (all in the head-centre frame)."""
    def __init__(self):
        self.off = P(0, 0)        # hop / shake / walk bob
        self.sx = self.sy = 1.0   # squash & stretch about the feet (or the bottom of the head)
        self.spin = 0.0           # whole-character spin (thanks)
        self.m = 0.0              # 0 = pointer, 1 = standing drop (body rotated 45 deg so the tip points up)
        self.legs = self.arms = 0.0
        self.face = "normal"
        self.lift = [0.0, 0.0]    # foot lift (left, right)
        self.swing = [0.0, 0.0]   # foot x swing
        self.hand = [P(0, 0), P(0, 0)]
        self.wave = -1.0          # >= 0 while waving (time)
        self.grow = 1.0           # hover grow


class AgentCursor(QWidget):
    parked_clicked = Signal()

    def __init__(self, theme="grape"):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(WIN_W, WIN_H)
        self.c1, self.c2 = (QColor(c) for c in THEMES[theme])
        self.limb_col = QColor(self.c1).darker(122)
        self.foot_col = QColor(self.c1).darker(190)
        self.path = body_path()
        self.path_c = self.path.translated(-CENTER)      # same body, head centre at (0,0)
        self.font_ = QFont("Segoe UI")
        self.font_.setPixelSize(13)
        self.font_.setWeight(QFont.DemiBold)
        self.small_font = QFont("Segoe UI")
        self.small_font.setPixelSize(10)
        self.small_font.setWeight(QFont.Bold)

        center = QApplication.primaryScreen().geometry().center()
        self.pos_ = P(center.x(), center.y())
        self.target = P(self.pos_.x(), self.pos_.y())
        self.vel = P(0, 0)
        self.frac = P(0, 0)
        self.tilt = self.squish = self.pop = self.bubble = 0.0
        self.look = P(0, 0)
        self.t = self.last = 0.0
        self.blink_t, self.blink_next = -1.0, 2.0
        self.ripples = []
        self.text, self.thinking = "", False
        self.script, self.wait_until, self.wait_arrive = None, 0.0, False
        self.cmds = queue.Queue()                  # thread-safe commands from the agent thread
        self.dpr = QApplication.primaryScreen().devicePixelRatio()

        self.mood_, self.mood_t0 = "normal", 0.0
        self.mode, self.mode_t0 = "pointer", 0.0   # pointer | morph_in | walk | parked | morph_out
        self.home = P(0, 0)                        # where to fly back to after unpark
        self.walk = None                           # (start, end, duration, t0)
        self.walk_phase = 0.0
        self.hover = self.hover_s = 0.0           # real mouse over the parked drop (raw, smoothed)
        self.park_text = PARK_SAY
        self.clickable = False
        self.on_parked_click = None
        self.reduced = False

        if sys.platform == "win32":                # visible to the user, invisible to screenshots (agent's eyes)
            import ctypes
            ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), 0x11)  # WDA_EXCLUDEFROMCAPTURE
            anim = ctypes.c_int(1)                 # Settings > Accessibility > "Animation effects"
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(anim), 0)
            self.reduced = not anim.value

        self.clock = QElapsedTimer()
        self.clock.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(15)
        self._place()

    # ---- public API -------------------------------------------------------
    # From another thread, never call these directly; use post("move_to_px", x, y), post("say", "hi"), ...
    def post(self, name, *args):
        self.cmds.put((name, args))

    def move_to_px(self, x, y):
        """Target in physical screen pixels (what screenshots / UI Automation / input use).
        Qt keeps each screen's origin in native px but scales its size, so convert per screen."""
        for s in QApplication.screens():
            g, d = s.geometry(), s.devicePixelRatio()
            if g.x() <= x < g.x() + g.width() * d and g.y() <= y < g.y() + g.height() * d:
                return self.move_to(g.x() + (x - g.x()) / d, g.y() + (y - g.y()) / d)
        self.move_to(x / self.dpr, y / self.dpr)

    def move_to(self, x, y):
        if self.mode == "pointer":
            self.target = P(x, y)
        else:                                      # parked: remember it for when we fly back
            self.home = P(x, y)

    def click(self):
        self.ripples.append(self.t)
        self.squish = 1.0

    def say(self, text, thinking=False):
        if text != self.text:
            self.pop = 1.0
        self.text, self.thinking = text, thinking

    def quiet(self):
        self.text, self.thinking = "", False

    @property
    def arrived(self):
        d = self.target - self.pos_
        return math.hypot(d.x(), d.y()) < 1.5 and math.hypot(self.vel.x(), self.vel.y()) < 25

    @property
    def parked(self):
        return self.mode == "parked"

    def run_script(self, gen):
        """gen yields seconds to wait, or "arrive" to wait until the cursor reaches its target."""
        self.script = gen

    def mood(self, name, text=None):
        """normal | calling (hops for attention) | stuck (hop + worried shake) | thanks (happy spin, then normal)."""
        self.mood_, self.mood_t0 = name, self.t
        if name != "normal":
            self.say(text or DEFAULT_SAY[name])

    def park(self, text=None):
        """Turn into the drop, walk to the bottom-left corner of the screen under the user's mouse, wait for a click."""
        if self.mode != "pointer":
            return
        self.park_text = text or PARK_SAY
        self.mood_ = "normal"
        self.home = P(self.target.x(), self.target.y())
        self.quiet()
        self._set_mode("morph_in")

    def unpark(self):
        if self.mode in ("pointer", "morph_out"):
            return
        self._set_mode("morph_out", skip=0.12)    # no press squish when the agent unparks by itself
        self.quiet()

    # ---- state ------------------------------------------------------------------
    def _set_mode(self, mode, skip=0.0):
        self.mode, self.mode_t0 = mode, self.t - skip
        self._set_clickable(mode == "parked")

    def _set_clickable(self, on):
        """Click-through everywhere except while parked. Flip WS_EX_TRANSPARENT directly so Qt
        doesn't re-create (and flash) the window the way setWindowFlags would."""
        if on == self.clickable:
            return
        self.clickable = on
        if sys.platform == "win32":
            import ctypes
            u, hwnd = ctypes.windll.user32, int(self.winId())
            ex = u.GetWindowLongW(hwnd, -20)                          # GWL_EXSTYLE
            u.SetWindowLongW(hwnd, -20, (ex & ~0x20) if on else (ex | 0x20))   # WS_EX_TRANSPARENT

    def _pressed(self):
        if self.mode != "parked":
            return
        self.mode, self.mode_t0 = "morph_out", self.t   # stays clickable until the button comes back up,
        self.quiet()                                    # so the window below never gets a lone mouse-up
        self.parked_clicked.emit()
        if self.on_parked_click:
            self.on_parked_click()

    def nativeEvent(self, etype, message):
        if self.clickable and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0020:                                 # WM_SETCURSOR -> hand while parked
                ctypes.windll.user32.SetCursor(ctypes.windll.user32.LoadCursorW(None, 32649))
                return True, 1
            if msg.message == 0x0201:                                 # WM_LBUTTONDOWN
                self._pressed()
                return True, 0
            if msg.message == 0x0202:                                 # WM_LBUTTONUP
                if self.mode != "parked":
                    self._set_clickable(False)
                return True, 0
        return super().nativeEvent(etype, message)

    def mousePressEvent(self, _):                  # fallback if the native hook didn't see it
        self._pressed()

    # ---- animation ----------------------------------------------------------
    def _tick(self):
        while not self.cmds.empty():
            name, args = self.cmds.get_nowait()
            getattr(self, name)(*args)
        now = self.clock.elapsed() / 1000
        dt = min(now - self.last, 0.04)
        self.last = self.t = now
        mt = now - self.mode_t0

        if self.mode == "morph_in" and mt > 0.9:
            self._start_walk(now)
        elif self.mode == "morph_out" and mt > 0.75:
            self._set_mode("pointer")
        if self.mood_ == "thanks" and now - self.mood_t0 > 2.4:
            self.mood_ = "normal"

        if self.mode == "walk":
            self._walk_step(now, dt)
        elif self.mode in ("morph_in", "parked"):
            self.vel, self.target = P(0, 0), P(self.pos_.x(), self.pos_.y())
        else:
            if self.mode == "morph_out":           # hold still for the press + stretch, then fly home
                self.target = P(self.pos_.x(), self.pos_.y()) if mt < 0.3 else self.home
            k = 140.0                              # springy glide with a tiny overshoot
            c = 2 * math.sqrt(k) * 0.72
            self.vel += ((self.target - self.pos_) * k - self.vel * c) * dt
            sp = math.hypot(self.vel.x(), self.vel.y())
            if sp > 4000:
                self.vel *= 4000 / sp
            self.pos_ += self.vel * dt
        sp = math.hypot(self.vel.x(), self.vel.y())

        tilt_t = max(-16.0, min(16.0, self.vel.x() * 0.01))
        self.tilt += (tilt_t - self.tilt) * min(1, dt * 12)
        look_t = P(0, 0)
        if self.mode == "parked":
            look_t = self._parked_look(now)
        elif self.thinking and sp < 60:
            look_t = P(1.6, -1.8)                  # eyes up-right while thinking
        elif sp > 1:
            look_t = self.vel * (min(sp / 500, 1) * 2.2 / sp)
        elif self.mood_ == "calling":
            look_t = P(0, -0.6)                    # looking at you
        elif self.mood_ == "stuck":
            look_t = P(0, 1.0)                     # looking down, a bit sheepish
        self.look += (look_t - self.look) * min(1, dt * 10)
        if self.mode != "parked":
            self.hover = 0.0
        self.hover_s += (self.hover - self.hover_s) * min(1, dt * 14)

        self.squish *= math.exp(-dt * 9)
        self.pop *= math.exp(-dt * 8)
        self.bubble += ((1.0 if self.text else 0.0) - self.bubble) * min(1, dt * 12)
        self.ripples = [r for r in self.ripples if now - r < 0.7]
        if now > self.blink_next:
            self.blink_t, self.blink_next = now, now + random.uniform(2.2, 5.0)

        self._run_script(now)
        self._place()
        self.update()

    def _start_walk(self, now):
        mouse = QCursor.pos()                       # read only - the real mouse is never moved
        scr = QGuiApplication.screenAt(mouse) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()                 # stand on top of the taskbar
        head = P(g.left() + 58, g.bottom() - GROUND - 3)
        end = head - CENTER                         # pos_ is always "pointer tip" = head centre - CENTER
        d = end - self.pos_
        dur = clamp(math.hypot(d.x(), d.y()) / 700, 0.8, 2.6)
        self.walk = (P(self.pos_.x(), self.pos_.y()), end, dur, now)
        self._set_mode("walk")

    def _walk_step(self, now, dt):
        start, end, dur, t0 = self.walk
        u = span(now, t0, t0 + dur)
        e = 0.5 - 0.5 * math.cos(math.pi * u)        # gentle start and stop
        new = start + (end - start) * e
        self.vel = (new - self.pos_) * (1 / max(dt, 1e-3))
        self.pos_, self.target = new, P(new.x(), new.y())
        speed = math.hypot(self.vel.x(), self.vel.y())
        self.walk_phase += dt * 2 * math.pi * clamp(speed / 52, 1.8, 3.6)   # ~26 px per step, capped
        if u >= 1:
            self.vel = P(0, 0)
            self._set_mode("parked")
            self.say(self.park_text)

    def _parked_look(self, now):
        m = QCursor.pos()
        head = self.pos_ + CENTER
        d = P(m.x(), m.y()) - head
        dist = math.hypot(d.x(), d.y())
        self.hover = 1.0 if dist < 26 else 0.0
        if 0 < dist < 300:                          # your mouse is close: watch it
            return d * (2.2 / dist)
        c = (now - self.mode_t0) % 4.6              # otherwise look around now and then
        return P(-2.6, 0.4) if 1.4 < c < 2.2 else P(2.6, 0.4) if 2.7 < c < 3.5 else P(0, 0)

    def _run_script(self, now):
        if not self.script or now < self.wait_until or (self.wait_arrive and not self.arrived):
            return
        self.wait_arrive = False
        try:
            step = next(self.script)
        except StopIteration:
            self.script = None
            return
        if step == "arrive":
            self.wait_arrive = True
        else:
            self.wait_until = now + float(step)

    def _place(self):
        x, y = self.pos_.x() - TIP.x(), self.pos_.y() - TIP.y()
        self.frac = P(x - math.floor(x), y - math.floor(y))
        self.move(math.floor(x), math.floor(y))

    # ---- poses -----------------------------------------------------------------
    def _pose(self, now):
        o = Pose()
        amp = 0.35 if self.reduced else 1.0         # Windows "animation effects" off -> gentler
        mt = now - self.mode_t0
        if self.mode == "pointer":
            self._mood_pose(o, now - self.mood_t0, amp)
        elif self.mode == "morph_in":               # crouch -> pop up & rotate upright -> limbs sprout -> settle
            a = ease_out(mt / 0.15)
            o.m = back_out(span(mt, 0.15, 0.55), 1.4)
            o.legs = back_out(span(mt, 0.30, 0.62), 2.2)
            o.arms = back_out(span(mt, 0.38, 0.70), 2.2)
            o.off = P(0, -12 * amp * math.sin(math.pi * span(mt, 0.15, 0.5)))
            if mt < 0.15:
                o.sx, o.sy = 1 + 0.12 * a, 1 - 0.15 * a
            else:
                st = wobble(mt - 0.15, 0.16, 2.0, 6)
                o.sy = lerp(0.85, 1 + st, min(1.0, (mt - 0.15) / 0.05))
                o.sx = 1 - (o.sy - 1) * 0.7
        elif self.mode in ("walk", "parked"):
            o.m = o.legs = o.arms = 1.0
            if self.mode == "walk":
                ph = self.walk_phase
                o.off = P(0, -abs(math.sin(ph)) * 3.2 * amp)
                o.sy = 1 + 0.04 * (abs(math.sin(ph)) * 2 - 1)        # squash on each footfall
                o.sx = 1 - (o.sy - 1) * 0.7
                for i in (0, 1):
                    s = math.sin(ph + i * math.pi)
                    o.lift[i] = max(0.0, s) * 5
                    o.swing[i] = math.cos(ph + i * math.pi) * 2.5
                    o.hand[i] = P(math.cos(ph + (1 - i) * math.pi) * 1.5, -math.sin(ph + (1 - i) * math.pi) * 3)
            else:
                d = wobble(mt, 0.12, 2.5, 7)                           # plant on arrival
                b = 0.015 * math.sin(now * 2.4)                        # breathing
                o.sy, o.sx = 1 - d + b, 1 + 0.8 * d - b * 0.7
                if 0.25 < mt < 1.35:
                    o.wave = mt - 0.25                                 # hello wave on arrival
                c = (mt - 2.0) % 3.4                                   # tap-tap of a foot
                if mt > 2.0 and c < 0.36:
                    o.lift[1] = math.sin(math.pi * (c % 0.18) / 0.18) * 3.5
                o.grow = 1 + 0.06 * self.hover_s
                if self.hover_s > 0.5:
                    o.face = "excited"
        elif self.mode == "morph_out":              # press squish -> stretch -> limbs tuck -> rotate back
            o.m = 1 - back_out(span(mt, 0.22, 0.52), 1.2)
            o.legs = 1 - ease_in(span(mt, 0.12, 0.30))
            o.arms = 1 - ease_in(span(mt, 0.10, 0.26))
            if mt < 0.12:
                a = ease_out(mt / 0.12)
                o.sx, o.sy = 1 + 0.15 * a, 1 - 0.16 * a
            else:
                st = wobble(mt - 0.12, 0.14, 1.8, 6)
                o.sy = lerp(0.84, 1 + st, min(1.0, (mt - 0.12) / 0.05))
                o.sx = 1 - (o.sy - 1) * 0.7
            if mt < 0.35:
                o.face = "happy"
        return o

    def _mood_pose(self, o, t, amp):
        if self.mood_ == "calling":                 # 3 hops, short rest, loop
            o.face = "excited"
            y, o.sx, o.sy = hops(t % 3.0, (0, 0.68, 1.36), (28 * amp, 30 * amp, 34 * amp))
            o.off = P(0, y)
        elif self.mood_ == "stuck":                 # little hop, worried head-shake, rest, loop
            o.face = "worried"
            c = t % 2.8
            y, o.sx, o.sy = hop(c, 18 * amp)
            u = span(c, 0.62, 1.32)
            x = 7 * amp * math.sin(2 * math.pi * 4.5 * (c - 0.62)) * (1 - u) ** 1.5 if 0 < u < 1 else 0.0
            o.off, o.spin = P(x, y), x * 0.9
        elif self.mood_ == "thanks":                # spin-hop, then a tiny happy bounce
            o.face = "happy"
            if t < 0.95:
                y, o.sx, o.sy = hop(t, 26 * amp, crouch=0.10, air=0.50, land=0.35)
                if not self.reduced:
                    o.spin = 360 * (0.5 - 0.5 * math.cos(math.pi * span(t, 0.10, 0.60)))
            else:
                y, o.sx, o.sy = hop(t - 0.95, 9 * amp, crouch=0.08, air=0.22, land=0.25)
            o.off = P(0, y)

    # ---- drawing --------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        self.paint_to(p, TIP + self.frac)
        p.end()

    def paint_to(self, p, origin):
        now = self.t
        o = self._pose(now)
        m = o.m
        p.setRenderHint(QPainter.Antialiasing)
        p.save()
        p.translate(origin)
        self._ripples(p, now)
        p.translate(CENTER)                                            # head-centre frame from here on

        sp = math.hypot(self.vel.x(), self.vel.y())
        bob = math.sin(now * 2.4) * 1.4 * max(0.0, 1 - sp / 80) * (1 - clamp(m, 0, 1))
        if o.legs > 0.05:                                              # soft ground shadow under the feet
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, int(38 * clamp(o.legs, 0, 1))))
            p.drawEllipse(P(o.off.x(), GROUND + 1), 12 + o.off.y() * 0.25, 2.8)

        p.save()
        p.translate(o.off + P(0, bob))
        lean_pivot = P(-24, -24) * (1 - m) + P(0, GROUND) * m           # tip for the pointer, feet for the drop
        p.translate(lean_pivot)
        p.rotate(self.tilt)
        p.translate(-lean_pivot)
        sq = P(0, RADIUS) * (1 - m) + P(0, GROUND) * m                  # squash against the floor
        p.translate(sq)
        p.scale(o.sx * o.grow, o.sy * o.grow)
        p.translate(-sq)
        k = self.squish * (1 - clamp(m, 0, 1))                          # click squish, pressed into the tip
        p.translate(-CENTER)
        p.scale(1 + 0.10 * k, 1 - 0.16 * k)
        p.translate(CENTER)

        bs = 1 + (DROP_SCALE - 1) * clamp(m, 0, 1.2)
        p.save()
        p.rotate(o.spin)                                                # limbs + face spin with the body,
        self._limbs(p, o, now, legs=True)                               # the drop shadow stays put
        self._limbs(p, o, now, legs=False)
        p.restore()
        tr = QTransform()
        tr.rotate(45 * m + o.spin)                                      # tip up-left -> tip straight up
        tr.scale(bs, bs)
        body = tr.map(self.path_c)
        p.setPen(Qt.NoPen)
        for dx, dy, a in ((1.5, 3, 40), (3, 5.5, 22)):              # soft shadow
            p.fillPath(body.translated(dx, dy), QColor(0, 0, 0, a))
        g = QLinearGradient(tr.map(P(-24, -24)), tr.map(P(18, 18)))
        g.setColorAt(0, self.c1)
        g.setColorAt(1, self.c2)
        p.fillPath(body, g)
        p.strokePath(body, QPen(QColor(255, 255, 255, 245), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(255, 255, 255, 75))                        # glossy highlight
        p.drawEllipse(P(-6, -9) * bs, 6 * bs, 3.2 * bs)
        p.rotate(o.spin)
        p.scale(bs, bs)
        self._face(p, now, o.face)
        p.restore()

        if self.bubble > 0.02:
            self._bubble(p, now, m)
        p.restore()

    def _limbs(self, p, o, now, legs):
        grow = clamp(o.legs if legs else o.arms, 0, 1.3)
        if grow < 0.04:
            return
        for i, sgn in ((0, -1), (1, 1)):
            if legs:
                root = P(6.0 * sgn, 13)
                end = P(8.0 * sgn + o.swing[i], 13 + 18 * grow - o.lift[i])
            else:
                root = P(15.5 * sgn, 5)
                rest = P(27.0 * sgn, 14) + o.hand[i]
                if sgn > 0 and o.wave >= 0:                              # waving: arm up, hand wiggles
                    up = math.sin(math.pi * clamp(o.wave / 1.1, 0, 1)) ** 0.5
                    ang = math.radians(lerp(38, -42 + 24 * math.sin(o.wave * 2 * math.pi * 3), up))
                    rest = root + P(math.cos(ang), math.sin(ang)) * 15
                end = root + (rest - root) * grow
            path = QPainterPath(root)
            path.lineTo(end)
            w = 4.4 if legs else 4.0
            p.strokePath(path, QPen(QColor(255, 255, 255, 240), w + 3, Qt.SolidLine, Qt.RoundCap))
            p.strokePath(path, QPen(self.limb_col, w, Qt.SolidLine, Qt.RoundCap))
            p.setPen(QPen(QColor(255, 255, 255, 240), 1.5))
            if legs:                                                     # little shoes, toes out
                p.setBrush(self.foot_col)
                p.drawEllipse(end + P(1.8 * sgn, 0.6), 4.6 * grow, 2.8 * grow)
            else:
                p.setBrush(self.limb_col)
                p.drawEllipse(end, 3.1 * grow, 3.1 * grow)

    def _ripples(self, p, now):
        p.setBrush(Qt.NoBrush)
        for r in self.ripples:
            a = (now - r) / 0.7
            e = 1 - (1 - a) ** 3
            col = QColor(self.c1)
            col.setAlpha(int(70 * (1 - a)))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(P(0, 0), 12 * e, 12 * e)
            col.setAlpha(int(220 * (1 - a)))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(col, 1 + 2.5 * (1 - a)))
            p.drawEllipse(P(0, 0), 4 + 32 * e, 4 + 32 * e)

    def _face(self, p, now, face):
        c = P(0, 1)
        opn = 1.0
        if self.blink_t >= 0 and now - self.blink_t < 0.16:
            opn = max(0.08, abs(math.cos(math.pi * (now - self.blink_t) / 0.16)))
        happy = face == "happy" or self.squish > 0.35
        ew, eh = {"excited": (2.35, 3.2), "worried": (1.9, 2.6)}.get(face, (2.1, 2.9))
        for sx in (-5.5, 5.5):
            ec = c + P(sx, -1.5) + self.look
            if happy:                                                # ^ ^ eyes
                arc = QPainterPath(ec + P(-2.4, 1.2))
                arc.quadTo(ec + P(0, -2.6), ec + P(2.4, 1.2))
                p.strokePath(arc, QPen(INK, 1.8, Qt.SolidLine, Qt.RoundCap))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(INK)
                p.drawEllipse(ec, ew, eh * opn)
                if opn > 0.5:
                    p.setBrush(WHITE)
                    p.drawEllipse(ec + P(-0.7, -1.1), 0.85, 0.85)
            if face == "worried":                                    # brows: inner ends up
                s = 1 if sx > 0 else -1
                b = QPainterPath(c + P(sx + 2.6 * s, -6.0))
                b.lineTo(c + P(sx - 2.0 * s, -7.9))
                p.strokePath(b, QPen(INK, 1.4, Qt.SolidLine, Qt.RoundCap))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 140, 190, 150 if face in ("happy", "excited") else 130))   # blush
        for sx in (-9.8, 9.8):
            p.drawEllipse(c + P(sx, 3.4), 2.8, 1.7)
        mc = c + P(0, 3.6) + self.look * 0.4
        if face in ("happy", "excited"):                                 # open smile
            m = QPainterPath(mc + P(-2.8, -0.2))
            m.lineTo(mc + P(2.8, -0.2))
            m.quadTo(mc + P(0, 4.8), mc + P(-2.8, -0.2))
            p.fillPath(m, INK)
            p.setBrush(QColor(255, 120, 150))
            p.drawEllipse(mc + P(0, 2.1), 1.4, 0.8)
        elif face == "worried":                                          # wobbly little frown
            m = QPainterPath(mc + P(-2.6, 1.4))
            m.quadTo(mc + P(-1.3, -0.2), mc + P(0, 0.8))
            m.quadTo(mc + P(1.3, 1.8), mc + P(2.6, 0.4))
            p.strokePath(m, QPen(INK, 1.5, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(QColor(125, 200, 255, 230))                       # sweat drop
            p.setPen(QPen(WHITE, 1.2))
            d = QPainterPath(P(18.5, -19))                               # sits just outside the head
            d.cubicTo(P(22, -13.5), P(22, -10), P(18.5, -10))
            d.cubicTo(P(15, -10), P(15, -13.5), P(18.5, -19))
            p.drawPath(d)
            p.setPen(Qt.NoPen)
        elif self.thinking:
            p.setBrush(INK)
            p.drawEllipse(mc + P(0, 0.6), 1.3, 1.5)
        else:
            m = QPainterPath(mc + P(-2.6, 0))
            m.quadTo(mc + P(0, 2.9), mc + P(2.6, 0))
            p.strokePath(m, QPen(INK, 1.6, Qt.SolidLine, Qt.RoundCap))

    def _bubble(self, p, now, m):
        fm = QFontMetricsF(self.font_)
        tw = fm.horizontalAdvance(self.text)
        dots_w = 24 if self.thinking else 0
        h = 30
        w = tw + 28 + dots_w
        b = self.bubble
        sc = (0.6 + 0.4 * b) * (1 + 0.08 * self.pop)
        mm = clamp(m, 0, 1)
        p.save()
        p.translate(P(24, 21) * (1 - mm) + P(32, -16) * mm)        # beside the pointer / beside the drop
        p.scale(sc, sc)
        p.setOpacity(b)
        rect = QRectF(0, -h / 2, w, h)
        rr = QPainterPath()
        rr.addRoundedRect(rect, h / 2, h / 2)
        p.fillPath(rr.translated(0, 3), QColor(0, 0, 0, 40))
        g = QLinearGradient(0, 0, w, 0)
        g.setColorAt(0, self.c1)
        g.setColorAt(1, self.c2)
        p.fillPath(rr, g)
        p.strokePath(rr, QPen(QColor(255, 255, 255, 210), 1.5))
        p.setPen(WHITE)
        p.setFont(self.font_)
        p.drawText(rect.adjusted(14, 0, -14 - dots_w, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text)
        if self.thinking:                                            # bouncing typing dots
            p.setPen(Qt.NoPen)
            p.setBrush(WHITE)
            for i in range(3):
                y = -2.4 * max(0.0, math.sin(now * 7 - i * 0.8))
                p.drawEllipse(P(14 + tw + 7 + i * 6.5, y + 1), 2, 2)
        if self.mode == "parked":                                    # "● REC" tag above the bubble
            pill = QRectF(2, -h / 2 - 23, 46, 18)
            rp = QPainterPath()
            rp.addRoundedRect(pill, 9, 9)
            p.fillPath(rp, QColor(30, 16, 44, 225))
            p.strokePath(rp, QPen(QColor(255, 255, 255, 200), 1.2))
            pulse = 0.5 + 0.5 * math.cos(2 * math.pi * (now - self.mode_t0) / 1.4)
            red = QColor(255, 59, 72)
            red.setAlphaF(0.45 + 0.55 * pulse)
            p.setPen(Qt.NoPen)
            p.setBrush(red)
            p.drawEllipse(pill.topLeft() + P(11, 9), 3.2 + 0.5 * pulse, 3.2 + 0.5 * pulse)
            p.setPen(WHITE)
            p.setFont(self.small_font)
            p.drawText(pill.adjusted(19, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "REC")
        p.restore()


# ---- demo / preview -----------------------------------------------------------
def demo(c, loops):
    g = QApplication.primaryScreen().availableGeometry()

    def at(fx, fy):
        return g.x() + g.width() * fx, g.y() + g.height() * fy

    for _ in range(loops):
        c.say("Hi! I'm your agent")
        yield 1.8
        c.say("Looking around", thinking=True)
        c.move_to(*at(0.22, 0.28))
        yield "arrive"
        yield 0.9
        c.say("Found the search box!")
        yield 0.6
        c.click()
        yield 1.0
        c.say("Typing", thinking=True)
        yield 1.8
        c.say("Opening the menu")
        c.move_to(*at(0.72, 0.35))
        yield "arrive"
        yield 0.3
        c.click()
        yield 1.0
        c.say("Double-click here")
        c.move_to(*at(0.55, 0.72))
        yield "arrive"
        yield 0.3
        c.click()
        yield 0.15
        c.click()
        yield 1.2
        c.say("All done!")
        c.move_to(*at(0.5, 0.5))
        yield "arrive"
        yield 2.0
    c.quiet()
    yield 0.6
    QApplication.quit()


def preview(out, theme):
    c = AgentCursor(theme)
    c.timer.stop()
    states = [
        ("idle", lambda: c.say("Hi! I'm your agent")),
        ("moving", lambda: (setattr(c, "tilt", 12), setattr(c, "look", P(2, 0.6)))),
        ("click", lambda: (c.ripples.append(c.t - 0.16), setattr(c, "squish", 0.7), c.say("Clicked!"))),
        ("thinking", lambda: (c.say("Thinking", thinking=True), setattr(c, "look", P(1.6, -1.8)))),
    ]
    cw, ch, s = 250, 110, 2
    img = QImage(cw * len(states) * s, ch * 2 * s, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.scale(s, s)
    for row, bg in enumerate(("#F4F4F6", "#1C1C21")):
        for col, (_, setup) in enumerate(states):
            c.t, c.tilt, c.squish, c.look, c.ripples, c.bubble = 1.0, 0.0, 0.0, P(0, 0), [], 1.0
            c.quiet()
            setup()
            if not c.text:
                c.bubble = 0.0
            p.fillRect(QRectF(col * cw, row * ch, cw, ch), QColor(bg))
            c.paint_to(p, P(col * cw + 40, row * ch + 30))
    p.end()
    img.save(out)
    print("saved", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="grape", choices=THEMES)
    ap.add_argument("--preview")
    ap.add_argument("--loops", type=int, default=2)
    a = ap.parse_args()
    signal.signal(signal.SIGINT, signal.SIG_DFL)                     # Ctrl+C quits
    app = QApplication(sys.argv)
    if a.preview:
        preview(a.preview, a.theme)
        sys.exit(0)
    cur = AgentCursor(a.theme)
    cur.show()
    cur.run_script(demo(cur, a.loops))
    sys.exit(app.exec())
