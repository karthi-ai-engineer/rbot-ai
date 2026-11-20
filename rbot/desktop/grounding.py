"""Can Qwen 3.8 find UI elements on a real screenshot? (research prototype)

Ground truth comes from Windows UI Automation (real element names + boxes).
Methods compared:
  px    - model returns pixel x,y on the screenshot
  norm  - model returns x,y normalized to 0-1000
  som   - "set of marks": numbered boxes drawn on the screenshot, model returns the number
Each with thinking on, plus px/norm with thinking off (speed check).

  python -m rbot.desktop.grounding --n 10           run the test (results -> runs/)
  python -m rbot.desktop.grounding --replay runs/grounding_XXXX.json --method norm   watch the buddy cursor replay
"""
import argparse
import base64
import ctypes
import io
import json
import random
import re
import time
from ctypes import wintypes
from pathlib import Path

ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: pixels == input coords

from rbot import RUNS  # noqa: E402
CLICKABLE = {"ButtonControl", "MenuItemControl", "TabItemControl", "ListItemControl", "HyperlinkControl",
             "EditControl", "CheckBoxControl", "ComboBoxControl", "TreeItemControl", "SplitButtonControl",
             "RadioButtonControl"}


# ---------------------------------------------------------------- screen + elements
def capture():
    import mss
    from PIL import Image
    with mss.MSS() as s:
        mon = s.monitors[1]
        shot = s.grab(mon)
    return Image.frombytes("RGB", shot.size, shot.rgb), mon


def root_hwnd_at(x, y):
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = wintypes.HWND
    u.GetAncestor.restype = wintypes.HWND
    h = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return u.GetAncestor(h, 2) if h else None


def collect_elements(mon, max_depth=14, time_budget=25):
    import uiautomation as auto
    wins = []
    fg = auto.GetForegroundControl()
    if fg:
        wins.append(fg)
    tray = auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd")
    if tray.Exists(0, 0):
        wins.append(tray)
    out, t0 = [], time.time()
    for win in wins:
        top = win.NativeWindowHandle
        for ctrl, _ in auto.WalkControl(win, maxDepth=max_depth):
            if time.time() - t0 > time_budget:
                break
            try:
                if ctrl.ControlTypeName not in CLICKABLE or ctrl.IsOffscreen:
                    continue
                name = (ctrl.Name or "").strip()
                r = ctrl.BoundingRectangle
            except Exception:
                continue
            w, h = r.right - r.left, r.bottom - r.top
            if not name or len(name) > 60 or w < 8 or h < 8 or w > mon["width"] * 0.5 or h > mon["height"] * 0.5:
                continue
            cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
            if not (mon["left"] <= cx < mon["left"] + mon["width"] and mon["top"] <= cy < mon["top"] + mon["height"]):
                continue
            if root_hwnd_at(cx, cy) != top:      # hidden behind another window
                continue
            out.append({"name": name, "type": ctrl.ControlTypeName.replace("Control", ""),
                        "box": [r.left - mon["left"], r.top - mon["top"], r.right - mon["left"], r.bottom - mon["top"]]})
    counts = {}
    for e in out:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
    return [e for e in out if counts[e["name"]] == 1]   # unique names only, so the target is unambiguous


# ---------------------------------------------------------------- model calls
def to_data_url(img, max_w=1920):
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), img.size


def draw_marks(img, elems):
    from PIL import ImageDraw, ImageFont
    img = img.copy()
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("arialbd.ttf", 13)
    for i, e in enumerate(elems):
        x1, y1, x2, y2 = e["box"]
        d.rectangle([x1, y1, x2, y2], outline=(255, 0, 140), width=2)
        tw = d.textlength(str(i), font=font)
        d.rectangle([x1, y1, x1 + tw + 6, y1 + 16], fill=(255, 0, 140))
        d.text((x1 + 3, y1 + 1), str(i), fill="white", font=font)
    return img


def ask(client, model, method, img, target, marks, thinking):
    from_img = draw_marks(img, marks) if method == "som" else img
    url, (sw, sh) = to_data_url(from_img)
    desc = f'the {target["type"].lower()} named "{target["name"]}"'
    if method == "px":
        q = (f"This is a {sw}x{sh} pixel screenshot of a Windows desktop. Find {desc}. "
             'Reply with only JSON {"x": int, "y": int}: the pixel coordinates of its center in this image.')
    elif method == "norm":
        q = (f"This is a screenshot of a Windows desktop. Find {desc}. "
             'Reply with only JSON {"x": int, "y": int}: its center, with coordinates normalized to 0-1000 '
             "(0,0 = top-left, 1000,1000 = bottom-right).")
    else:
        listing = "\n".join(f"[{i}] {e['type']}: {e['name']}" for i, e in enumerate(marks))
        q = (f"This screenshot has numbered pink boxes on UI elements. Elements:\n{listing}\n\n"
             f'Which number is {desc}? Reply with only JSON {{"id": int}}.')
    extra = {} if thinking else {"chat_template_kwargs": {"enable_thinking": False}}
    t = time.time()
    r = client.chat.completions.create(
        model=model, max_tokens=4096 if thinking else 200, extra_body=extra,
        messages=[{"role": "user", "content": [{"type": "text", "text": q},
                                               {"type": "image_url", "image_url": {"url": url}}]}])
    dt = time.time() - t
    text = r.choices[0].message.content or ""
    nums = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", text.split("}")[0] if "}" in text else text)]
    box = re.search(r'bbox(?:_2d)?"?\s*:\s*\[([^\]]+)\]', text)       # models often answer in their native bbox format
    if method != "som" and box:
        b4 = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", box.group(1))][:4]
        if len(b4) == 4:
            nums = [(b4[0] + b4[2]) / 2, (b4[1] + b4[3]) / 2]
    pt = None
    if method == "som" and nums and 0 <= nums[0] < len(marks):
        b = marks[nums[0]]["box"]
        pt = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
    elif method == "px" and len(nums) >= 2:
        pt = [nums[0] * img.width / sw, nums[1] * img.height / sh]
    elif method == "norm" and len(nums) >= 2:
        pt = [nums[0] * img.width / 1000, nums[1] * img.height / 1000]
    return pt, dt, r.usage.completion_tokens, text.strip()[:120]


def run(n, seed):
    from rbot.llm import MODEL, get_client
    client = get_client()
    client.models.list()  # warm up the connection

    img, mon = capture()
    elems = collect_elements(mon)
    print(f"screen {img.width}x{img.height}, {len(elems)} usable elements found")
    if not elems:
        return
    rnd = random.Random(seed)
    targets = rnd.sample(elems, min(n, len(elems)))
    others = [e for e in elems if e not in targets]
    marks = targets + rnd.sample(others, min(len(others), max(0, 40 - len(targets))))
    rnd.shuffle(marks)

    variants = [("px", True), ("norm", True), ("som", True), ("px", False), ("norm", False)]
    results = {f"{m}{'' if th else '-fast'}": [] for m, th in variants}
    from concurrent.futures import ThreadPoolExecutor

    def one(tgt, m, th):
        try:
            return ask(client, MODEL, m, img, tgt, marks, th)
        except Exception as e:  # noqa: BLE001
            return None, 0, 0, f"ERROR {e}"

    with ThreadPoolExecutor(len(variants)) as pool:          # the 5 variants of one target run at once
        for i, tgt in enumerate(targets, 1):
            x1, y1, x2, y2 = tgt["box"]
            futs = {f"{m}{'' if th else '-fast'}": pool.submit(one, tgt, m, th) for m, th in variants}
            for key, f in futs.items():
                pt, dt, toks, raw = f.result()
                hit = bool(pt and x1 <= pt[0] <= x2 and y1 <= pt[1] <= y2)
                dist = round(((pt[0] - (x1 + x2) / 2) ** 2 + (pt[1] - (y1 + y2) / 2) ** 2) ** 0.5) if pt else None
                results[key].append({"target": tgt, "pt": pt, "hit": hit, "dist": dist, "sec": round(dt, 2),
                                     "tokens": toks, "raw": raw})
                print(f"[{i}/{len(targets)}] {key:<10} {'HIT ' if hit else 'miss'} {dt:5.1f}s {toks:5d} tok  "
                      f"off by {dist}px", flush=True)

    print("\nmethod      hits    avg sec  avg tokens  median miss distance")
    for key, rows in results.items():
        hits = sum(r["hit"] for r in rows)
        misses = sorted(r["dist"] for r in rows if not r["hit"] and r["dist"] is not None)
        med = misses[len(misses) // 2] if misses else "-"
        print(f"{key:<10} {hits:>2}/{len(rows):<3}  {sum(r['sec'] for r in rows) / len(rows):7.1f}  "
              f"{sum(r['tokens'] for r in rows) / len(rows):10.0f}  {med}")

    RUNS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = RUNS / f"grounding_{stamp}.json"
    out.write_text(json.dumps({"screen": [img.width, img.height], "monitor": mon, "results": results}, indent=1))
    img.save(RUNS / f"grounding_{stamp}.png")
    draw_marks(img, marks).save(RUNS / f"grounding_{stamp}_marks.png")
    print("saved", out)


# ---------------------------------------------------------------- replay with the buddy cursor
def replay(path, method):
    import signal
    import sys
    from PySide6.QtWidgets import QApplication
    from rbot.desktop.cursor_overlay import AgentCursor
    data = json.loads(Path(path).read_text())
    rows = data["results"][method]
    mon = data["monitor"]
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    cur = AgentCursor()
    dpr = app.primaryScreen().devicePixelRatio()

    def script():
        cur.say(f"Replaying: {method}")
        yield 1.5
        for r in rows:
            cur.say(f"Find: {r['target']['name'][:30]}", thinking=True)
            yield 1.0
            if r["pt"]:
                cur.move_to((r["pt"][0] + mon["left"]) / dpr, (r["pt"][1] + mon["top"]) / dpr)
                yield "arrive"
                cur.click()
            cur.say("Hit!" if r["hit"] else f"Missed ({r['dist']}px off)")
            yield 1.4
        hits = sum(r["hit"] for r in rows)
        cur.say(f"Score: {hits}/{len(rows)}")
        yield 3
        app.quit()

    cur.show()
    cur.run_script(script())
    sys.exit(app.exec())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--replay")
    ap.add_argument("--method", default="norm")
    a = ap.parse_args()
    if a.replay:
        replay(a.replay, a.method)
    else:
        run(a.n, a.seed)
