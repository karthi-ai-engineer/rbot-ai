"""Preview for the human-in-the-loop UI (hitl_ui.py): question card + handover chip.

  python scripts/hitl_ui_preview.py                              live ~45 s sequence on screen (Ctrl+C quits)
  python scripts/hitl_ui_preview.py --sheet runs/hitl_ui.png     offscreen review sheet (nothing shown on screen)
  python scripts/hitl_ui_preview.py --selftest                   headless thread-safety tests (offscreen platform)

Live: the buddy asks "cookie banner" with 3 options + text box (auto-answers "Reject all" after 6 s if you
don't) -> handover: hops, chip at the top of the screen, parks bottom-left (click the drop or [I'm done],
or it carries on after 30 s) -> 3-2-1 countdown -> thanks -> stuck -> quit. Your real mouse is never moved.
"""
import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # project root, so `rbot` imports work

if "--selftest" in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"                 # no windows on the user's screen

from rbot.desktop.cursor_overlay import P, AgentCursor, QApplication  # noqa: E402,F401  (sets the DPI env var first)
from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter

from rbot.human import hitl_ui
from rbot.human.hitl_ui import PAD, HumanUI


# ---- live -------------------------------------------------------------------------------------
class Quitter(QObject):
    quit = Signal()


def live(theme):
    cur = AgentCursor(theme)
    cur.show()
    ui = HumanUI(cur)
    ui.on_stop.append(lambda: print("[Stop agent] clicked"))
    q = Quitter()
    q.quit.connect(QApplication.quit)
    g = QApplication.primaryScreen().availableGeometry()

    def sequence():
        cur.post("move_to", g.x() + g.width() * 0.38, g.y() + g.height() * 0.32)
        cur.post("say", "Opening the site", True)
        time.sleep(2.2)
        cur.post("say", "A cookie banner popped up", False)
        time.sleep(1.4)

        state = {"open": True}                                  # auto-answer if nobody clicks in 6 s
        threading.Timer(6.0, lambda: state["open"] and ui.answer("Reject all")).start()
        r = ui.ask("This site wants to set cookies. What should I do with the banner?",
                   ["Accept all", "Reject all", "I'll choose"])
        state["open"] = False
        print("ask ->", repr(r))
        if ui.stop_clicked.is_set():
            return q.quit.emit()
        time.sleep(1.4)
        cur.post("say", f"Okay: {r or 'skipped'}", False)
        time.sleep(1.6)

        ui.reset()
        ui.handover("Sign in", "Log in to your account, then click me")
        t0 = time.time()
        while not (ui.done_clicked.is_set() or ui.stop_clicked.is_set()) and time.time() - t0 < 30:
            time.sleep(0.1)
        print("handover ->", "done" if ui.done_clicked.is_set() else "stop" if ui.stop_clicked.is_set() else "timeout")
        if ui.stop_clicked.is_set():
            time.sleep(1.5)
            return q.quit.emit()
        for n in (3, 2, 1):
            ui.status(f"continuing in {n}…")
            time.sleep(1.0)
        ui.end_handover(thanks=True)
        time.sleep(4.0)

        ui.stuck("I can't find the Save button")
        time.sleep(5.6)
        cur.post("mood", "normal")
        cur.post("say", "That's the whole preview!", False)
        time.sleep(2.0)
        q.quit.emit()

    threading.Thread(target=sequence, daemon=True).start()


# ---- offscreen review sheet -----------------------------------------------------------------------
def sheet(out):
    from rbot.human.hitl_ui import AskPopup, HandoverChip, Pill
    W = 1720
    img = QImage(W, 1500, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#ECECF1"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    cap, head = QFont("Segoe UI"), QFont("Segoe UI")
    cap.setPixelSize(12)
    head.setPixelSize(14)
    head.setWeight(QFont.DemiBold)

    def panel(x, y, w, h, dark=True, label=""):
        g = QLinearGradient(x, y, x + w, y + h)
        if dark:
            g.setColorAt(0, QColor("#2B3A55"))
            g.setColorAt(1, QColor("#1B2333"))
        else:
            g.setColorAt(0, QColor("#F6F7FB"))
            g.setColorAt(1, QColor("#DCE3EE"))
        p.fillRect(QRectF(x, y, w, h), g)
        p.setPen(QColor("#B8C2D6") if dark else QColor("#5A6478"))
        p.setFont(cap)
        p.drawText(QRectF(x + 10, y + h - 22, w - 20, 18), Qt.AlignLeft | Qt.AlignVCenter, label)

    def title(x, y, text):
        p.setPen(QColor("#3A2A4A"))
        p.setFont(head)
        p.drawText(QPointF(x, y), text)

    cur = AgentCursor()
    cur.timer.stop()

    def buddy(x, y, mood="calling", text=""):
        cur.t, cur.tilt, cur.squish, cur.look, cur.ripples = 10.0, 0.0, 0.0, P(0, -0.6), []
        cur.mode, cur.mode_t0, cur.mood_, cur.mood_t0, cur.blink_t, cur.pop = "pointer", 0.0, mood, 10.0 - 2.4, -1, 0
        cur.vel = P(0, 0)
        cur.say(text)
        cur.bubble = 1.0 if text else 0.0
        cur.paint_to(p, P(x, y))

    popup = AskPopup()
    chip = HandoverChip()

    def ask_img(q, opts, allow_text=True, text="", focus_first=False):
        popup.load(q, opts, allow_text)
        popup.text.setText(text)
        if focus_first and popup.pills:
            popup.pills[0]._kbd_focus = True
            popup.pills[0].hasFocus = lambda: True
        popup.fit()
        popup.t, popup.snap, popup.closing = 1.0, None, False
        return popup.grab()

    def chip_img(t, d, done=False):
        chip.load(t, d)
        chip.rec.frozen = 1.0
        if done:
            chip.mark_done()
        chip.fit()
        return chip.grab()

    Q = "This site wants to set cookies. What should I do with the banner?"
    OPTS = ["Accept all", "Reject all", "I'll choose"]

    # 1. the main design, beside the buddy, on a dark and a light "desktop"
    title(16, 26, "ask() — question card beside the buddy (card at tip + (-8, 66), clamped to the buddy's screen)")
    panel(10, 36, 560, 380, True, "dark wallpaper · options + text box")
    buddy(70, 80, "calling", hitl_ui.shorten(Q))
    p.drawPixmap(QPoint(70 - 8 - PAD, 80 + 66 - PAD), ask_img(Q, OPTS))
    panel(580, 36, 560, 380, False, "light wallpaper · user typing (Send turns primary)")
    buddy(640, 80, "calling", hitl_ui.shorten(Q))
    p.drawPixmap(QPoint(640 - 8 - PAD, 80 + 66 - PAD), ask_img(Q, OPTS, text="Only the necessary ones"))
    panel(1150, 36, 560, 380, True, "options only (allow_text=False) · keyboard focus ring on first")
    buddy(1210, 80, "calling", "Which account?")
    p.drawPixmap(QPoint(1210 - 8 - PAD, 80 + 66 - PAD),
                 ask_img("Which account should I sign in with?", ["Work (karthi@…)", "Personal"], False,
                         focus_first=True))

    # 2. stress cases
    title(16, 448, "stress — long question + many options (wraps), text-only question")
    panel(10, 458, 560, 420, True, "long question, 5 options incl. a very long one")
    p.drawPixmap(QPoint(20, 466), ask_img(
        "The form has two 'Submit' buttons — one at the top that says 'Submit draft' and one at the bottom that "
        "says 'Submit for approval'. They look identical otherwise. Which one did you mean when you said submit?",
        ["Submit for approval", "Submit draft", "Neither", "Ask me again later",
         "Save it locally as a PDF first and then submit for approval"]))
    panel(580, 458, 560, 420, False, "text only (no options) · Send is primary, disabled until you type")
    p.drawPixmap(QPoint(590, 466), ask_img("What should the subject line of the email be?", []))
    panel(1150, 458, 560, 420, True, "one option + text")
    p.drawPixmap(QPoint(1160, 466), ask_img("I found 3 files called report.xlsx. Use the newest one?",
                                            ["Yes, the newest"]))

    # 3. the chip
    title(16, 910, "handover() chip — top-centre of the screen where the buddy parks")
    y = 920
    for dark, (t, d, done, lbl) in zip((True, False, True, False), [
            ("Sign in", "Log in to your account, then click me", False, "handover"),
            ("Sign in", "continuing in 3…", True, "after [I'm done] / drop clicked + status() countdown"),
            ("Captcha", "Please solve the captcha on the page — I can't do those and I'll wait right here for you",
             False, "long detail: elided at 380 px"),
            ("2-factor code", "", False, "no detail")]):
        panel(10, y, 1150, 86, dark, lbl)
        im = chip_img(t, d, done)
        p.drawPixmap(QPoint(10 + (1150 - im.width()) // 2, y - 8), im)
        y += 94

    # 4. motion frames
    title(16, 1316, "enter motion: scale 0.94 -> 1 from the corner nearest the buddy + fade, 180 ms strong ease-out "
                    "(frames at eased t = 0.15 / 0.4 / 0.7 / 1)")
    popup.load("Close the unsaved document?", ["Save", "Don't save"], False)
    popup.fit()
    popup.origin = QPointF(0.06, 0.0)
    fx = 10
    for t in (0.15, 0.4, 0.7, 1.0):
        popup.snap, popup.t, popup.closing = popup.card.grab(), t, False
        popup.card.move(-10000, PAD)
        panel(fx, 1326, 420, 168, True, f"t = {t}")
        p.drawPixmap(QPoint(fx + 4, 1326), popup.grab())
        popup.snap = None
        popup.card.move(PAD, PAD)
        fx += 428

    # 5. button states
    px, py = 1180, 920
    title(px, py - 10 + 20 - 10, "")
    panel(px, py, 530, 370, True, "pill states")
    rows = [("primary", "secondary", "danger", "ghost")]
    yy = py + 16
    for state in ("rest", "hover", "pressed", "disabled"):
        xx = px + 16
        for kind in rows[0]:
            b = Pill({"primary": "Reject all", "secondary": "I'll choose", "danger": "Stop agent",
                      "ghost": "Stop agent"}[kind], kind, small=kind in ("danger", "ghost"), focusable=False)
            b.resize(b.sizeHint())
            b.setAttribute(Qt.WA_TranslucentBackground)
            b.anim.v["hover"] = 1.0 if state in ("hover", "pressed") else 0.0
            b.anim.v["press"] = 1.0 if state == "pressed" else 0.0
            b.setEnabled(state != "disabled")
            p.drawPixmap(QPoint(xx, yy), b.grab())
            xx += b.width() + 14
        p.setPen(QColor("#B8C2D6"))
        p.setFont(cap)
        p.drawText(QPointF(xx + 6, yy + 21), state)
        yy += 60
    p.end()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.save(out)
    print("saved", out)


# ---- headless self-test ---------------------------------------------------------------------------
class FakeCursor(QObject):
    parked_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.pos_ = QPointF(500, 400)
        self.reduced = False
        self.log = []

    def post(self, *a):
        self.log.append(a)


def selftest():
    app = QApplication.instance()
    cur = FakeCursor()
    ui = HumanUI(cur)
    ui.HANDOVER_CALL_MS = 200
    stops = []
    ui.on_stop.append(lambda: stops.append(1))
    fails = []

    def check(name, cond, extra=""):
        print(("PASS " if cond else "FAIL ") + name, extra)
        if not cond:
            fails.append(name)

    def run(worker, *actions, limit=8.0):
        """Run worker() on a real thread; schedule (ms, fn) actions on the Qt thread; spin until done."""
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("r", worker()), daemon=True)
        for ms, fn in actions:
            QTimer.singleShot(ms, fn)
        th.start()
        t0 = time.time()
        while th.is_alive() and time.time() - t0 < limit:
            app.processEvents()
            time.sleep(0.003)
        for _ in range(60):                                   # let exit animations finish
            app.processEvents()
            time.sleep(0.005)
        return box.get("r", "<still blocked>")

    Q, O = "Cookies?", ["Accept all", "Reject all", "I'll choose"]

    r = run(lambda: ui.ask(Q, O), (300, lambda: ui.popup.pills[1].click()))
    check("click option -> option text", r == "Reject all", repr(r))
    check("buddy calling then normal", ("mood", "calling", "Cookies?") in cur.log and ("mood", "normal") in cur.log)
    check("popup hidden after answer", not ui.popup.isVisible())

    r = run(lambda: ui.ask("Subject?", None), (250, lambda: ui.popup.text.setText("  hello there ")),
            (300, lambda: ui.popup.text.returnPressed.emit()))
    check("typed text + Enter -> text", r == "hello there", repr(r))

    r = run(lambda: ui.ask("Subject?", O, timeout_s=0.8), (250, lambda: ui.popup.send.click()))
    check("Send with empty text is ignored (waits, then times out)", r is None, repr(r))

    t0 = time.time()
    r = run(lambda: ui.ask(Q, O, timeout_s=0.5))
    check("timeout -> None", r is None and 0.45 < time.time() - t0 < 3, f"{r!r} {time.time() - t0:.2f}s")
    check("popup hidden after timeout", not ui.popup.isVisible())

    from PySide6.QtTest import QTest
    r = run(lambda: ui.ask(Q, O), (300, lambda: QTest.keyClick(ui.popup.text, Qt.Key_Escape)))
    check("Esc -> None", r is None and ("say", "Okay, skipping that", False) in cur.log, repr(r))
    r = run(lambda: ui.ask(Q, O, allow_text=False), (300, lambda: QTest.keyClick(ui.popup.pills[0], Qt.Key_3)))
    check("options only: key 3 -> third option", r == "I'll choose", repr(r))

    r = run(lambda: ui.ask(Q, O), (300, lambda: ui.answer("Accept all")))
    check("ui.answer() from code -> text", r == "Accept all", repr(r))

    # one question at a time
    seen = []
    def two():
        out = {}
        ts = [threading.Thread(target=lambda k=k: out.__setitem__(k, ui.ask(f"Q{k}", ["a", "b"]))) for k in (1, 2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return out

    def answer_visible():
        seen.append(ui.popup.question.text())
        ui.popup.pills[0].click()
    r = run(two, (300, answer_visible), (900, answer_visible), limit=6)
    check("two concurrent asks are sequential", r == {1: "a", 2: "a"} and sorted(seen) == ["Q1", "Q2"], f"{r} {seen}")

    # stop from the popup
    r = run(lambda: ui.ask(Q, O), (300, lambda: ui.popup.stop_btn.click()))
    check("Stop agent in popup -> None + stop_clicked + on_stop", r is None and ui.stop_clicked.is_set() and stops,
          repr(r))
    check("popup hidden after stop", not ui.popup.isVisible())
    r = run(lambda: ui.ask(Q, O))
    check("ask while stopped -> None immediately", r is None)
    ui.reset()
    check("reset clears events", not ui.stop_clicked.is_set() and not ui.done_clicked.is_set())

    # should_stop hook (chat window's stop event / kill switch)
    flag = threading.Event()
    ui.should_stop = flag.is_set
    r = run(lambda: ui.ask(Q, O), (300, flag.set))
    check("should_stop() -> None", r is None)
    ui.should_stop = None

    # handover
    cur.log.clear()
    run(lambda: ui.handover("Sign in", "Log in, then click me"), limit=0.5)
    vis = ui.chip.isVisible()
    run(lambda: time.sleep(0.4))
    check("handover shows chip + calling + park", vis and ("park", "Your turn — click me when done") in cur.log
          and cur.log[0][:2] == ("mood", "calling"), str(cur.log))
    run(lambda: ui.status("continuing in 3…"))
    check("status -> chip text + bubble", ui.chip.detail.text() == "continuing in 3…"
          and ("say", "continuing in 3…", False) in cur.log)
    cur.parked_clicked.emit()
    check("parked click -> done_clicked", ui.done_clicked.is_set())
    run(lambda: ui.end_handover(True), limit=0.5)
    run(lambda: time.sleep(1.0))
    check("end_handover hides chip + thanks", not ui.chip.isVisible() and ("mood", "thanks") in cur.log,
          str(cur.log[-3:]))

    cur.log.clear()
    ui.reset()
    run(lambda: ui.handover("Captcha", "Solve it please"), (100, lambda: ui.chip.done_btn.click()), limit=0.6)
    check("chip [I'm done] -> done_clicked, no park", ui.done_clicked.is_set()
          and not any(a[0] == "park" for a in cur.log), str(cur.log))
    run(lambda: ui.handover("Captcha", "x"), (100, lambda: ui.chip.stop_btn.click()), limit=0.6)
    check("chip [Stop agent] -> stop_clicked + chip hidden", ui.stop_clicked.is_set() and not ui.chip.isVisible())
    ui.reset()
    run(lambda: ui.stuck("Can't find Save"))
    check("stuck -> mood stuck", ("mood", "stuck", "Can't find Save") in cur.log)

    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
    return not fails


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", help="render a PNG review sheet instead of the live preview")
    ap.add_argument("--selftest", action="store_true", help="headless thread-safety tests")
    ap.add_argument("--theme", default="grape")
    a = ap.parse_args()
    signal.signal(signal.SIGINT, signal.SIG_DFL)                  # Ctrl+C quits
    app = QApplication(sys.argv)
    if a.sheet:
        sheet(a.sheet)
        sys.exit(0)
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    live(a.theme)
    sys.exit(app.exec())
