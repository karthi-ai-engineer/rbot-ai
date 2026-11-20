"""Buddy Hub, the composite views: running card, needs-you card, queue + done lists, chat, settings drawer,
title bar, the window shell and the mini-bar. They only show state and emit signals; app.py decides."""
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget)

from rbot.hub import core
from rbot.hub.widgets import (CTX, IC, KIND_ICON, METHOD_ICON, SKINS, Badge, Button, BuddyFace, Card, ChatInput, Elided,
                    EmptyState, Glyph, IconCircle, PulseDot, SectionHeader, Segmented, SendButton, Toggle,
                    col, ease_out, font, fpx, grad, label, paint_face, paint_shadow, px, reveal, scroll_qss,
                    scroller, set_color)

LEVELS = {1: ("L1", "Quick look"), 2: ("L2", "Thinking harder"), 3: ("L3", "Strongest model")}


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())


def _left(w):
    box = QWidget()
    l = QHBoxLayout(box)
    l.setContentsMargins(0, 0, 0, 0)
    l.addWidget(w)
    l.addStretch(1)
    return box


def keep_gutter(area, lay, right):
    """Keep the same right edge whether or not the scrollbar shows (it sits inside the gutter)."""
    sb = area.verticalScrollBar()

    def sync(_a=0, mx=None):
        mx = sb.maximum() if mx is None else mx
        m = lay.contentsMargins()
        r = max(0, px(right) - (sb.sizeHint().width() if mx > 0 else 0))
        if m.right() != r:
            lay.setContentsMargins(m.left(), m.top(), r, m.bottom())
    sb.rangeChanged.connect(sync)
    sync()


def hline():
    w = QWidget()
    w.setFixedHeight(1)
    w.setStyleSheet(f"background:{CTX.t['border']};")
    return w


# ---------------------------------------------------------------- running
class RunningCard(Card):
    stop = Signal()

    def __init__(self):
        super().__init__()
        top = QHBoxLayout()
        top.setSpacing(px(6))
        self.dot = PulseDot(9, 16)
        self.dot.set_state("running")
        self.state = label("Working", "cap_b", "accent_text")
        self.elapsed = label("0:00", "cap", "text3")
        self.counter = label("", "cap", "text3")
        top.addWidget(self.dot)
        top.addWidget(self.state)
        top.addWidget(label("·", "cap", "text3"))
        top.addWidget(self.elapsed)
        top.addStretch(1)
        top.addWidget(self.counter)
        self.title = label("", "head", "text", wrap=True)
        self.step = label("", "body", "text2", wrap=True)
        self.detail = Elided("", "mono_s", "text3")
        bottom = QHBoxLayout()
        bottom.setSpacing(px(6))
        self.method = Badge("", None, "neutral")
        self.level = Badge("L1", None, "neutral")
        self.stop_btn = Button("Stop", "danger", icon="stop", small=True, tip="Stop this task")
        self.stop_btn.clicked.connect(self.stop)
        bottom.addWidget(self.method)
        bottom.addWidget(self.level)
        bottom.addStretch(1)
        bottom.addWidget(self.stop_btn)
        self.bottom = QWidget()
        self.bottom.setLayout(bottom)
        bottom.setContentsMargins(0, 0, 0, 0)
        self.lay.addLayout(top)
        self.lay.addWidget(self.title)
        self.lay.addWidget(self.step)
        self.lay.addWidget(self.detail)
        self.lay.addSpacing(px(2))
        self.lay.addWidget(self.bottom)
        self.t_start = time.time()

    def show_task(self, t, needs=None):
        self.t_start = t.t_start or time.time()
        title = t.text if len(t.text) <= 110 else t.text[:108].rstrip() + "…"
        self.title.setText(title)
        if needs:
            self.dot.set_state("needs")
            self.state.setText("Waiting for you")
            set_color(self.state, "warn")
        else:
            self.dot.set_state("running")
            self.state.setText("Working")
            set_color(self.state, "accent_text")
        self.counter.setText(f"Step {max(t.step, 1)} of {t.max_steps}")
        self.step.setText(t.step_text or "Getting started…")
        self.detail.setText(t.step_detail)
        self.detail.setVisible(bool(t.step_detail) and not needs)
        self.step.setVisible(not needs)              # compact while the human has the turn:
        self.bottom.setVisible(not needs)            # the needs-you card above carries Stop
        if t.method in METHOD_ICON:
            self.method.set(core.METHODS[t.method], METHOD_ICON[t.method])
            self.method.show()
        else:
            self.method.hide()
        short, tip = LEVELS.get(t.level, (f"L{t.level}", ""))
        self.level.set(short, None, "accent" if t.level >= 2 else "neutral")
        self.level.setToolTip(tip)
        self.tick()

    def tick(self):
        self.elapsed.setText(core.clock(time.time() - self.t_start))


# ---------------------------------------------------------------- needs you
class NeedsCard(Card):
    go_there = Signal()
    done = Signal()
    stop = Signal()
    answer = Signal(str)

    def __init__(self):
        super().__init__(accent="warn")
        self.lay.setContentsMargins(px(18), px(14), px(16), px(14))
        head = QHBoxLayout()
        head.setSpacing(px(12))
        self.icon = IconCircle("key", "warn", 36)
        head.addWidget(self.icon, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(px(2))
        self.title = label("", "head", "text", wrap=True)
        self.detail = label("", "body", "text2", wrap=True)
        self.status = label("", "cap", "warn")
        text.addWidget(self.title)
        text.addWidget(self.detail)
        text.addWidget(self.status)
        head.addLayout(text, 1)
        self.opts = QVBoxLayout()
        self.opts.setSpacing(px(8))
        self.hint = label("Or type your answer in the box below.", "cap", "text3")
        acts = QHBoxLayout()
        acts.setSpacing(px(8))
        self.go_btn = Button("Go there", "secondary", icon="open", small=True,
                             tip="Show me the window where I need you")
        self.done_btn = Button("I'm done", "primary", icon="check", small=True, tip="Tell rbot-ai you finished")
        self.stop_btn = Button("Stop", "ghost", icon="stop", small=True, tip="Stop this task")
        self.go_btn.clicked.connect(self.go_there)
        self.done_btn.clicked.connect(self.done)
        self.stop_btn.clicked.connect(self.stop)
        acts.addWidget(self.go_btn)
        acts.addWidget(self.done_btn)
        acts.addStretch(1)
        acts.addWidget(self.stop_btn)
        self.lay.addLayout(head)
        self.lay.addLayout(self.opts)
        self.lay.addWidget(self.hint)
        self.lay.addSpacing(px(2))
        self.lay.addLayout(acts)
        self.key = None

    def show_needs(self, n):
        self.icon.icon = KIND_ICON.get(n["kind"], "chat")
        self.icon.update()
        self.title.setText(n["title"])
        self.detail.setText(n.get("detail") or "")
        self.detail.setVisible(bool(n.get("detail")))
        self.status.setText(n.get("status") or "")
        self.status.setVisible(bool(n.get("status")))
        key = (n["title"], tuple(n["options"]))
        if key != self.key:
            self.key = key
            clear_layout(self.opts)
            row, used, width = None, 0, px(340)
            for i, o in enumerate(n["options"]):
                b = Button(o, "primary" if i == 0 else "secondary", small=True)
                b.clicked.connect(lambda _=False, o=o: self.answer.emit(o))
                w = b.sizeHint().width()
                if row is None or used + w > width:
                    row = QHBoxLayout()
                    row.setSpacing(px(8))
                    self.opts.addLayout(row)
                    used = 0
                row.addWidget(b)
                used += w + px(8)
            for r in range(self.opts.count()):
                self.opts.itemAt(r).layout().addStretch(1)
        asking = bool(n["options"]) or n.get("allow_text")
        self.hint.setVisible(bool(n.get("allow_text")))
        self.hint.setText("Or type your answer in the box below." if n["options"] else
                          "Type your answer in the box below.")
        self.done_btn.setVisible(not asking)


# ---------------------------------------------------------------- queue + done
class QueueRow(QWidget):
    remove = Signal(int)

    def __init__(self, task, n):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(12), px(6), px(6), px(6))
        lay.setSpacing(px(10))
        num = label(str(n), "cap_b", "text3")
        num.setFixedWidth(px(16))
        num.setAlignment(Qt.AlignCenter)
        lay.addWidget(num)
        lay.addWidget(Elided(task.text, "body", "text"), 1)
        b = Button("", "icon", icon="remove", small=True, tip="Remove from the queue")
        b.setAccessibleName(f"Remove {task.text[:40]}")
        b.clicked.connect(lambda: self.remove.emit(task.id))
        lay.addWidget(b)


class DoneRow(QWidget):
    open_video = Signal(str)
    open_folder = Signal(str)

    def __init__(self, t):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(12), px(10), px(8), px(10))
        lay.setSpacing(px(12))
        icon = {"ok": "check", "bad": "cross"}.get(t.tone, "stopped")
        lay.addWidget(IconCircle(icon, t.tone, 28), 0, Qt.AlignTop)
        mid = QVBoxLayout()
        mid.setSpacing(px(2))
        mid.addWidget(Elided(t.text, "body_b", "text"))
        if t.summary:
            s = t.summary if len(t.summary) <= 150 else t.summary[:148].rstrip() + "…"
            mid.addWidget(label(s, "cap", "text2", wrap=True))
        when = time.strftime("%H:%M", time.localtime(t.t_end)) if t.t_end else ""
        bits = [t.result_label, f"{t.steps} step{'s' if t.steps != 1 else ''}", core.duration(t.seconds), when]
        meta = label("  ·  ".join(b for b in bits if b), "cap", "text3")
        mid.addWidget(meta)
        lay.addLayout(mid, 1)
        btns = QHBoxLayout()
        btns.setSpacing(px(2))
        if t.video:
            v = Button("", "icon", icon="play", small=True, tip="Watch the video")
            v.clicked.connect(lambda: self.open_video.emit(t.video))
            btns.addWidget(v)
        if t.run_dir:
            f = Button("", "icon", icon="folder", small=True, tip="Open the log folder")
            f.clicked.connect(lambda: self.open_folder.emit(t.run_dir))
            btns.addWidget(f)
        lay.addLayout(btns)
        lay.setAlignment(btns, Qt.AlignTop)


class ListCard(Card):
    """A card holding rows separated by hairlines."""
    def __init__(self):
        super().__init__(pad=0)
        self.lay.setContentsMargins(0, px(2), 0, px(2))
        self.lay.setSpacing(0)

    def set_rows(self, rows):
        clear_layout(self.lay)
        for i, r in enumerate(rows):
            if i:
                sep = QWidget()
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"background:{CTX.t['border']}; margin: 0 {px(12)}px;")
                self.lay.addWidget(sep)
            self.lay.addWidget(r)


# ---------------------------------------------------------------- chat
class Bubble(QWidget):
    def __init__(self, who, text):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(px(8))
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.PlainText)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setFont(font("body"))
        body.setMaximumWidth(px(300))
        body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        if who == "user":
            body.setStyleSheet(f"background:{CTX.t['user_bubble']}; color:{CTX.t['text']};"
                               f"border-radius:{px(12)}px; padding:{px(7)}px {px(12)}px;")
            lay.addStretch(1)
            lay.addWidget(body)
        else:
            body.setStyleSheet(f"background:{CTX.t['surface2']}; color:{CTX.t['text']};"
                               f"border-radius:{px(12)}px; padding:{px(7)}px {px(12)}px;")
            face = BuddyFace(22)
            lay.addWidget(face, 0, Qt.AlignBottom)
            lay.addWidget(body)
            lay.addStretch(1)


class ChatView(QWidget):
    def __init__(self):
        super().__init__()
        self.inner = QWidget()
        self.col = QVBoxLayout(self.inner)
        self.col.setContentsMargins(px(16), px(10), px(16), px(10))
        self.col.setSpacing(px(8))
        self.col.addStretch(1)
        self.sa = scroller(self.inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.sa)
        self.sa.verticalScrollBar().rangeChanged.connect(lambda _a, b: self.sa.verticalScrollBar().setValue(b))
        self.msgs = []
        keep_gutter(self.sa, self.col, 16)
        self.fade_top = FadeEdge(self.sa, top=True)

    @property
    def count(self):
        return len(self.msgs)

    def add(self, who, text, animate=True):
        b = Bubble(who, text)
        self.col.addWidget(b)
        self.msgs.append((who, text))
        if animate:
            reveal(b, 180)

    def reset(self, messages):
        while self.col.count() > 1:
            w = self.col.takeAt(1).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.msgs = []
        for who, text in messages[-60:]:
            self.add(who, text, animate=False)


class Composer(QWidget):
    submit = Signal(str)

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(16), px(8), px(16), px(16))
        lay.setSpacing(px(8))
        self.input = ChatInput()
        self.input.setAccessibleName("Task or answer")
        self.send = SendButton()
        lay.addWidget(self.input, 1)
        lay.addWidget(self.send)
        self.input.returnPressed.connect(self._go)
        self.send.clicked.connect(self._go)

    def _go(self):
        t = self.input.text().strip()
        if t:
            self.input.clear()
            self.submit.emit(t)

    def set_mode(self, answering, running):
        self.input.setPlaceholderText("Type your answer…" if answering else
                                      "Add a task to the queue…" if running else "Type a task…")


# ---------------------------------------------------------------- settings drawer
class SkinPicker(QWidget):
    changed = Signal(str)

    def __init__(self, value):
        super().__init__()
        self.value = value
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(px(8))
        self.btns = {}
        for name in SKINS:
            b = _Swatch(name, name == value)
            b.clicked.connect(lambda _=False, n=name: self._pick(n))
            lay.addWidget(b)
            self.btns[name] = b

    def _pick(self, n):
        self.value = n
        for k, b in self.btns.items():
            b.selected = k == n
            b.update()
        self.changed.emit(n)


class _Swatch(Button):
    def __init__(self, name, selected):
        super().__init__("", "ghost", tip=name.capitalize())
        self.name, self.selected = name, selected
        self.setAccessibleName(f"{name} skin")

    def sizeHint(self):
        return QSize(px(34), px(34))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(fpx(3), fpx(3), -fpx(3), -fpx(3))
        c1, c2 = (QColor(c) for c in SKINS[self.name])
        if self.selected or self.anim["hover"]:
            ring = QColor(CTX.t["text"])
            ring.setAlphaF(1.0 if self.selected else 0.3 * self.anim["hover"])
            p.setPen(QPen(ring, fpx(2)))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r.adjusted(-fpx(1.5), -fpx(1.5), fpx(1.5), fpx(1.5)))
        paint_face(p, r.adjusted(fpx(1.5), fpx(1.5), -fpx(1.5), -fpx(1.5)), c1, c2, ring=False)


class TeamsPeople(QWidget):
    changed = Signal(list)

    def __init__(self, people):
        super().__init__()
        self.people = list(people)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(px(8))
        self.chips = QVBoxLayout()
        self.chips.setSpacing(px(6))
        add = QHBoxLayout()
        add.setSpacing(px(8))
        self.input = ChatInput()
        self.input.setFixedHeight(px(32))
        self.input.setPlaceholderText("Add a name, e.g. Tharun K")
        self.add_btn = Button("Add", "secondary", icon="add", small=True)
        add.addWidget(self.input, 1)
        add.addWidget(self.add_btn)
        self.lay.addLayout(self.chips)
        self.lay.addLayout(add)
        self.input.returnPressed.connect(self._add)
        self.add_btn.clicked.connect(self._add)
        self._fill()

    def _add(self):
        n = " ".join(self.input.text().split())
        if n and n.lower() not in (p.lower() for p in self.people):
            self.people.append(n)
            self.changed.emit(list(self.people))
            self._fill()
        self.input.clear()

    def _remove(self, n):
        self.people = [p for p in self.people if p != n]
        self.changed.emit(list(self.people))
        self._fill()

    def _fill(self):
        clear_layout(self.chips)
        if not self.people:
            self.chips.addWidget(label("Nobody yet: I'll ask before messaging anyone but you.", "cap", "text3"))
            return
        row, used, width = None, 0, px(330)
        for n in self.people:
            chip = _PersonChip(n)
            chip.remove.connect(self._remove)
            w = chip.sizeHint().width()
            if row is None or used + w > width:
                row = QHBoxLayout()
                row.setSpacing(px(6))
                self.chips.addLayout(row)
                used = 0
            row.addWidget(chip)
            used += w + px(6)
        for r in range(self.chips.count()):
            it = self.chips.itemAt(r)
            if it.layout():
                it.layout().addStretch(1)


class _PersonChip(QWidget):
    remove = Signal(str)

    def __init__(self, name):
        super().__init__()
        self.name = name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(10), 0, px(2), 0)
        lay.setSpacing(px(2))
        lay.addWidget(label(name, "cap_b", "text"))
        b = Button("", "icon", icon="remove", small=True, tip=f"Remove {name}")
        b.setFixedSize(px(22), px(22))
        b.clicked.connect(lambda: self.remove.emit(self.name))
        lay.addWidget(b)
        self.setFixedHeight(px(26))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, col("surface2"))
        p.strokePath(path, QPen(col("border2"), 1))


class SettingRow(QWidget):
    def __init__(self, title, desc, control=None, below=None):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(px(16), px(12), px(16), px(12))
        outer.setSpacing(px(10))
        lay = QHBoxLayout()
        lay.setSpacing(px(16))
        text = QVBoxLayout()
        text.setSpacing(px(2))
        text.addWidget(label(title, "body", "text"))
        if desc:
            text.addWidget(label(desc, "cap", "text3", wrap=True))
        lay.addLayout(text, 1)
        if control is not None:
            lay.addWidget(control, 0, Qt.AlignVCenter)
        outer.addLayout(lay)
        if below is not None:
            outer.addWidget(below)


class SettingsPanel(QWidget):
    changed = Signal(str, object)
    autostart = Signal(bool)
    back = Signal()

    def __init__(self, settings, autostart_on, hotkey_text):
        super().__init__()
        self.setAutoFillBackground(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(px(12), px(4), px(16), px(8))
        head.setSpacing(px(8))
        back = Button("", "icon", icon="back", tip="Back (Esc)")
        back.clicked.connect(self.back)
        head.addWidget(back)
        head.addWidget(label("Settings", "title", "text"))
        head.addStretch(1)
        lay.addLayout(head)
        inner = QWidget()
        col_ = QVBoxLayout(inner)
        col_.setContentsMargins(px(16), px(4), px(12), px(16))
        col_.setSpacing(px(8))
        s = settings

        def group(title, rows):
            col_.addSpacing(px(8))
            h = label(title, "cap_b", "text2")
            h.setContentsMargins(px(4), 0, 0, 0)
            col_.addWidget(h)
            card = Card(pad=0)
            card.lay.setContentsMargins(0, 0, 0, 0)
            card.lay.setSpacing(0)
            for i, r in enumerate(rows):
                if i:
                    sep = QWidget()
                    sep.setFixedHeight(1)
                    sep.setStyleSheet(f"background:{CTX.t['border']};")
                    card.lay.addWidget(sep)
                card.lay.addWidget(r)
            col_.addWidget(card)

        self.mode = Segmented([("live", "Live", True), ("background", "Background", True)], s["mode"])
        self.mode.changed.connect(lambda v: self.changed.emit("mode", v))
        idle = Segmented([(4, "4 s", True), (6, "6 s", True), (10, "10 s", True)], s["idle_seconds"])
        idle.changed.connect(lambda v: self.changed.emit("idle_seconds", v))
        group("How I work", [
            SettingRow("Mode", "Live: I work on your screen while you watch. Background mode, where you keep "
                               "using your PC: web tasks run inside the rbot-ai browser, other steps borrow the mouse when you pause.", None, _left(self.mode)),
            SettingRow("Using your mouse", "When a step needs the real mouse and you've been idle this long, "
                       "I go ahead after a 3 s notice. If you're busy, I ask first.", None, _left(idle)),
        ])
        skin = SkinPicker(s["skin"])
        skin.changed.connect(lambda v: self.changed.emit("skin", v))
        theme = Segmented([("dark", "Dark", True), ("light", "Light", True)], s["theme"])
        theme.changed.connect(lambda v: self.changed.emit("theme", v))
        group("Look", [SettingRow("Cursor colour", "Grape, mint or sunset.", skin),
                       SettingRow("Theme", "", theme)])
        rec = Toggle(s["record_video"])
        rec.toggled.connect(lambda v: self.changed.emit("record_video", v))
        shots = Toggle(s["keep_screenshots"])
        shots.toggled.connect(lambda v: self.changed.emit("keep_screenshots", v))
        group("Recording", [
            SettingRow("Record a video of each task", "Waiting parts are sped up. Videos never include this "
                       "window.", rec),
            SettingRow("Keep step screenshots", "Off: screenshots are deleted when a task ends. Logs and "
                       "videos are kept.", shots),
        ])
        people = TeamsPeople(s["teams_allowed"])
        people.changed.connect(lambda v: self.changed.emit("teams_allowed", v))
        group("Teams", [SettingRow("People I may message", "Messages to anyone else, and every call, need "
                                   "your OK first.", None, people)])
        auto = Toggle(autostart_on)
        auto.toggled.connect(self.autostart)
        self.auto = auto
        group("Startup", [SettingRow("Start with Windows", "Opens quietly in the tray when you sign in.", auto)])
        foot = label(f"Show or hide the Hub: {hotkey_text}\nEmergency stop, any time: Ctrl+Alt+Q", "cap", "text3")
        foot.setContentsMargins(px(4), px(12), 0, 0)
        col_.addWidget(foot)
        col_.addStretch(1)
        self.sa = scroller(inner)
        lay.addWidget(self.sa, 1)

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), col("bg"))


# ---------------------------------------------------------------- title bar
class TitleBar(QWidget):
    settings = Signal()
    minimize = Signal()
    close = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedHeight(px(52))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(px(16), 0, px(8), 0)
        lay.setSpacing(px(8))
        lay.addWidget(BuddyFace(24))
        t = label("rbot-ai", "head", "text")
        t.setFont(font("head", weight=600))
        lay.addWidget(t)
        lay.addSpacing(px(2))
        self.status = Badge("Idle", None, "neutral")
        lay.addWidget(self.status)
        lay.addStretch(1)
        for icon, sig, tip in (("settings", self.settings, "Settings"), ("minimize", self.minimize, "Minimize"),
                               ("close", self.close, "Hide to tray (keeps running)")):
            b = Button("", "icon", icon=icon, tip=tip)
            b.clicked.connect(sig)
            lay.addWidget(b)

    def set_status(self, text, tone):
        self.status.set(text, None, tone)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.window().windowHandle() is not None:
            self.window().windowHandle().startSystemMove()


# ---------------------------------------------------------------- the window shell
SHADOW = 18                         # room around the panel for its soft shadow (logical px)


class Shell(QWidget):
    """Frameless, translucent top-level: paints a 12 px rounded panel + soft shadow; content goes in .panel."""

    def __init__(self, flags, radius=12, shadow=SHADOW):
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.radius, self.shadow_px = radius, shadow
        self.panel = QWidget(self)
        self.panel.setAttribute(Qt.WA_TranslucentBackground)

    def margin(self):
        return px(self.shadow_px)

    def resizeEvent(self, e):
        m = self.margin()
        self.panel.setGeometry(m, m, self.width() - 2 * m, self.height() - 2 * m)
        super().resizeEvent(e)

    def panel_rect(self):
        m = self.margin()
        return QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.panel_rect()
        rad = min(fpx(self.radius), r.height() / 2)
        paint_shadow(p, r, rad, layers=9, spread=1.9, drop=4)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        p.fillPath(path, col("bg"))
        bd = QColor(255, 255, 255, 22) if CTX.t["dark"] else QColor(0, 0, 0, 30)
        p.strokePath(path, QPen(bd, 1))


class FadeEdge(QWidget):
    """Soft fade at the bottom of a scroll area while more content sits below the fold."""
    def __init__(self, area, top=False):
        super().__init__(area)
        self.area, self.top = area, top
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        sb = area.verticalScrollBar()
        sb.valueChanged.connect(self.sync)
        sb.rangeChanged.connect(self.sync)
        area.installEventFilter(self)
        self.sync()

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Resize:
            self.sync()
        return False

    def sync(self, *_):
        sb, h = self.area.verticalScrollBar(), px(28)
        vw = self.area.viewport().width()
        self.setGeometry(0, 0 if self.top else self.area.height() - h, vw, h)
        more = sb.value() > 0 if self.top else sb.value() < sb.maximum()
        self.setVisible(sb.maximum() > 0 and more)
        self.raise_()

    def paintEvent(self, _):
        from PySide6.QtGui import QLinearGradient
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(1 if self.top else 0, col("bg", 0.0))
        g.setColorAt(0 if self.top else 1, col("bg", 0.95))
        p.fillRect(self.rect(), g)


class HubWindow(Shell):
    """The full Hub. Build once per theme/scale (rebuild() on change); app.py feeds it state."""
    W, H = 420, 720
    submit = Signal(str)
    stop = Signal()
    go_there = Signal()
    done = Signal()
    answer = Signal(str)
    remove_queued = Signal(int)
    open_video = Signal(str)
    open_folder = Signal(str)
    setting = Signal(str, object)
    autostart = Signal(bool)
    hide_requested = Signal()
    screen_scale_changed = Signal()

    def __init__(self):
        super().__init__(Qt.FramelessWindowHint | Qt.Window)
        self.setWindowTitle("rbot-ai")
        self.setMouseTracking(True)
        self.setMinimumSize(px(380) + 2 * self.margin(), px(560) + 2 * self.margin())
        self.resize(px(self.W) + 2 * self.margin(), px(self.H) + 2 * self.margin())
        self.body = None
        self.drawer = None
        self.drawer_open = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._escape)
        self.fade = QVariantAnimation(self)
        self.fade.setEasingCurve(ease_out())
        self.fade.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))
        self._after_fade = None
        self.fade.finished.connect(self._faded)

    # ---- build ----
    def build(self, settings, autostart_on, hotkey_text):
        self.setStyleSheet(scroll_qss())
        if self.panel.layout() is not None:
            old = self.panel.layout()
            clear_layout(old)
            QWidget().setLayout(old)              # reparent the old layout away
        root = QVBoxLayout(self.panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.title = TitleBar()
        self.title.settings.connect(self.open_settings)
        self.title.minimize.connect(self.showMinimized)
        self.title.close.connect(self.hide_requested)
        root.addWidget(self.title)
        self.body = QWidget()
        root.addWidget(self.body, 1)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # status column
        inner = QWidget()
        c = QVBoxLayout(inner)
        c.setContentsMargins(px(16), px(4), px(16), px(12))
        c.setSpacing(px(8))
        self.run_head = SectionHeader("Running")
        self.running = RunningCard()
        self.running.stop.connect(self.stop)
        self.idle = EmptyState("face", "Nothing running. Type a task below and I'll get going.",
                               "You can keep working; I'll ask when I need you.")
        self.needs_head = SectionHeader("Needs you")
        set_color(self.needs_head.title, "warn")
        self.needs = NeedsCard()
        self.needs.go_there.connect(self.go_there)
        self.needs.done.connect(self.done)
        self.needs.stop.connect(self.stop)
        self.needs.answer.connect(self.answer)
        self.queue_head = SectionHeader("Queued")
        self.queue = ListCard()
        self.done_head = SectionHeader("Done today")
        self.done_list = ListCard()
        self.done_empty = EmptyState("history", "Finished tasks show up here, with a video and the log.")
        for w in (self.needs_head, self.needs, self.run_head, self.running, self.idle):
            c.addWidget(w)
        c.addSpacing(px(4))
        c.addWidget(self.queue_head)
        c.addWidget(self.queue)
        c.addSpacing(px(4))
        c.addWidget(self.done_head)
        c.addWidget(self.done_list)
        c.addWidget(self.done_empty)
        c.addStretch(1)
        self.status_inner, self.status_col = inner, c
        self.status_area = scroller(inner)
        body.addWidget(self.status_area)
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.setInterval(0)
        self.fit_timer.timeout.connect(self.fit_status)
        inner.installEventFilter(self)
        keep_gutter(self.status_area, c, 16)
        self.fade_edge = FadeEdge(self.status_area)

        # chat
        div = QWidget()
        dl = QHBoxLayout(div)
        dl.setContentsMargins(px(16), px(10), px(16), 0)
        dl.setSpacing(px(8))
        dl.addWidget(label("Chat", "cap_b", "text2"))
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{CTX.t['border']};")
        dl.addWidget(line, 1)
        body.addWidget(div)
        self.chat = ChatView()
        body.addWidget(self.chat, 1)
        self.composer = Composer()
        self.composer.submit.connect(self.submit)
        body.addWidget(self.composer)

        # settings drawer (overlay, slides in from the right)
        self.drawer = SettingsPanel(settings, autostart_on, hotkey_text)
        self.drawer.setParent(self.body)
        self.drawer.hide()
        self.drawer.back.connect(self.close_settings)
        self.drawer.changed.connect(self.setting)
        self.drawer.autostart.connect(self.autostart)
        self.drawer_open = False
        self.slide = QVariantAnimation(self)
        self.slide.setEasingCurve(ease_out())
        self.slide.valueChanged.connect(self._slide_frame)
        self.slide.finished.connect(self._slide_done)
        self.timer.start()

    # ---- render state ----
    def render(self, model, parts=None, animate=True):
        parts = parts or {"running", "needs", "queue", "done", "chat"}
        busy = model.current is not None
        if "running" in parts or "needs" in parts:
            if busy:
                self.running.show_task(model.current, model.needs)
                self.running.show()
                self.idle.hide()
            else:
                self.running.hide()
                self.idle.show()
            self.title.set_status(*(("Needs you", "warn") if model.needs else ("Working", "accent") if busy
                                    else ("Idle", "neutral")))
        if "needs" in parts:
            was = self.needs.isVisible()
            if model.needs:
                self.needs.show_needs(model.needs)
                self.needs_head.show()
                if not was and animate:
                    reveal(self.needs, 200)
                elif getattr(self.needs, "_reveal", None) is None:
                    self.needs.setMaximumHeight(16777215)
                    self.needs.show()
                if not was:
                    self.status_area.verticalScrollBar().setValue(0)
            else:
                anim = getattr(self.needs, "_reveal", None)
                if anim is not None:
                    try:
                        anim.stop()
                    except RuntimeError:
                        pass
                    self.needs._reveal = None
                self.needs.setGraphicsEffect(None)
                self.needs.setMaximumHeight(16777215)
                self.needs_head.hide()
                self.needs.hide()
            self.composer.set_mode(bool(model.needs and model.needs.get("allow_text")), busy)
        if "running" in parts:
            self.composer.set_mode(bool(model.needs and model.needs.get("allow_text")), busy)
        if "queue" in parts:
            rows = []
            for i, t in enumerate(model.queue, 1):
                r = QueueRow(t, i)
                r.remove.connect(self.remove_queued)
                rows.append(r)
            self.queue.set_rows(rows)
            self.queue_head.set_count(len(model.queue))
            self.queue_head.setVisible(bool(rows))
            self.queue.setVisible(bool(rows))
        if "running" in parts and "done" not in parts:
            self.done_empty.setVisible(not model.done and not busy)
            self.done_head.setVisible(bool(model.done) or not busy)
        if "done" in parts:
            rows = []
            for t in model.done[:30]:
                r = DoneRow(t)
                r.open_video.connect(self.open_video)
                r.open_folder.connect(self.open_folder)
                rows.append(r)
            first_new = animate and rows and self.done_head.count.text() != str(len(model.done))
            self.done_list.set_rows(rows)
            self.done_head.set_count(len(model.done))
            self.done_list.setVisible(bool(rows))
            self.done_empty.setVisible(not rows and not busy)
            self.done_head.setVisible(bool(rows) or not busy)
            if first_new:
                reveal(rows[0], 200)
        if "chat" in parts:
            shown = self.chat.msgs
            if model.chat[:len(shown)] != shown or (not shown and model.chat and not animate):
                self.chat.reset(model.chat)
            else:
                for who, text in model.chat[len(shown):]:
                    self.chat.add(who, text, animate)
        self.fit_timer.start()

    def _tick(self):
        if self.running.isVisible():
            self.running.tick()

    def eventFilter(self, obj, e):
        if e.type() == QEvent.LayoutRequest and obj is getattr(self, "status_inner", None):
            self.fit_timer.start()
        return False

    def fit_status(self):
        """Status sections take what they need (up to ~2/3 of the window); the chat gets the rest."""
        if self.body is None:
            return
        w = self.status_area.viewport().width() or self.status_area.width()
        lay = self.status_col
        need = lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        cap = int((self.body.height() - self.composer.sizeHint().height()) * 0.68)
        h = max(px(96), min(need, cap))
        if self.status_area.height() != h:
            self.status_area.setFixedHeight(h)

    def resizeEvent_fit(self):
        if self.body is not None:
            self.fit_timer.start()

    # ---- settings drawer ----
    def open_settings(self):
        if self.drawer_open or self.drawer is None:
            return
        self.drawer_open = True
        self.drawer.setGeometry(self.body.rect())
        self.drawer.show()
        self.drawer.raise_()
        self._run_slide(0.0, 1.0)

    def close_settings(self):
        if not self.drawer_open:
            return
        self.drawer_open = False
        self._run_slide(1.0, 0.0)

    def _run_slide(self, a, b):
        self.slide.stop()
        self.slide.setDuration(0 if CTX.reduced else 200)
        self.slide.setStartValue(a)
        self.slide.setEndValue(b)
        self.slide.start()

    def _slide_frame(self, v):
        w = self.body.width()
        self.drawer.setGeometry(int(w * (1 - float(v))), 0, w, self.body.height())

    def _slide_done(self):
        if not self.drawer_open:
            self.drawer.hide()

    def _escape(self):
        if self.drawer_open:
            self.close_settings()
        else:
            self.hide_requested.emit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.drawer is not None and self.drawer_open:
            self.drawer.setGeometry(self.body.rect())
        self.resizeEvent_fit()

    # ---- edge resize (frameless) ----
    def _edges(self, pos):
        r, g = self.panel_rect(), fpx(8)
        edges = Qt.Edges()
        if abs(pos.x() - r.left()) < g:
            edges |= Qt.LeftEdge
        if abs(pos.x() - r.right()) < g:
            edges |= Qt.RightEdge
        if abs(pos.y() - r.top()) < g:
            edges |= Qt.TopEdge
        if abs(pos.y() - r.bottom()) < g:
            edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, e):
        edges = self._edges(e.position())
        if e.button() == Qt.LeftButton and edges and self.windowHandle() is not None:
            self.windowHandle().startSystemResize(edges)

    def mouseMoveEvent(self, e):
        E = self._edges(e.position())
        horiz, vert = bool(E & (Qt.LeftEdge | Qt.RightEdge)), bool(E & (Qt.TopEdge | Qt.BottomEdge))
        diag = (E & Qt.LeftEdge and E & Qt.TopEdge) or (E & Qt.RightEdge and E & Qt.BottomEdge)
        shape = (Qt.SizeFDiagCursor if diag else Qt.SizeBDiagCursor) if horiz and vert else             Qt.SizeHorCursor if horiz else Qt.SizeVerCursor if vert else Qt.ArrowCursor
        self.setCursor(shape)

    # ---- show / hide with a short fade ----
    def fade_in(self):
        self.setWindowOpacity(0.0 if not CTX.reduced else 1.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._fade(1.0, 180, None)

    def fade_out(self, then=None):
        if not self.isVisible():
            if then:
                then()
            return
        self._fade(0.0, 150, then)

    def _fade(self, to, ms, then):
        self.fade.stop()
        self._after_fade = then
        self.fade.setDuration(0 if CTX.reduced else ms)
        self.fade.setStartValue(float(self.windowOpacity()))
        self.fade.setEndValue(to)
        self.fade.start()

    def _faded(self):
        if self.fade.endValue() == 0.0:
            self.hide()
            self.setWindowOpacity(1.0)
        fn, self._after_fade = self._after_fade, None
        if fn:
            fn()


# ---------------------------------------------------------------- mini-bar
class MiniBar(Shell):
    """Small always-on-top pill, top-centre of the primary screen: live step + Stop. Never takes focus."""
    W, H = 360, 44
    stop = Signal()
    expand = Signal()

    def __init__(self):
        super().__init__(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus,
                         radius=22, shadow=12)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("rbot-ai mini-bar")
        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(ease_out())
        self.anim.valueChanged.connect(self._frame)
        self.anim.finished.connect(self._settled)
        self.t, self.closing, self.home = 1.0, False, QPoint(0, 0)
        self.build()

    def build(self):
        if self.panel.layout() is not None:
            old = self.panel.layout()
            clear_layout(old)
            QWidget().setLayout(old)
        self.resize(px(self.W) + 2 * self.margin(), px(self.H) + 2 * self.margin())
        lay = QHBoxLayout(self.panel)
        lay.setContentsMargins(px(14), 0, px(6), 0)
        lay.setSpacing(px(8))
        self.dot = PulseDot(8, 16)
        self.dot.set_state("running")
        self.text = Elided("Getting started…", "body", "text")
        self.counter = label("", "mono_s", "text3")
        self.stop_btn = Button("Stop", "danger", icon="stop", small=True, tip="Stop this task")
        self.stop_btn.setFocusPolicy(Qt.NoFocus)
        self.stop_btn.clicked.connect(self.stop)
        self.exp = Button("", "icon", icon="expand", small=True, tip="Open the Hub (Ctrl+Alt+B)")
        self.exp.setFocusPolicy(Qt.NoFocus)
        self.exp.clicked.connect(self.expand)
        lay.addWidget(self.dot)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.counter)
        lay.addWidget(self.stop_btn)
        lay.addWidget(self.exp)

    def show_state(self, model):
        cur, n = model.current, model.needs
        if cur is None:
            return
        if n:
            self.dot.set_state("needs")
            self.text.setText(f"Needs you: {n['title']}")
            set_color(self.text, "warn")
        else:
            self.dot.set_state("running")
            self.text.setText(cur.step_text or "Working…")
            set_color(self.text, "text")
        self.counter.setText(f"{max(cur.step, 1)}/{cur.max_steps}")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.panel_rect().contains(e.position()):
            self.expand.emit()

    def place(self, screen):
        g = screen.availableGeometry()
        self.home = QPoint(g.center().x() - self.width() // 2, g.top() + px(8) - self.margin())
        self.move(self.home)

    def present(self, screen):
        self.place(screen)
        self.closing = False
        self._run(1.0, 200)
        self.show()
        self.raise_()

    def dismiss(self):
        if not self.isVisible() or self.closing:
            return
        self.closing = True
        self._run(0.0, 140)

    def hide_now(self):
        self.anim.stop()
        self.closing = False
        self.hide()

    def _run(self, to, ms):
        self.anim.stop()
        self.anim.setDuration(0 if CTX.reduced else ms)
        self.anim.setStartValue(self.t if self.isVisible() else 0.0)
        self.anim.setEndValue(to)
        self.anim.start()

    def _frame(self, v):
        self.t = float(v)
        self.setWindowOpacity(min(1.0, self.t * 1.2))
        self.move(self.home.x(), self.home.y() - int(px(10) * (1 - self.t)))

    def _settled(self):
        if self.closing:
            self.hide()
            self.closing = False
            self.t = 0.0
