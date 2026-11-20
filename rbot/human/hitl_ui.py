"""Human-in-the-loop UI for the buddy agent: a question card next to the buddy, and a slim "your turn" chip.

    ui = HumanUI(cursor)                         # create in the Qt (main) thread
    # from the agent worker thread (all thread-safe):
    ui.ask("Accept the cookies?", ["Accept all", "Reject all"])   -> "Reject all" | typed text | None
    ui.handover("Sign in", "Log into your account")   # buddy hops, parks; chip at the top of the screen
    ui.status("continuing in 3…");  ui.end_handover();  ui.stuck("Can't find Save")
    ui.done_clicked / ui.stop_clicked            # threading.Events

Both windows are always on top and excluded from screen capture (the agent never sees them in its own
screenshots). See scripts/hitl_ui_preview.py for a live demo, an offscreen review sheet and a self-test.
"""
import ctypes
import os
import sys
import threading
import time

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")          # same pixel space as cursor_overlay.py

from PySide6.QtCore import (QEasingCurve, QEventLoop, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread,
                            QTimer, QVariantAnimation, Signal, QObject)
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QKeySequence, QLinearGradient,
                           QPainter, QPainterPath, QPalette, QPen, QShortcut)
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget)

C1, C2 = QColor("#8B5CF6"), QColor("#EC4899")
INK = QColor("#2A1240")
TEXT, MUTED, SOFT = QColor("#F4F0FB"), QColor("#A99BC4"), QColor("#C4B5FD")
REC_RED = QColor(255, 59, 72)
PAD = 22                                     # room around a card for its soft shadow
CARD_W = 372                                 # question card width
EASE_OUT = QEasingCurve(QEasingCurve.BezierSpline)                  # strong ease-out: cubic-bezier(.23,1,.32,1)
EASE_OUT.addCubicBezierSegment(QPointF(0.23, 1.0), QPointF(0.32, 1.0), QPointF(1.0, 1.0))
ANSWER_SAY = {"answer": "Got it!", "skip": "Okay, skipping that", "timeout": "No answer — I'll carry on",
              "stopped": "Stopping"}


def font(px, weight=QFont.Normal):
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def shorten(text, n=34):
    """First sentence, cut on a word boundary: what the buddy's speech bubble shows."""
    t = " ".join(str(text).split())
    for stop in ("? ", ". ", "! "):
        if stop in t:
            t = t[:t.index(stop) + 1]
    if len(t) <= n:
        return t
    cut = t[:n - 1].rsplit(" ", 1)[0] if " " in t[:n - 1] else t[:n - 1]
    return cut.rstrip(",;:-— ") + "…"


def reduced_motion():
    if sys.platform != "win32":
        return False
    anim = ctypes.c_int(1)                    # Settings > Accessibility > "Animation effects"
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(anim), 0)
    return not anim.value


# ---- win32: keep out of the agent's screenshots, grab / give back keyboard focus ------------------
if sys.platform == "win32":
    from ctypes import wintypes
    _u32 = ctypes.WinDLL("user32", use_last_error=True)          # private copy: our argtypes don't leak
    _k32 = ctypes.WinDLL("kernel32")
    for _name, _args, _res in (("SetWindowDisplayAffinity", [wintypes.HWND, wintypes.DWORD], wintypes.BOOL),
                               ("GetForegroundWindow", [], wintypes.HWND),
                               ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
                               ("BringWindowToTop", [wintypes.HWND], wintypes.BOOL),
                               ("SetFocus", [wintypes.HWND], wintypes.HWND),
                               ("IsWindow", [wintypes.HWND], wintypes.BOOL),
                               ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.c_void_p], wintypes.DWORD),
                               ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)):
        getattr(_u32, _name).argtypes, getattr(_u32, _name).restype = _args, _res


def _native():
    return sys.platform == "win32" and QGuiApplication.platformName() == "windows"


def exclude_from_capture(widget, on=True):
    if _native():
        _u32.SetWindowDisplayAffinity(int(widget.winId()), 0x11 if on else 0)    # WDA_EXCLUDEFROMCAPTURE


def take_focus(widget):
    """Bring our window to the foreground even though another app (the one the agent drives) owns it.
    Returns the window that had the focus, so it can be given back afterwards."""
    widget.raise_()
    widget.activateWindow()
    if not _native():
        return None
    hwnd = int(widget.winId())
    fg = _u32.GetForegroundWindow()
    if not fg or fg == hwnd:
        return None
    me, other = _k32.GetCurrentThreadId(), _u32.GetWindowThreadProcessId(fg, None)
    attached = bool(other and other != me and _u32.AttachThreadInput(me, other, True))
    try:
        _u32.BringWindowToTop(hwnd)
        _u32.SetForegroundWindow(hwnd)
        _u32.SetFocus(hwnd)
    finally:
        if attached:
            _u32.AttachThreadInput(me, other, False)
    return fg


def give_back_focus(widget, prev):
    """Only if we still hold the focus (the user didn't click somewhere else meanwhile)."""
    if _native() and prev and _u32.IsWindow(prev) and _u32.GetForegroundWindow() == int(widget.winId()):
        _u32.SetForegroundWindow(prev)


# ---- pieces ------------------------------------------------------------------------------------
class _Smooth:
    """A couple of 0..1 values that ease towards targets (hover / press), repainting while they move."""
    def __init__(self, widget, **values):
        self.w, self.v, self.target = widget, dict(values), dict(values)
        self.timer = QTimer(widget)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._step)

    def set(self, name, value, instant=False):
        self.target[name] = value
        if instant:
            self.v[name] = value
            self.w.update()
        elif not self.timer.isActive():
            self.timer.start()

    def _step(self):
        moving = False
        for k, t in self.target.items():
            d = t - self.v[k]
            self.v[k] = t if abs(d) < 0.01 else self.v[k] + d * 0.38      # ~110 ms to settle
            moving |= abs(d) >= 0.01
        self.w.update()
        if not moving:
            self.timer.stop()

    def __getitem__(self, k):
        return self.v[k]


class Pill(QAbstractButton):
    """Rounded button. kind: primary (gradient) | secondary | danger | ghost. Presses scale to 0.97."""
    def __init__(self, text, kind="secondary", small=False, focusable=True, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.kind, self.small = kind, small
        self.setFont(font(12 if small else 13, QFont.DemiBold))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus if focusable else Qt.NoFocus)
        self.anim = _Smooth(self, hover=0.0, press=0.0)
        self._kbd_focus = False
        self.pressed.connect(lambda: self.anim.set("press", 1.0, instant=True))
        self.released.connect(lambda: self.anim.set("press", 0.0))

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        h = 26 if self.small else 34
        pad = 12 if self.small else 16
        return QSize(min(fm.horizontalAdvance(self.text()), 300) + pad * 2, h)

    def enterEvent(self, e):
        self.anim.set("hover", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.anim.set("hover", 0.0)
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._kbd_focus = e.reason() != Qt.MouseFocusReason
        super().focusInEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.isEnabled():
            self.anim.set("press", 1.0, instant=True)
            QTimer.singleShot(90, lambda: (self.anim.set("press", 0.0), self.click()))
            return
        super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h, pr = self.anim["hover"] if self.isEnabled() else 0.0, self.anim["press"]
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        s = 1 - 0.03 * pr
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        if not self.isEnabled():
            p.setOpacity(0.45)
        rad = r.height() / 2
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        text_col = QColor(TEXT)
        if self.kind == "primary":
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0, C1.lighter(100 + int(10 * h)))
            g.setColorAt(1, C2.lighter(100 + int(10 * h)))
            p.fillPath(path.translated(0, 1.5), QColor(236, 72, 153, int(50 + 40 * h)))   # coloured glow
            p.fillPath(path, g)
            p.strokePath(path, QPen(QColor(255, 255, 255, 60 + int(40 * h)), 1))
            text_col = QColor("white")
        elif self.kind == "danger":
            p.fillPath(path, QColor(244, 63, 94, int(18 + 40 * h)))
            p.strokePath(path, QPen(QColor(244, 63, 94, int(90 + 110 * h)), 1))
            text_col = QColor("#FDA4AF").lighter(100 + int(12 * h))
        elif self.kind == "ghost":
            p.fillPath(path, QColor(255, 255, 255, int(16 * h)))
            text_col = QColor(MUTED).lighter(100 + int(30 * h))
        else:
            p.fillPath(path, QColor("#2B2240").lighter(100 + int(18 * h)))
            border = QColor("#4B3A70")
            border = QColor(int(border.red() + (167 - border.red()) * h), int(border.green() + (139 - border.green()) * h),
                            int(border.blue() + (250 - border.blue()) * h))
            p.strokePath(path, QPen(border, 1))
        if self.hasFocus() and self._kbd_focus:
            ring = QPainterPath()
            ring.addRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), rad + 1.5, rad + 1.5)
            p.strokePath(ring, QPen(SOFT, 2))
        p.setPen(text_col)
        p.setFont(self.font())
        txt = QFontMetrics(self.font()).elidedText(self.text(), Qt.ElideRight, int(r.width()) - 16)
        p.drawText(r, Qt.AlignCenter, txt)


class BuddyBadge(QWidget):
    """Tiny buddy face for the card header."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(2, 2, 18, 18)
        g.setColorAt(0, C1)
        g.setColorAt(1, C2)
        p.setPen(QPen(QColor(255, 255, 255, 230), 1.4))
        p.setBrush(g)
        p.drawEllipse(QRectF(2, 2, 16, 16))
        p.setPen(Qt.NoPen)
        p.setBrush(INK)
        p.drawEllipse(QPointF(7.4, 9.6), 1.2, 1.6)
        p.drawEllipse(QPointF(12.6, 9.6), 1.2, 1.6)
        p.setBrush(QColor(255, 140, 190, 170))
        p.drawEllipse(QPointF(5.4, 12.4), 1.4, 0.9)
        p.drawEllipse(QPointF(14.6, 12.4), 1.4, 0.9)


class RecDot(QWidget):
    """"● REC" with a softly pulsing red dot."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 22)
        self.t0 = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update)
        self.frozen = None                       # fixed pulse phase for offscreen renders

    def showEvent(self, e):
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, QColor(255, 59, 72, 26))
        p.strokePath(path, QPen(QColor(255, 59, 72, 70), 1))
        pulse = self.frozen if self.frozen is not None else \
            0.5 + 0.5 * math.cos(2 * math.pi * (time.monotonic() - self.t0) / 1.4)
        halo = QColor(REC_RED)
        halo.setAlphaF(0.22 * pulse)
        c = QPointF(12, r.center().y())
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, 6.5, 6.5)
        dot = QColor(REC_RED)
        dot.setAlphaF(0.55 + 0.45 * pulse)
        p.setBrush(dot)
        p.drawEllipse(c, 3.6, 3.6)
        p.setPen(QColor("#FFD5D9"))
        p.setFont(font(11, QFont.Bold))
        p.drawText(r.adjusted(21, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "REC")


class Card(QWidget):
    """Dark grape card: subtle vertical gradient fill + a hairline gradient border."""
    def __init__(self, parent, radius):
        super().__init__(parent)
        self.radius = radius

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = min(self.radius, r.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, QColor("#251C3A"))
        g.setColorAt(1, QColor("#1A1428"))
        p.fillPath(path, g)
        b = QLinearGradient(r.topLeft(), r.bottomRight())
        b.setColorAt(0, QColor(167, 139, 250, 150))
        b.setColorAt(0.5, QColor(167, 139, 250, 55))
        b.setColorAt(1, QColor(236, 72, 153, 120))
        p.strokePath(path, QPen(b, 1))
        hl = QPainterPath()                                      # faint top highlight, like light from above
        hl.addRoundedRect(r.adjusted(1, 1, -1, -1), rad - 1, rad - 1)
        top = QLinearGradient(r.topLeft(), r.topLeft() + QPointF(0, 18))
        top.setColorAt(0, QColor(255, 255, 255, 22))
        top.setColorAt(1, QColor(255, 255, 255, 0))
        p.strokePath(hl, QPen(top, 1))


class Floating(QWidget):
    """Frameless, translucent, always-on-top window holding one Card, excluded from screen capture.
    present()/dismiss() play a short scale + fade by painting a snapshot of the card (Qt can't
    transform a live top-level window); the live card sits just outside the window meanwhile, so it
    keeps keyboard focus and nothing typed during the animation is lost."""
    ENTER_MS, EXIT_MS = 180, 140

    def __init__(self, radius, focusable, scale_from, rise=0.0):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        if not focusable:
            flags |= Qt.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        if not focusable:
            self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.card = Card(self, radius)
        self.radius, self.scale_from, self.rise = radius, scale_from, rise
        self.origin = QPointF(0.5, 0.0)                         # transform origin, as a fraction of the card
        self.reduced = False
        self.t, self.snap, self.closing = 1.0, None, False
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setEasingCurve(EASE_OUT)
        self.anim.valueChanged.connect(self._frame)
        self.anim.finished.connect(self._settled)
        self._drag = None
        exclude_from_capture(self)

    # ---- geometry ----
    def card_size(self):
        return self.card.sizeHint()

    def fit(self):
        lay = self.card.layout()
        lay.activate()
        s = self.card_size()
        self.resize(s.width() + 2 * PAD, s.height() + 2 * PAD)
        self.card.setGeometry(PAD, PAD, s.width(), s.height())

    def card_rect(self):
        return QRectF(PAD, PAD, self.width() - 2 * PAD, self.height() - 2 * PAD)

    # ---- show / hide ----
    def present(self, card_top_left):
        self.fit()
        self.move(card_top_left - QPoint(PAD, PAD))
        self.closing = False
        self._animate(enter=True)
        self.show()
        self.raise_()

    def dismiss(self):
        if not self.isVisible() or self.closing:
            return
        self.closing = True
        self._animate(enter=False)

    def _animate(self, enter):
        self.anim.stop()
        self.snap = self.card.grab()
        self.card.move(-10 * self.width(), PAD)                 # off-window: invisible, still focused
        start = self.t if self.isVisible() else 0.0
        self.anim.setDuration(int((self.ENTER_MS if enter else self.EXIT_MS) * (0.6 if self.reduced else 1)))
        self.anim.setStartValue(start)
        self.anim.setEndValue(1.0 if enter else 0.0)
        self.anim.start()

    def _frame(self, v):
        self.t = float(v)
        self.update()

    def _settled(self):
        self.snap = None
        if self.closing:
            self.hide()
            self.closing = False
            self.t = 0.0
        else:
            self.card.move(PAD, PAD)
        self.update()

    # ---- painting ----
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        cr = self.card_rect()
        if self.snap is not None:
            t = self.t
            s = 1.0 if self.reduced else self.scale_from + (1 - self.scale_from) * t
            o = QPointF(cr.x() + cr.width() * self.origin.x(), cr.y() + cr.height() * self.origin.y())
            p.setOpacity(min(1.0, t * 1.4) if not self.closing else t)          # fade leads the scale a bit
            p.translate(0, -self.rise * (1 - t) * (0 if self.reduced else 1))
            p.translate(o)
            p.scale(s, s)
            p.translate(-o)
            self.paint_shadow(p, cr)
            p.drawPixmap(cr.topLeft(), self.snap)
        else:
            self.paint_shadow(p, cr)

    def paint_shadow(self, p, cr):
        rad = min(self.radius, cr.height() / 2)
        p.setPen(Qt.NoPen)
        for i, a in enumerate((26, 18, 12, 8, 5, 3)):           # soft, slightly dropped shadow
            grow = 1.5 + i * 2.6
            path = QPainterPath()
            path.addRoundedRect(cr.adjusted(-grow, -grow + 4, grow, grow + 4), rad + grow, rad + grow)
            p.fillPath(path, QColor(8, 4, 16, a))

    # ---- drag the card around (it may cover something you need) ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        self._drag = None


# ---- the question card --------------------------------------------------------------------------
STYLE = """
QLabel#head { color: #C4B5FD; font: 600 12px 'Segoe UI'; }
QLabel#question { color: #F4F0FB; font: 15px 'Segoe UI'; }
QLabel#hint { color: #8A7BA8; font: 11px 'Segoe UI'; }
QLineEdit { background: #140F1E; border: 1px solid #3F3160; border-radius: 17px; color: white;
            font: 13px 'Segoe UI'; padding: 0 14px; selection-background-color: #7C3AED; }
QLineEdit:focus { border: 1px solid #A78BFA; background: #181225; }
"""


class AskPopup(Floating):
    def __init__(self):
        super().__init__(radius=18, focusable=True, scale_from=0.94)
        self.setStyleSheet(STYLE)
        self.card.setFixedWidth(CARD_W)
        self.on_answer = None                   # fn(text, reason) - set by HumanUI
        self.options, self.pills, self.prev_fg, self.allow_text = [], [], None, True

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(BuddyBadge())
        h = QLabel("rbot-ai needs you")
        h.setObjectName("head")
        head.addWidget(h)
        head.addStretch(1)
        self.question = QLabel()
        self.question.setObjectName("question")
        self.question.setWordWrap(True)
        self.question.setTextFormat(Qt.PlainText)
        self.pill_box = QVBoxLayout()
        self.pill_box.setSpacing(8)
        self.text = QLineEdit()
        self.text.setFixedHeight(36)
        self.text.setPlaceholderText("Type your answer… (voice coming later)")
        pal = self.text.palette()
        pal.setColor(QPalette.PlaceholderText, QColor("#7D6E9C"))
        self.text.setPalette(pal)
        self.text.returnPressed.connect(self._send)
        self.send = Pill("Send", "primary")
        self.send.setFixedHeight(36)
        self.send.clicked.connect(self._send)
        self.text.textChanged.connect(self._sync_send)
        self.text_row = QWidget()
        tr = QHBoxLayout(self.text_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(8)
        tr.addWidget(self.text, 1)
        tr.addWidget(self.send)
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        self.stop_btn = Pill("Stop agent", "ghost", small=True, focusable=False)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addWidget(self.hint)
        foot.addStretch(1)
        foot.addWidget(self.stop_btn)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(18, 14, 18, 10)
        lay.setSpacing(0)
        lay.addLayout(head)
        lay.addSpacing(8)
        lay.addWidget(self.question)
        lay.addSpacing(14)
        self.sp_question = lay.itemAt(lay.count() - 1).spacerItem()
        lay.addLayout(self.pill_box)
        lay.addSpacing(10)
        self.sp_text = lay.itemAt(lay.count() - 1).spacerItem()
        lay.addWidget(self.text_row)
        lay.addSpacing(8)
        lay.addLayout(foot)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=lambda: self._answer(None, "skip"))

    def card_size(self):
        lay = self.card.layout()
        return QSize(CARD_W, lay.totalHeightForWidth(CARD_W) if lay.hasHeightForWidth()
                     else lay.totalSizeHint().height())

    def load(self, question, options, allow_text):
        self.options = list(options)
        self.question.setText(question)
        while self.pill_box.count():                        # clear old rows of pills
            row = self.pill_box.takeAt(0).layout()
            while row.count():
                w = row.takeAt(0).widget()
                if w:
                    w.hide()                                # gone now, not at the next event loop turn
                    w.setParent(None)
                    w.deleteLater()
        self.pills = []
        width, row, used = CARD_W - 36, None, 0
        for i, opt in enumerate(self.options):              # primary first; wrap onto new rows as needed
            b = Pill(opt, "primary" if i == 0 else "secondary")
            b.clicked.connect(lambda _=False, o=opt: self._answer(o, "answer"))
            w = b.sizeHint().width()
            if row is None or used + 8 + w > width:
                row = QHBoxLayout()
                row.setSpacing(8)
                row.setContentsMargins(0, 0, 0, 0)
                self.pill_box.addLayout(row)
                used = 0
            row.addWidget(b)
            used += w + (8 if used else 0)
            self.pills.append(b)
        for r in range(self.pill_box.count()):
            self.pill_box.itemAt(r).layout().addStretch(1)
        lay = self.card.layout()
        self.sp_question.changeSize(0, 14 if self.options else 4)
        self.sp_text.changeSize(0, 10 if self.options and allow_text else 0)
        self.text_row.setVisible(allow_text)
        self.text.clear()
        self.send.kind = "secondary" if self.options else "primary"
        self._sync_send()
        n = len(self.options)
        keys = ("Enter to send · " if allow_text else (f"1–{n} to pick · " if n > 1 else "Enter to pick · "))
        self.hint.setText(keys + "Esc to skip")
        self.allow_text = allow_text
        lay.invalidate()

    def _sync_send(self, *_):
        has = bool(self.text.text().strip())
        self.send.setEnabled(has)
        if self.options:
            self.send.kind = "primary" if has else "secondary"
        self.send.update()

    def focus_input(self):
        if self.allow_text:
            self.text.setFocus(Qt.OtherFocusReason)
        elif self.pills:
            self.pills[0].setFocus(Qt.OtherFocusReason)

    def keyPressEvent(self, e):
        k = e.key() - Qt.Key_1
        if not self.allow_text and 0 <= k < min(9, len(self.pills)) and not e.modifiers():
            self.pills[k].animateClick()
            return
        super().keyPressEvent(e)

    def _send(self):
        t = self.text.text().strip()
        if t:
            self._answer(t, "answer")

    def _answer(self, text, reason):
        if self.closing or not self.isVisible():          # one answer only
            return
        if self.on_answer:
            self.on_answer(text, reason)


# ---- the handover chip ----------------------------------------------------------------------------
class HandoverChip(Floating):
    def __init__(self):
        super().__init__(radius=22, focusable=False, scale_from=0.96, rise=10)
        self.title = QLabel()
        self.title.setStyleSheet("color: white; font: 600 13px 'Segoe UI';")
        self.detail = QLabel()
        self.detail.setStyleSheet("color: #C9BDE0; font: 13px 'Segoe UI';")
        self.dots = [QLabel("·") for _ in range(2)]
        for d in self.dots:
            d.setStyleSheet("color: #6E5F8C; font: 700 14px 'Segoe UI';")
        self.rec = RecDot()
        self.done_btn = Pill("I'm done", "primary", small=True, focusable=False)
        self.stop_btn = Pill("Stop agent", "danger", small=True, focusable=False)
        lay = QHBoxLayout(self.card)
        lay.setContentsMargins(16, 7, 7, 7)
        lay.setSpacing(9)
        lay.addWidget(BuddyBadge())
        for w in (self.title, self.dots[0], self.detail, self.dots[1], self.rec):
            lay.addWidget(w)
        lay.addSpacing(2)
        lay.addWidget(self.done_btn)
        lay.addWidget(self.stop_btn)
        self._title = self._detail = ""
        self.origin = QPointF(0.5, 0.0)

    def card_size(self):
        return QSize(self.card.layout().totalSizeHint().width(), 44)

    def load(self, title, detail):
        self._title, self._detail = title, detail
        self.done_btn.setText("I'm done")
        self.done_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self._fill()

    def set_detail(self, text):
        self._detail = text
        self._fill()
        if self.isVisible() and self.snap is None:
            self.recenter()

    def recenter(self):                                         # keep it centred as its width changes
        c = self.geometry().center().x()
        self.fit()
        self.move(c - self.width() // 2, self.y())

    def _settled(self):
        super()._settled()
        if self.isVisible():
            self.recenter()

    def mark_done(self):
        self.done_btn.setText("Got it ✓")
        self.done_btn.setEnabled(False)

    def _fill(self):
        fm = QFontMetrics(self.detail.font())
        self.title.setText(QFontMetrics(self.title.font()).elidedText(self._title, Qt.ElideRight, 220))
        self.detail.setText(fm.elidedText(self._detail, Qt.ElideRight, 380))
        self.detail.setVisible(bool(self._detail))
        self.dots[0].setVisible(bool(self._detail))
        self.card.layout().invalidate()

    def present_on(self, screen):
        self.fit()
        g = screen.availableGeometry()
        w = self.width() - 2 * PAD
        super().present(QPoint(g.center().x() - w // 2, g.top() + 12))


# ---- the public API -------------------------------------------------------------------------------
class _Ask:
    def __init__(self, question, options, allow_text, mood):
        self.question, self.options, self.allow_text, self.mood = question, options, allow_text, mood
        self.done = threading.Event()
        self.answer = self.reason = None
        self._lock = threading.Lock()

    def resolve(self, answer, reason):
        """First resolution wins (a click racing a timeout can't produce two answers)."""
        with self._lock:
            if self.done.is_set():
                return False
            self.answer, self.reason = answer, reason
            self.done.set()
            return True


class HumanUI(QObject):
    """Question card + handover chip around the buddy cursor. Create in the Qt thread; every public
    method may be called from the agent's worker thread."""
    _call_sig = Signal(str, object)
    HANDOVER_CALL_MS = 3000                         # hop for attention this long before parking

    def __init__(self, cursor):
        super().__init__()
        self.cur = cursor
        self.done_clicked = threading.Event()
        self.stop_clicked = threading.Event()
        self.on_stop = []                           # callables run (Qt thread) when the user clicks Stop agent
        self.should_stop = None                     # optional fn() -> bool polled while ask() waits
        self.reduced = bool(getattr(cursor, "reduced", False)) or reduced_motion()
        self.popup = AskPopup()
        self.chip = HandoverChip()
        for w in (self.popup, self.chip):
            w.reduced = self.reduced
        self.popup.on_answer = self._on_popup_answer
        self.popup.stop_btn.clicked.connect(self._on_stop)
        self.chip.done_btn.clicked.connect(self._on_chip_done)
        self.chip.stop_btn.clicked.connect(self._on_stop)
        self._req = None
        self._ask_lock = threading.Lock()
        self._parked = False
        self._park_text = ""
        self._park_timer = QTimer(self)
        self._park_timer.setSingleShot(True)
        self._park_timer.timeout.connect(self._do_park)
        self._call_sig.connect(self._dispatch)
        sig = getattr(cursor, "parked_clicked", None)
        if sig is not None:
            sig.connect(self._on_parked_click)

    # ---- thread plumbing ----
    def _on_qt_thread(self):
        return QThread.currentThread() is self.thread()

    def _call(self, name, *args):
        if self._on_qt_thread():
            getattr(self, name)(*args)
        else:
            self._call_sig.emit(name, args)          # queued onto the Qt thread

    def _dispatch(self, name, args):
        getattr(self, name)(*args)

    def _post(self, *a):
        self.cur.post(*a)

    def _stopping(self):
        try:
            return self.stop_clicked.is_set() or bool(self.should_stop and self.should_stop())
        except Exception:  # noqa: BLE001
            return False

    # ---- public API (thread-safe) ----
    def ask(self, question, options=None, allow_text=True, timeout_s=300, mood="calling"):
        """Show the question card next to the buddy and block until answered.
        Returns the clicked option / typed text, or None on Esc, timeout or stop."""
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        req = _Ask(str(question).strip(), opts, bool(allow_text) or not opts, mood or "calling")
        if self._stopping():
            return None
        deadline = time.monotonic() + timeout_s if timeout_s else float("inf")
        if self._on_qt_thread():                     # e.g. called from a Qt slot: spin a local loop
            loop = QEventLoop()
            poll = QTimer()
            poll.timeout.connect(lambda: self._poll(req, deadline) and loop.quit())
            poll.start(50)
            self._show_ask(req)
            loop.exec()
            poll.stop()
            return req.answer
        with self._ask_lock:                         # one question at a time
            if self._stopping():
                return None
            self._call_sig.emit("_show_ask", (req,))
            while not req.done.wait(0.05):
                self._poll(req, deadline)
            return req.answer

    def answer(self, text):
        """Answer the open question programmatically (e.g. voice input later)."""
        self._call("_answer_current", text)

    def handover(self, title, detail, park_text="Your turn — click me when done"):
        self.done_clicked.clear()                    # done for *this* handover only
        self._call("_handover", title, detail, park_text)

    def status(self, text):
        self._call("_status", text)

    def end_handover(self, thanks=True):
        self._call("_end_handover", thanks, None)

    def stuck(self, text):
        self._post("mood", "stuck", text)

    def reset(self):
        self.done_clicked.clear()
        self.stop_clicked.clear()

    def dismiss_all(self):
        """Close any open question (-> None) and the chip, unpark the buddy. Safe to call any time."""
        self._call("_dismiss_all")

    # ---- Qt-thread side ----
    def _poll(self, req, deadline):
        if req.done.is_set():
            return True
        reason = "stopped" if self._stopping() else "timeout" if time.monotonic() >= deadline else None
        if reason and req.resolve(None, reason):
            self._call("_close_ask", req)
        return bool(reason)

    def _show_ask(self, req):
        if req.done.is_set():                        # timed out / stopped before we got here
            return
        if self._req is not None and self._req is not req and self._req.resolve(None, "skip"):
            self._close_ask(self._req)
        self._req = req
        p = self.popup
        p.load(req.question, req.options, req.allow_text)
        p.fit()
        tl, p.origin = self._place_near_buddy(QSize(p.width() - 2 * PAD, p.height() - 2 * PAD))
        p.present(tl)
        p.prev_fg = take_focus(p)
        p.focus_input()
        QTimer.singleShot(0, p.focus_input)          # again once the window is really active
        self._post("mood", req.mood, shorten(req.question))

    def _place_near_buddy(self, size):
        """Card top-left (global) beside the buddy, clamped to the buddy's screen, plus the corner
        of the card nearest the buddy (where the scale animation grows from)."""
        pos = getattr(self.cur, "pos_", None)
        tip = QPoint(int(pos.x()), int(pos.y())) if pos is not None else QCursor.pos()
        scr = QGuiApplication.screenAt(tip) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry().adjusted(12, 12, -12, -12)
        w, h = size.width(), size.height()
        below, above = tip.y() + 66, tip.y() - 44 - h        # clear of the hops (up) and the body (down)
        y, oy = (below, 0.0) if below + h <= g.bottom() or below + h - g.bottom() < g.top() - above else (above, 1.0)
        x, ox = tip.x() - 8, 0.06
        if x + w > g.right():                                    # no room on the right: hang to the left
            x, ox = tip.x() + 56 - w, 0.94
        x = max(g.left(), min(x, g.right() - w))
        y = max(g.top(), min(y, g.bottom() - h))
        return QPoint(x, y), QPointF(ox, oy)

    def _on_popup_answer(self, text, reason):
        req = self._req
        if req is not None and req.resolve(text, reason):
            self._close_ask(req)

    def _answer_current(self, text):
        if self._req is not None and self.popup.isVisible():
            self.popup._answer(text, "answer" if text is not None else "skip")

    def _close_ask(self, req):
        if self._req is not req:
            return
        self._req = None
        give_back_focus(self.popup, self.popup.prev_fg)
        self.popup.prev_fg = None
        self.popup.dismiss()
        if not self._parked:
            self._post("mood", "normal")
            self._post("say", ANSWER_SAY.get(req.reason, "Okay"), False)

    def _handover(self, title, detail, park_text):
        self.chip.load(title, detail)
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()   # where the buddy parks
        self.chip.present_on(scr)
        self._park_text = park_text
        self._post("mood", "calling", shorten(title, 28) if title else None)
        self._park_timer.start(self.HANDOVER_CALL_MS)

    def _do_park(self):
        self._parked = True
        self._post("park", self._park_text)

    def _status(self, text):
        if self.chip.isVisible() and not self.chip.closing:
            self.chip.set_detail(text)
        self._post("say", text, False)

    def _end_handover(self, thanks, say):
        was_calling = self._park_timer.isActive()
        self._park_timer.stop()
        self.chip.dismiss()
        if self._parked:
            self._parked = False
            self._post("unpark")
            delay = 850                               # let it morph back before the happy spin
        else:
            delay = 0
        if thanks:
            QTimer.singleShot(delay, lambda: self._post("mood", "thanks"))
        elif was_calling or delay or say:
            self._post("mood", "normal")
            if say:
                QTimer.singleShot(delay, lambda: self._post("say", say, False))

    def _on_chip_done(self):
        self.done_clicked.set()
        self.chip.mark_done()
        if self._park_timer.isActive():               # still hopping: don't bother parking now
            self._park_timer.stop()
            self._post("mood", "normal")
        elif self._parked:
            self._parked = False
            self._post("unpark")

    def _on_parked_click(self):
        self._parked = False                          # the drop morphs back by itself
        self.done_clicked.set()
        self.chip.mark_done()

    def _on_stop(self):
        self.stop_clicked.set()
        for fn in list(self.on_stop):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print("hitl_ui on_stop:", e)
        self._dismiss_all(say="Stopped")

    def _dismiss_all(self, say=None):
        if self._req is not None and self._req.resolve(None, "stopped"):
            self._close_ask(self._req)
        if self.chip.isVisible() or self._parked or self._park_timer.isActive():
            self._end_handover(False, say)
        elif say:
            self._post("say", say, False)
