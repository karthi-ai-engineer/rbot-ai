"""Buddy Hub, the look: theme tokens, type scale, icons and the widgets the Hub window is built from.

Pixels: the process runs with QT_ENABLE_HIGHDPI_SCALING=0 (the buddy overlay needs plain physical pixels on
mixed 125 % / 100 % monitors), so Qt does no scaling for us. Every size here goes through px(), which applies
the scale of the screen the Hub is on (logical DPI / 96). Text is rendered at physical pixels = crisp.
"""
import math
import time

from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QLinearGradient, QPainter,
                           QPainterPath, QPalette, QPen)
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

# ---------------------------------------------------------------- tokens
DARK = dict(
    bg="#15121B", surface="#1E1A26", surface2="#272231", surface3="#312B3D", border="#2D2738",
    border2="#3E3650", text="#F4F1F8", text2="#BDB5CC", text3="#978FA8", accent_text="#C4B5FD",
    ok="#34D399", warn="#FBBF24", bad="#F87171", neutral="#A59DB5", user_bubble="#3B2A5E",
    scrim=(8, 5, 14, 150), shadow=(0, 0, 0), shadow_a=1.0, dark=True,
)
LIGHT = dict(
    bg="#F4F2F7", surface="#FFFFFF", surface2="#F1EEF6", surface3="#E7E2EF", border="#E3DEEA",
    border2="#CFC7DC", text="#1C1625", text2="#4F475E", text3="#6A6379", accent_text="#6D28D9",
    ok="#047857", warn="#B45309", bad="#C42B2B", neutral="#6A6379", user_bubble="#EDE5FD",
    scrim=(30, 20, 45, 70), shadow=(40, 20, 70), shadow_a=0.55, dark=False,
)
SKINS = {"grape": ("#8B5CF6", "#EC4899"), "mint": ("#10B981", "#3B82F6"), "sunset": ("#F59E0B", "#EF4444")}
A1, A2 = QColor("#8B5CF6"), QColor("#EC4899")       # the one accent: grape gradient


class Ctx:
    """Current theme + scale. The window is rebuilt when either changes."""
    def __init__(self):
        self.t = dict(DARK)
        self.scale = 1.0
        self.skin = "grape"
        self.reduced = False
        self.fam = None

    def set_theme(self, name):
        self.t = dict(LIGHT if name == "light" else DARK)


CTX = Ctx()


def px(n):
    return int(round(n * CTX.scale))


def fpx(n):
    return n * CTX.scale


def col(key, alpha=None):
    c = QColor(CTX.t[key])
    if alpha is not None:
        c.setAlphaF(alpha)
    return c


def rgba(key, a):
    c = QColor(CTX.t[key])
    return f"rgba({c.red()},{c.green()},{c.blue()},{a})"


# ---------------------------------------------------------------- fonts (Segoe UI Variable, fallbacks)
def families():
    if CTX.fam is None:
        have = set(QFontDatabase.families())
        pick = lambda *names: next((n for n in names if n in have), names[-1])  # noqa: E731
        CTX.fam = {"display": pick("Segoe UI Variable Display", "Segoe UI"),
                   "text": pick("Segoe UI Variable Text", "Segoe UI"),
                   "small": pick("Segoe UI Variable Small", "Segoe UI Variable Text", "Segoe UI"),
                   "mono": pick("Cascadia Code", "Cascadia Mono", "Consolas"),
                   "icons": pick("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol")}
    return CTX.fam


TYPE = {"title": (20, 600, "display"), "head": (15, 600, "text"), "body": (13, 400, "text"),
        "body_b": (13, 600, "text"), "cap": (12, 400, "small"), "cap_b": (12, 600, "small"),
        "over": (11, 600, "small"), "mono": (12, 400, "mono"), "mono_s": (11, 400, "mono")}


def font(style="body", size=None, weight=None):
    s, w, role = TYPE[style]
    f = QFont(families()[role])
    f.setPixelSize(px(size or s))
    f.setWeight(QFont.Weight(weight or w))
    f.setHintingPreference(QFont.PreferNoHinting)       # DirectWrite-like shapes, matches Windows 11 apps
    if style == "over":
        f.setLetterSpacing(QFont.AbsoluteSpacing, fpx(0.6))
    return f


def icon_font(size=16):
    f = QFont(families()["icons"])
    f.setPixelSize(px(size))
    return f


# Segoe Fluent Icons code points
IC = dict(settings="", close="", minimize="", back="", send="", stop="",
          play="", folder="", remove="", check="", cross="", mouse="",
          globe="", command="", key="", phone="", shield="", card="",
          question="", people="", pointer="", chat="", open="", expand="",
          clock="", queue="", history="", moon="", sun="", info="",
          add="", sparkle="", video="", collapse="", stopped="", warn="")
KIND_ICON = {"login": "key", "otp": "phone", "captcha": "shield", "payment": "card", "consent": "question",
             "stuck": "people", "takeover": "pointer", "ask": "chat", "mouse": "mouse"}
METHOD_ICON = {"mouse": "mouse", "browser": "globe", "command": "command", "uia": "command"}


def label(text="", style="body", color="text", wrap=False, sel=False):
    lb = QLabel(text)
    lb.setFont(font(style))
    lb.setStyleSheet(f"color:{CTX.t[color]}; background:transparent;")
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.PlainText)
    if sel:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def set_color(lb, color):
    lb.setStyleSheet(f"color:{CTX.t[color] if color in CTX.t else color}; background:transparent;")


class Elided(QLabel):
    """One-line label that elides with … instead of growing the layout."""
    def __init__(self, text="", style="body", color="text", mode=Qt.ElideRight):
        super().__init__()
        self.full, self.mode = text, mode
        self.setFont(font(style))
        set_color(self, color)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(px(24))
        self.setText(text)

    def setText(self, t):
        self.full = t
        self.setToolTip(t if len(t) > 40 else "")
        self._elide()

    def _elide(self):
        w = max(10, self.width())
        super().setText(QFontMetrics(self.font()).elidedText(self.full, self.mode, w))

    def resizeEvent(self, e):
        self._elide()
        super().resizeEvent(e)

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self.full) + 2, fm.height())


# ---------------------------------------------------------------- motion
def ease_out():
    return QEasingCurve(QEasingCurve.OutCubic)


class Smooth:
    """Named 0..1 values that ease toward targets (hover/press/checked), repainting the widget meanwhile."""
    def __init__(self, widget, **values):
        self.w, self.v, self.target = widget, dict(values), dict(values)
        self.timer = QTimer(widget)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._step)

    def set(self, name, value, instant=False):
        self.target[name] = value
        if instant or CTX.reduced:
            self.v[name] = value
            self.w.update()
        elif not self.timer.isActive():
            self.timer.start()

    def _step(self):
        moving = False
        for k, t in self.target.items():
            d = t - self.v[k]
            self.v[k] = t if abs(d) < 0.01 else self.v[k] + d * 0.3       # ~150 ms to settle
            moving |= abs(d) >= 0.01
        self.w.update()
        if not moving:
            self.timer.stop()

    def __getitem__(self, k):
        return self.v[k]


def reveal(widget, ms=200):
    """Card appear: fade + grow from 0 height (ease-out). Leaves the widget untouched afterwards."""
    old = getattr(widget, "_reveal", None)
    if old is not None:                                  # a reveal still running on this widget: replace it
        try:
            old.stop()
        except RuntimeError:
            pass
        widget._reveal = None
    if CTX.reduced:
        widget.setMaximumHeight(16777215)
        widget.setGraphicsEffect(None)
        widget.show()
        return
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    widget.show()
    target = max(widget.sizeHint().height(), widget.minimumSizeHint().height())
    widget.setMaximumHeight(0)
    anim = QVariantAnimation(widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(ease_out())

    def frame(v):
        try:
            widget.setMaximumHeight(int(target * v) + 1)
            eff.setOpacity(min(1.0, v * 1.3))
        except RuntimeError:                             # widget / effect deleted mid-animation
            anim.stop()

    def done():
        try:
            widget._reveal = None
            widget.setMaximumHeight(16777215)
            widget.setGraphicsEffect(None)
        except RuntimeError:
            pass

    anim.valueChanged.connect(frame)
    anim.finished.connect(done)
    anim.start(QVariantAnimation.DeleteWhenStopped)
    widget._reveal = anim


def blend(a, b, t):
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def grad(r, horizontal=True, c1=None, c2=None):
    g = QLinearGradient(r.topLeft(), r.topRight() if horizontal else r.bottomRight())
    g.setColorAt(0, c1 or A1)
    g.setColorAt(1, c2 or A2)
    return g


# ---------------------------------------------------------------- small pieces
class Glyph(QWidget):
    def __init__(self, name, size=16, color="text2", box=None):
        super().__init__()
        self.name, self.size_, self.color = name, size, color
        b = px(box or size + 4)
        self.setFixedSize(b, b)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set(self, name=None, color=None):
        self.name = name or self.name
        self.color = color or self.color
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(icon_font(self.size_))
        p.setPen(col(self.color) if self.color in CTX.t else QColor(self.color))
        p.drawText(self.rect(), Qt.AlignCenter, IC.get(self.name, self.name))


class BuddyFace(QWidget):
    """The buddy's round face (brand mark): skin gradient, two eyes, blush."""
    def __init__(self, size=24, parent=None):
        super().__init__(parent)
        self.setFixedSize(px(size), px(size))
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, _):
        paint_face(QPainter(self), QRectF(self.rect()))


def paint_face(p, r, c1=None, c2=None, ring=True):
    p.setRenderHint(QPainter.Antialiasing)
    s1, s2 = SKINS.get(CTX.skin, SKINS["grape"])
    d = min(r.width(), r.height())
    k = d / 20.0
    rr = QRectF(r.center().x() - 8 * k, r.center().y() - 8 * k, 16 * k, 16 * k)
    g = QLinearGradient(rr.topLeft(), rr.bottomRight())
    g.setColorAt(0, c1 or QColor(s1))
    g.setColorAt(1, c2 or QColor(s2))
    p.setPen(QPen(QColor(255, 255, 255, 235), 1.2 * k) if ring else Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(rr)
    cx, cy = rr.center().x(), rr.center().y()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#2A1240"))
    p.drawEllipse(QPointF(cx - 2.6 * k, cy + 0.6 * k), 1.25 * k, 1.65 * k)
    p.drawEllipse(QPointF(cx + 2.6 * k, cy + 0.6 * k), 1.25 * k, 1.65 * k)
    p.setBrush(QColor(255, 150, 200, 150))
    p.drawEllipse(QPointF(cx - 4.7 * k, cy + 3.3 * k), 1.4 * k, 0.9 * k)
    p.drawEllipse(QPointF(cx + 4.7 * k, cy + 3.3 * k), 1.4 * k, 0.9 * k)


class PulseDot(QWidget):
    """Status dot. running: grape gradient with a soft breathing halo; needs: amber; idle: grey."""
    def __init__(self, size=10, box=18):
        super().__init__()
        self.state, self.d = "idle", size
        self.setFixedSize(px(box), px(box))
        self.t0 = time.monotonic()
        self.frozen = None
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_state(self, s):
        self.state = s
        self._sync()
        self.update()

    def _sync(self):
        if self.state in ("running", "needs") and self.isVisible() and not CTX.reduced:
            self.timer.start()
        else:
            self.timer.stop()

    def showEvent(self, e):
        self._sync()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = fpx(self.d) / 2
        if self.state == "idle":
            p.setPen(Qt.NoPen)
            p.setBrush(col("text3"))
            p.drawEllipse(c, r * 0.8, r * 0.8)
            return
        ph = self.frozen if self.frozen is not None else \
            0.5 - 0.5 * math.cos(2 * math.pi * (time.monotonic() - self.t0) / 1.6)
        if CTX.reduced:
            ph = 0.4
        base = QColor(CTX.t["warn"]) if self.state == "needs" else blend(A1, A2, 0.5)
        halo = QColor(base)
        halo.setAlphaF(0.30 * (1 - ph))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        hr = r + (self.width() / 2 - r) * ph
        p.drawEllipse(c, hr, hr)
        if self.state == "needs":
            p.setBrush(base)
        else:
            p.setBrush(grad(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), False))
        p.drawEllipse(c, r, r)


class Badge(QWidget):
    """Small rounded tag: [icon] Text.  tone: neutral | accent | ok | warn | bad."""
    def __init__(self, text="", icon=None, tone="neutral"):
        super().__init__()
        self.text, self.icon, self.tone = text, icon, tone
        self.f = font("cap_b")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set(self, text, icon=None, tone=None):
        self.text, self.icon = text, icon
        self.tone = tone or self.tone
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        fm = QFontMetrics(self.f)
        w = fm.horizontalAdvance(self.text) + px(16) + (px(18) if self.icon else 0)
        return QSize(w, px(22))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fg = {"accent": col("accent_text"), "ok": col("ok"), "warn": col("warn"), "bad": col("bad")}.get(
            self.tone, col("text2"))
        bg = QColor(fg)
        bg.setAlphaF(0.14 if self.tone != "neutral" else (0.10 if CTX.t["dark"] else 0.08))
        path = QPainterPath()
        path.addRoundedRect(r, fpx(6), fpx(6))
        p.fillPath(path, bg)
        x = r.left() + fpx(8)
        if self.icon:
            p.setFont(icon_font(12))
            p.setPen(fg)
            p.drawText(QRectF(x, r.top(), fpx(14), r.height()), Qt.AlignCenter, IC[self.icon])
            x += fpx(18)
        p.setFont(self.f)
        p.setPen(fg)
        p.drawText(QRectF(x, r.top(), r.right() - x, r.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text)


class Button(QAbstractButton):
    """kind: primary (grape gradient) | secondary | danger | warn | ghost | icon (square, glyph only)."""
    def __init__(self, text="", kind="secondary", icon=None, small=False, tip=""):
        super().__init__()
        self.setText(text)
        self.kind, self.icon, self.small = kind, icon, small
        self.setFont(font("cap_b" if small else "body_b"))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip(tip)
        self.setAccessibleName(text or tip)
        self.anim = Smooth(self, hover=0.0, press=0.0)
        self._kbd = False
        self.pressed.connect(lambda: self.anim.set("press", 1.0, instant=True))
        self.released.connect(lambda: self.anim.set("press", 0.0))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self):
        h = px(28 if self.small else 32)
        if self.kind == "icon":
            return QSize(h, h)
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self.text()) + px(24 if self.small else 32)
        if self.icon:
            w += px(20)
        return QSize(max(w, px(64) if not self.small else 0), h)

    def enterEvent(self, e):
        self.anim.set("hover", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.anim.set("hover", 0.0)
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._kbd = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        super().focusInEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.isEnabled():
            self.animateClick()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h, pr = (self.anim["hover"] if self.isEnabled() else 0.0), self.anim["press"]
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if pr:
            s = 1 - 0.03 * pr
            p.translate(r.center())
            p.scale(s, s)
            p.translate(-r.center())
        if not self.isEnabled():
            p.setOpacity(0.4)
        rad = fpx(8)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        dark = CTX.t["dark"]
        fg = col("text")
        if self.kind == "primary":
            p.fillPath(path, grad(r, True, A1.lighter(100 + int(8 * h)), A2.lighter(100 + int(8 * h))))
            p.strokePath(path, QPen(QColor(255, 255, 255, 40 + int(30 * h)), 1))
            fg = QColor("white")
        elif self.kind in ("danger", "warn"):
            base = col("bad" if self.kind == "danger" else "warn")
            bg = QColor(base)
            bg.setAlphaF(0.12 + 0.10 * h)
            p.fillPath(path, bg)
            bd = QColor(base)
            bd.setAlphaF(0.35 + 0.35 * h)
            p.strokePath(path, QPen(bd, 1))
            fg = QColor(base) if not dark else QColor(base).lighter(108)
        elif self.kind in ("ghost", "icon"):
            bg = col("text")
            bg.setAlphaF(0.07 * h + 0.05 * pr)
            p.fillPath(path, bg)
            fg = blend(col("text2"), col("text"), h)
        else:
            p.fillPath(path, blend(col("surface2"), col("surface3"), h))
            p.strokePath(path, QPen(blend(col("border2"), col("text3"), h * 0.6), 1))
        if self.hasFocus() and self._kbd:
            ring = QPainterPath()
            ring.addRoundedRect(r.adjusted(-0.5, -0.5, 0.5, 0.5), rad, rad)
            p.strokePath(ring, QPen(col("accent_text"), 2))
        p.setPen(fg)
        if self.kind == "icon":
            p.setFont(icon_font(14 if not self.small else 12))
            p.drawText(r, Qt.AlignCenter, IC.get(self.icon, self.icon or ""))
            return
        fm = QFontMetrics(self.font())
        text_w = min(fm.horizontalAdvance(self.text()), int(r.width()) - px(16))
        total = text_w + (px(20) if self.icon else 0)
        x = r.center().x() - total / 2
        if self.icon:
            p.setFont(icon_font(12 if self.small else 13))
            p.drawText(QRectF(x, r.top(), fpx(14), r.height()), Qt.AlignCenter, IC[self.icon])
            x += fpx(20)
        p.setFont(self.font())
        p.drawText(QRectF(x, r.top(), text_w + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self.text(), Qt.ElideRight, text_w + 2))


class Toggle(QAbstractButton):
    """Windows 11 style switch (40×20), animated knob."""
    def __init__(self, checked=False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.anim = Smooth(self, on=1.0 if checked else 0.0, hover=0.0)
        self.toggled.connect(lambda on: self.anim.set("on", 1.0 if on else 0.0))
        self.setFixedSize(px(40), px(20))

    def enterEvent(self, e):
        self.anim.set("hover", 1.0)

    def leaveEvent(self, e):
        self.anim.set("hover", 0.0)

    def sizeHint(self):
        return QSize(px(40), px(20))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, h = self.anim["on"], self.anim["hover"]
        if not self.isEnabled():
            p.setOpacity(0.4)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        off_bg = col("surface2")
        on_bg = A1.lighter(100 + int(8 * h))
        p.fillPath(path, blend(off_bg, on_bg, on))
        if on < 1:
            bd = blend(col("text3"), col("text2"), h)
            bd.setAlphaF(1 - on)
            p.strokePath(path, QPen(bd, 1))
        k = r.height() / 2 - fpx(4) + fpx(1) * h
        cx = r.left() + r.height() / 2 + (r.width() - r.height()) * on
        p.setPen(Qt.NoPen)
        p.setBrush(blend(col("text2"), QColor("white"), on))
        p.drawEllipse(QPointF(cx, r.center().y()), k, k)
        if self.hasFocus():
            ring = QPainterPath()
            ring.addRoundedRect(r.adjusted(-1, -1, 1, 1), r.height() / 2 + 1, r.height() / 2 + 1)
            p.strokePath(ring, QPen(col("accent_text"), 1.5))


class Segmented(QWidget):
    """[ Live | Background · soon ] picker. options: [(value, label, enabled)]."""
    changed = Signal(str)

    def __init__(self, options, value):
        super().__init__()
        self.options, self.value = options, value
        self.f = font("cap_b")
        self.hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(px(32))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self):
        fm = QFontMetrics(self.f)
        w = sum(fm.horizontalAdvance(l) + px(28) for _, l, _ in self.options) + px(4)
        return QSize(w, px(32))

    def _rects(self):
        fm = QFontMetrics(self.f)
        x, out = fpx(2), []
        for _, l, _ in self.options:
            w = fm.horizontalAdvance(l) + px(28)
            out.append(QRectF(x, fpx(2), w, self.height() - fpx(4)))
            x += w
        return out

    def mouseMoveEvent(self, e):
        i = next((i for i, r in enumerate(self._rects()) if r.contains(e.position())), -1)
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e):
        self.hover = -1
        self.update()

    def mousePressEvent(self, e):
        for (v, _, en), r in zip(self.options, self._rects()):
            if r.contains(e.position()) and en and v != self.value:
                self.value = v
                self.update()
                self.changed.emit(v)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(outer, fpx(8), fpx(8))
        p.fillPath(path, col("surface2"))
        p.strokePath(path, QPen(col("border"), 1))
        p.setFont(self.f)
        for i, ((v, l, en), r) in enumerate(zip(self.options, self._rects())):
            if v == self.value:
                sp = QPainterPath()
                sp.addRoundedRect(r, fpx(6), fpx(6))
                p.fillPath(sp, col("surface3") if CTX.t["dark"] else QColor("white"))
                p.strokePath(sp, QPen(col("border2"), 1))
            elif i == self.hover and en:
                sp = QPainterPath()
                sp.addRoundedRect(r, fpx(6), fpx(6))
                p.fillPath(sp, col("text", 0.05))
            p.setPen(col("text") if v == self.value else col("text2") if en else col("text3"))
            p.drawText(r, Qt.AlignCenter, l)


class Card(QFrame):
    """Rounded 12 px surface, 1 px border. accent: optional status colour key for a left stripe + tint."""
    def __init__(self, accent=None, pad=16):
        super().__init__()
        self.accent = accent
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(px(pad), px(pad - 2), px(pad), px(pad - 2))
        self.lay.setSpacing(px(8))
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, fpx(12), fpx(12))
        p.fillPath(path, col("surface"))
        bd = col("border")
        if self.accent:
            tint = col(self.accent, 0.07 if CTX.t["dark"] else 0.06)
            p.fillPath(path, tint)
            bd = col(self.accent, 0.38)
        p.strokePath(path, QPen(bd, 1))
        if self.accent:                                    # 3 px stripe hugging the left edge
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(r.left(), r.top(), fpx(3), r.height()), col(self.accent))
            p.restore()


class SectionHeader(QWidget):
    def __init__(self, text, count=None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(4), 0, px(4), 0)
        lay.setSpacing(px(8))
        self.title = label(text, "cap_b", "text2")
        self.count = label("", "cap_b", "text3")
        lay.addWidget(self.title)
        lay.addWidget(self.count)
        lay.addStretch(1)
        self.set_count(count)

    def set_count(self, n):
        self.count.setText(str(n) if n else "")
        self.count.setVisible(bool(n))


class EmptyState(QWidget):
    def __init__(self, icon, text, sub=""):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(16), px(14), px(16), px(14))
        lay.setSpacing(px(12))
        g = BuddyFace(28) if icon == "face" else Glyph(icon, 16, "text3", box=32)
        lay.addWidget(g, 0, Qt.AlignVCenter)
        col_ = QVBoxLayout()
        col_.setSpacing(px(2))
        self.text = label(text, "body", "text2", wrap=True)
        col_.addWidget(self.text)
        if sub:
            col_.addWidget(label(sub, "cap", "text3", wrap=True))
        lay.addLayout(col_, 1)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, fpx(12), fpx(12))
        pen = QPen(col("border2"), 1, Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.strokePath(path, pen)


class IconCircle(QWidget):
    """Status disc for list rows: ✓ green, ✗ red, ■ neutral, or a needs-you kind in amber."""
    def __init__(self, icon, tone, size=32):
        super().__init__()
        self.icon, self.tone = icon, tone
        self.setFixedSize(px(size), px(size))
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = col({"ok": "ok", "bad": "bad", "warn": "warn"}.get(self.tone, "neutral"))
        bg = QColor(c)
        bg.setAlphaF(0.16)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawEllipse(r)
        p.setPen(c)
        p.setFont(icon_font(max(10, int(self.width() / CTX.scale * 0.46))))
        p.drawText(r, Qt.AlignCenter, IC.get(self.icon, self.icon))


# ---------------------------------------------------------------- input
class ChatInput(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setFont(font("body"))
        self.setFixedHeight(px(40))
        self.setPlaceholderText("Type a task…")
        pal = self.palette()
        pal.setColor(QPalette.PlaceholderText, col("text3"))
        self.setPalette(pal)
        self.setStyleSheet(f"""
            QLineEdit {{ background:{CTX.t['surface2']}; border:1px solid {CTX.t['border2']};
                        border-radius:{px(10)}px; color:{CTX.t['text']}; padding:0 {px(12)}px;
                        selection-background-color:#7C3AED; selection-color:white; }}
            QLineEdit:focus {{ border:1px solid {CTX.t['accent_text']}; background:{CTX.t['surface']}; }}
        """)


class SendButton(QAbstractButton):
    """Round grape button with the send glyph: the single strongest accent on the screen."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(px(40), px(40))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Send (Enter)")
        self.setAccessibleName("Send")
        self.anim = Smooth(self, hover=0.0)

    def enterEvent(self, e):
        self.anim.set("hover", 1.0)

    def leaveEvent(self, e):
        self.anim.set("hover", 0.0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, fpx(10), fpx(10))
        h = self.anim["hover"]
        if self.isEnabled():
            p.fillPath(path, grad(r, False, A1.lighter(100 + int(10 * h)), A2.lighter(100 + int(10 * h))))
            p.setPen(QColor("white"))
        else:
            p.fillPath(path, col("surface2"))
            p.setPen(col("text3"))
        p.setFont(icon_font(15))
        p.drawText(r, Qt.AlignCenter, IC["send"])


def scroll_qss():
    return f"""
        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: {px(10)}px; margin: {px(2)}px; }}
        QScrollBar::handle:vertical {{ background: {rgba('text3', 0.35)}; border-radius: {px(3)}px;
                                       min-height: {px(28)}px; margin: 0 {px(1)}px; }}
        QScrollBar::handle:vertical:hover {{ background: {rgba('text3', 0.6)}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QToolTip {{ background: {CTX.t['surface3']}; color: {CTX.t['text']}; border: 1px solid {CTX.t['border2']};
                    padding: {px(4)}px {px(8)}px; border-radius: {px(6)}px; }}
        QSpinBox {{ background:{CTX.t['surface2']}; border:1px solid {CTX.t['border2']}; border-radius:{px(6)}px;
                    color:{CTX.t['text']}; padding:{px(2)}px {px(6)}px; }}
    """


def scroller(inner):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setWidget(inner)
    sa.viewport().setAutoFillBackground(False)
    inner.setAutoFillBackground(False)
    return sa


def paint_shadow(p, r, radius, layers=8, spread=2.2, drop=3, strength=1.0):
    """Soft, slightly dropped shadow around rect r (window-level, painted on a translucent window)."""
    p.setPen(Qt.NoPen)
    sc = CTX.t["shadow"]
    base = CTX.t["shadow_a"] * strength
    for i in range(layers):
        grow = fpx(1 + i * spread)
        a = int(base * 34 * (1 - i / layers) ** 2)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(-grow, -grow + fpx(drop), grow, grow + fpx(drop)), radius + grow, radius + grow)
        p.fillPath(path, QColor(sc[0], sc[1], sc[2], a))


__all__ = ["CTX", "px", "fpx", "col", "font", "icon_font", "IC", "label", "Elided", "Glyph", "BuddyFace",
           "PulseDot", "Badge", "Button", "Toggle", "Segmented", "Card", "SectionHeader", "EmptyState",
           "IconCircle", "ChatInput", "SendButton", "scroller", "scroll_qss", "paint_shadow", "reveal",
           "paint_face", "KIND_ICON", "METHOD_ICON", "SKINS", "grad", "blend", "A1", "A2", "rgba", "set_color"]
