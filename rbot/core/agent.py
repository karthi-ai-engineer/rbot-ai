"""Cursor agent: an LLM looks at marked screenshots and drives the PC, one tool call per step.

  python -m rbot.core.agent "open notepad and type hello"            with the cute cursor overlay
  python -m rbot.core.agent "..." --no-cursor --max-steps 10          headless

Escalation ladder per step: L1 = MODEL thinking off -> L2 = MODEL thinking on -> L3 = $ESCALATION_MODEL
(if set). An attempt fails on: no/invalid tool call, unknown mark id, bad coords, executor error, an action
that did not change the screen, or the same action 3x in a row. A failed step is retried at the next level.
Every attempt is logged to runs/agent_<timestamp>/steps.jsonl (+ step_NN.jpg screenshots, summary.json).

Human in the loop (run_task(..., hitl=human_loop.HumanLoop(...))): sign-in / 2FA / CAPTCHA / payment screens are
handed to the human before the model is asked, consent popups ask the user, the model can ask_human(), a step
that fails at every level asks the human (I'll do it / Try again / Stop), and grabbing the mouse is a takeover.
Each human turn is reviewed (did the human finish the step?) and lands in the history as "HUMAN TURN ..." so the
model does not redo it; it is logged as a {"type": "human", ...} node in steps.jsonl.
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from rbot import RUNS
from rbot.llm import MODEL, get_client

ESCALATION_MODEL = os.getenv("ESCALATION_MODEL", "")
TOOL_CHOICE = os.getenv("TOOL_CHOICE", "auto")        # "auto" or "required" (whichever the gateway accepts)
DIFF_MIN = 0.002                                       # below this an action "did nothing"
TEXT_TYPES = ("Edit", "Document", "ComboBox")          # where typed text lands (local diff box)
MAX_IMG_W = 1920
SETTLE = {"open_app": 2.0, "open_url": 3.0}                             # seconds to wait before re-checking the screen
SETTLE_DEFAULT = 0.7
VERIFY = {"click", "double_click", "right_click", "drag", "type_text", "press_key", "hotkey", "open_app", "open_url",
          "press_and_hold"}
RESULT_KINDS = {"KillSwitch": "killed", "KeyboardInterrupt": "stopped", "HumanTakeover": "stopped",
                "MouseDenied": "stopped"}           # exceptions that end a task (anything else: "error", re-raised)
MOUSE_DENIED_TEXT = "you needed the mouse, so I stopped"


def end_result(e):
    """Exception that ended a run -> (result kind, text)."""
    kind = type(e).__name__
    res = RESULT_KINDS.get(kind, "error")
    if kind == "MouseDenied":
        return res, f"{MOUSE_DENIED_TEXT} ({e})" if str(e) else MOUSE_DENIED_TEXT
    return res, f"{kind}: {e}"

SYSTEM = """You control a Windows PC to complete the user's task.
Each turn you get a screenshot with numbered pink boxes on UI elements, plus a list of those elements.
Call exactly ONE tool per turn.
- Target elements by their box number ("id") whenever possible. Only if the target has no box, use "x","y"
  normalized to 0-1000 (0,0 = top-left, 1000,1000 = bottom-right of the screenshot), each a SINGLE number
  (e.g. "x": 460, "y": 537 -- never lists). Never use pixels. With x,y also give "target": a short
  description of what you are pointing at (e.g. "three-dot menu on the report card"); the system zooms in
  to aim exactly at it.
- Give every action a short "reason" (a few words, shown to the user).
- Look at the new screenshot to verify your previous action worked before moving on.
  If something did not work, try a different approach instead of repeating it.
- To enter text, click the input field first, then type_text. Use press_key("enter") to submit.
- Use open_app to start programs and web_search when you need facts from the internet.
- For files and folders use the file tools instead of clicking through Explorer: list_files, find_files,
  move_files, copy_files, rename_file, make_folder, zip_files, open_file, delete_files. They work without the
  screen. Paths are full paths or start with Downloads, Documents, Desktop, Pictures, Videos, Music or ~
  (e.g. "Downloads/report.pdf", "Documents/Reports"); wildcards like "Downloads/*.pdf" work in sources.
  delete_files only moves to the Recycle Bin and the user is always asked first; never delete unless the
  task asks for it.
- For a "press and hold" button use press_and_hold (holds the mouse button down for some seconds).
- Call ask_human(question, options) instead of guessing when information you need is missing, when the task
  is ambiguous (e.g. two people or files with the same name), or before anything irreversible the user did
  not explicitly ask for (sending, deleting, buying, submitting). Keep questions short; offer options.
  Write questions for a non-technical person in plain everyday words: never mention box numbers, ids,
  coordinates, tool names or error messages (e.g. "Which Tharun should I message: the one in Sales or IT?").
- NEVER type passwords, one-time/2FA codes, CAPTCHA answers or payment details, and never try to get past
  sign-in, verification, CAPTCHA or payment screens yourself: the system notices them and hands them to the
  human automatically. Just continue the task afterwards.
- History lines starting with HUMAN TURN, USER ANSWERED, USER INSTRUCTION or USER HINT are what the human did
  or said. Trust them: never redo work the human already did, and follow the user's instructions.
- Call done(summary) as soon as the task is complete. Call fail(reason) only if it is truly impossible."""


# ---------------------------------------------------------------- tools
def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


REASON = {"type": "string", "description": "a few words on why (shown to the user)"}
TARGET = {"id": {"type": "integer", "description": "number of the marked box (preferred)"},
          "x": {"type": "number", "description": "0-1000 normalized x, only if the target has no box"},
          "y": {"type": "number", "description": "0-1000 normalized y, only if the target has no box"},
          "target": {"type": "string", "description": "with x,y: short description of what you are pointing at, "
                                                      "e.g. 'three-dot menu on the report card'"}}


PATH_ARG = {"type": "string", "description": "full path, or starting with Downloads, Documents, Desktop, Pictures, "
                                             "Videos, Music or ~"}
PATHS_ARG = {"type": "array", "items": {"type": "string"},
             "description": "paths (full, or starting with Downloads, Documents, Desktop...); wildcards ok"}


def _pointer(name, desc, extra=None):
    return _fn(name, desc, {**TARGET, **(extra or {}), "reason": REASON}, ["reason"])


TOOLS = [
    _pointer("click", "Left-click an element."),
    _pointer("double_click", "Double-click an element (e.g. open a file or desktop icon)."),
    _pointer("right_click", "Right-click an element to open its context menu."),
    _pointer("move", "Hover the mouse over an element without clicking."),
    _pointer("scroll", "Scroll the mouse wheel over an element (screen center if no target given).",
             {"clicks": {"type": "integer", "description": "wheel notches; positive = up, negative = down"}}),
    _fn("drag", "Drag from one point to another. Each end is a box id or 0-1000 x,y.",
        {"from_id": {"type": "integer"}, "from_x": {"type": "number"}, "from_y": {"type": "number"},
         "to_id": {"type": "integer"}, "to_x": {"type": "number"}, "to_y": {"type": "number"},
         "target": {"type": "string", "description": "with x,y: short description of what you drag and where to"},
         "reason": REASON}, ["reason"]),
    _fn("type_text", "Type text into the focused field.", {"text": {"type": "string"}, "reason": REASON},
        ["text", "reason"]),
    _fn("press_key", "Press one key, e.g. enter, tab, esc, backspace, down, f5, win.",
        {"key": {"type": "string"}, "reason": REASON}, ["key", "reason"]),
    _fn("hotkey", "Press a key combination, e.g. [\"ctrl\", \"s\"].",
        {"keys": {"type": "array", "items": {"type": "string"}}, "reason": REASON}, ["keys", "reason"]),
    _fn("open_app", "Start a program by name, e.g. notepad, chrome, calculator.",
        {"name": {"type": "string"}, "reason": REASON}, ["name", "reason"]),
    _fn("open_url", "Open a website in the default web browser (fastest way to go to a site), e.g. chatgpt.com.",
        {"url": {"type": "string"}, "reason": REASON}, ["url", "reason"]),
    _fn("wait", "Wait for something to load.", {"seconds": {"type": "number"}, "reason": REASON},
        ["seconds", "reason"]),
    _fn("web_search", "Search the web; returns the top results as text.",
        {"query": {"type": "string"}, "reason": REASON}, ["query", "reason"]),
    _pointer("press_and_hold", "Press and hold the left mouse button on an element for some seconds, then let go "
             "(e.g. a 'press and hold' button).",
             {"seconds": {"type": "number", "description": "how long to hold (default 3)"}}),
    _fn("list_files", "List the files and folders in a folder (no screen needed).",
        {"folder": PATH_ARG, "pattern": {"type": "string", "description": "optional filter, e.g. *.pdf"},
         "reason": REASON}, ["folder", "reason"]),
    _fn("find_files", "Find files by (part of) their name, a wildcard like *.pdf, or text inside them, in the "
        "user's folders (or one folder).",
        {"name_or_text": {"type": "string"}, "folder": {**PATH_ARG, "description": "optional folder to search"},
         "max": {"type": "integer", "description": "most results (default 20)"}, "reason": REASON},
        ["name_or_text", "reason"]),
    _fn("move_files", "Move files or folders into a folder (made if missing).",
        {"sources": PATHS_ARG, "dest_folder": PATH_ARG, "reason": REASON}, ["sources", "dest_folder", "reason"]),
    _fn("copy_files", "Copy files or folders into a folder (made if missing).",
        {"sources": PATHS_ARG, "dest_folder": PATH_ARG, "reason": REASON}, ["sources", "dest_folder", "reason"]),
    _fn("rename_file", "Rename a file or folder (new name only, it stays in its folder).",
        {"path": PATH_ARG, "new_name": {"type": "string", "description": "just the new name, e.g. Nov report.pdf"},
         "reason": REASON}, ["path", "new_name", "reason"]),
    _fn("make_folder", "Make a new folder.", {"path": PATH_ARG, "reason": REASON}, ["path", "reason"]),
    _fn("zip_files", "Pack files or folders into a .zip file.",
        {"sources": PATHS_ARG, "zip_path": PATH_ARG, "reason": REASON}, ["sources", "zip_path", "reason"]),
    _fn("open_file", "Open a file with its default app (or a folder in Explorer).",
        {"path": PATH_ARG, "reason": REASON}, ["path", "reason"]),
    _fn("delete_files", "Move files or folders to the Recycle Bin (never a permanent delete). The user is always "
        "asked first.", {"paths": PATHS_ARG, "reason": REASON}, ["paths", "reason"]),
    _fn("ask_human", "Ask the user a short question and wait for the answer (missing info, ambiguity, or "
        "confirmation before something irreversible).",
        {"question": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"}, "description": "optional short answer choices"},
         "reason": REASON}, ["question", "reason"]),
    _fn("done", "The task is complete.", {"summary": {"type": "string"}}, ["summary"]),
    _fn("fail", "The task is impossible.", {"reason": {"type": "string"}}, ["reason"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
REQUIRED = {t["function"]["name"]: t["function"]["parameters"]["required"] for t in TOOLS}


class BadAction(Exception):
    """The model's answer can't be executed (invalid call, unknown id, bad coords...)."""


# ---------------------------------------------------------------- parsing the model reply
def strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return text.split("</think>")[-1].strip()           # opening tag sometimes lives in the template


def _json_objects(text):
    dec, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
            yield obj
            i = end
        except json.JSONDecodeError:
            i += 1


def _as_call(obj):
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):            # {"type":"function","function":{...}}
        obj = obj["function"]
    name = next((obj[k] for k in ("name", "tool", "action") if isinstance(obj.get(k), str)), None)
    if name not in TOOL_NAMES:
        return None
    args = next((obj[k] for k in ("arguments", "parameters", "args") if k in obj), None)
    if args is None:                                      # flat form: {"name": "click", "id": 3, ...}
        args = {k: v for k, v in obj.items() if k not in ("name", "tool", "action")}
    if isinstance(args, str):
        args = json.loads(args or "{}")
    return name, args


def parse_reply(message):
    """-> (name, args). Handles real tool_calls and tool-call JSON leaked into content."""
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        fn = calls[0].function
        if fn.name not in TOOL_NAMES:
            raise BadAction(f"unknown tool {fn.name!r}")
        try:
            args = json.loads(fn.arguments or "{}")
        except json.JSONDecodeError as e:
            raise BadAction(f"tool arguments are not valid JSON: {e}") from None
        return fn.name, args
    text = strip_think(getattr(message, "content", "") or "")
    chunks = re.findall(r"<tool_call>(.*?)(?:</tool_call>|$)", text, flags=re.S)
    chunks += re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S) + [text]
    for chunk in chunks:
        for obj in _json_objects(chunk):
            try:
                call = _as_call(obj)
            except json.JSONDecodeError:
                call = None
            if call:
                return call
    # Qwen3-Coder/3.6 XML style: <function=click><parameter=id>12</parameter>...</function>
    fx = re.search(r"<function=([\w\-]+)>(.*?)(?:</function>|$)", text, flags=re.S)
    if fx and fx.group(1) in TOOL_NAMES:
        args = {}
        for k, v in re.findall(r"<parameter=([\w\-]+)>\s*(.*?)\s*(?:</parameter>|(?=<parameter=)|$)", fx.group(2), flags=re.S):
            try:
                args[k] = json.loads(v)
            except json.JSONDecodeError:
                args[k] = v
        return fx.group(1), args
    raise BadAction("no tool call in reply: " + (text[:120] or "(empty)"))


def check_args(name, args):
    if not isinstance(args, dict):
        raise BadAction("arguments must be an object")
    missing = [k for k in REQUIRED[name] if args.get(k) in (None, "") and k != "reason"]
    if missing:
        raise BadAction(f"{name} is missing {', '.join(missing)}")


# ---------------------------------------------------------------- acting
def resolve(args, marks, size, prefix=""):
    """Box id or 0-1000 coords -> physical screen px (center of the box)."""
    mid = args.get(prefix + "id")
    if mid not in (None, ""):
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            raise BadAction(f"mark id {mid!r} is not a number") from None
        if mid not in marks:
            top = max(marks) if marks else 0
            hint = (" - that looks like a screen position: to click a spot without a box use x and y (0-1000), "
                    "not id" if mid > top else "")
            raise BadAction(f"unknown mark id {mid} (the boxes are numbered 0-{top}){hint}")
        x1, y1, x2, y2 = marks[mid]["box"]
        return round((x1 + x2) / 2), round((y1 + y2) / 2)
    x, y = args.get(prefix + "x"), args.get(prefix + "y")
    if x is None or y is None:
        raise BadAction(f"need {prefix}id or {prefix}x,{prefix}y")
    if isinstance(x, list) and isinstance(y, list) and len(x) == 2 and len(y) == 2:   # two corner points
        x, y = (x[0] + y[0]) / 2, (x[1] + y[1]) / 2                                    # -> their centre
    x = x[0] if isinstance(x, list) and len(x) == 1 else x                            # [460] -> 460
    y = y[0] if isinstance(y, list) and len(y) == 1 else y
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        raise BadAction(f"coordinates {x!r},{y!r} are not numbers") from None
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        raise BadAction(f"coordinates {x:g},{y:g} are outside 0-1000")
    w, h = size
    return round(x * (w - 1) / 1000), round(y * (h - 1) / 1000)


def _is_textbox_at(args, marks, obs):
    """Is the element under a coordinate click an editable text box? (focus clicks change nothing visible)"""
    try:
        import uiautomation as auto
        x, y = resolve(args, marks, obs["size"])
        ox, oy = obs.get("origin", (0, 0))
        with auto.UIAutomationInitializerInThread():
            c = auto.ControlFromPoint(x + ox, y + oy)
            for _ in range(4):
                if c is None:
                    return False
                if c.ControlTypeName in ("EditControl", "DocumentControl") or "message" in (c.Name or "").lower():
                    return True
                c = c.GetParentControl()
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------- zoom-in refine (coordinate pointing)
ZOOM_REFINE = True                        # x,y guesses get a second, zoomed-in look before the pointer goes there
ZOOM_W, ZOOM_H, ZOOM_SCALE = 360, 240, 3  # crop around the guess (physical px), magnification
REFINE_TIMEOUT = 25.0
REFINE_MAX_TOKENS = 200
SNAP_MAX = 60                             # guess inside a box smaller than this (px) -> snap to the box centre
EXACT_MAX_W, EXACT_MAX_H = 400, 100       # guess inside a button-sized box -> already on an element, no refine
REFINE_ACTIONS = {"click", "double_click", "right_click", "move", "drag", "press_and_hold"}


def _to_norm(px, py, size):
    w, h = size
    return (round(min(max(px, 0), w - 1) * 1000 / max(w - 1, 1), 1),
            round(min(max(py, 0), h - 1) * 1000 / max(h - 1, 1), 1))


def box_at(marks, px, py):
    """Smallest mark box containing the point (monitor px) -> mark or None."""
    hits = [m for m in marks.values() if m.get("box") and m["box"][0] <= px <= m["box"][2]
            and m["box"][1] <= py <= m["box"][3]]
    return min(hits, key=lambda m: (m["box"][2] - m["box"][0]) * (m["box"][3] - m["box"][1]), default=None)


def zoom_crop(size, px, py, cw=ZOOM_W, ch=ZOOM_H):
    """(l, t, r, b) of a cw x ch crop centred on the point, shifted to stay inside the image."""
    w, h = size
    cw, ch = min(cw, w), min(ch, h)
    l = min(max(0, round(px - cw / 2)), w - cw)
    t = min(max(0, round(py - ch / 2)), h - ch)
    return l, t, l + cw, t + ch


def make_zoom(img, box, guess=None, scale=ZOOM_SCALE):
    """Magnified crop with a light 10x10 grid labelled in 0-1000 (this image) and a ring at the guess."""
    from PIL import Image, ImageDraw, ImageFont
    l, t, r, b = box
    w, h = (r - l) * scale, (b - t) * scale
    z = img.crop(box).convert("RGB").resize((w, h), Image.LANCZOS)
    over = Image.new("RGBA", z.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    for i in range(1, 10):
        gx, gy = round(w * i / 10), round(h * i / 10)
        d.line([(gx, 0), (gx, h)], fill=(0, 150, 255, 70), width=1)
        d.line([(0, gy), (w, gy)], fill=(0, 150, 255, 70), width=1)
        for (tx, ty) in ((gx + 2, 1), (2, gy + 1)):
            lab = str(i * 100)
            d.rectangle([tx - 1, ty, tx + d.textlength(lab, font=font) + 1, ty + 14], fill=(255, 255, 255, 170))
            d.text((tx, ty), lab, fill=(0, 90, 200, 255), font=font)
    if guess:
        gx, gy = (guess[0] - l) * scale, (guess[1] - t) * scale
        d.ellipse([gx - 14, gy - 14, gx + 14, gy + 14], outline=(255, 0, 0, 200), width=2)
    return Image.alpha_composite(z.convert("RGBA"), over).convert("RGB")


def parse_point(message):
    """Model reply -> (x, y) in 0-1000, "none" (not visible) or None (unusable)."""
    texts = [getattr(c.function, "arguments", "") or "" for c in (getattr(message, "tool_calls", None) or [])]
    texts.append(strip_think(getattr(message, "content", "") or ""))
    for text in texts:
        for obj in _json_objects(text):
            if not isinstance(obj, dict):
                continue
            if obj.get("none") in (True, "true", "yes", 1) or obj.get("found") is False:
                return "none"
            x, y = obj.get("x"), obj.get("y")
            pt = obj.get("point_2d") or obj.get("point")
            bb = obj.get("bbox_2d") or obj.get("bbox")
            if x is None and isinstance(pt, list) and len(pt) >= 2:
                x, y = pt[:2]
            if x is None and isinstance(bb, list) and len(bb) == 4:
                try:
                    x, y = (float(bb[0]) + float(bb[2])) / 2, (float(bb[1]) + float(bb[3])) / 2
                except (TypeError, ValueError):
                    continue
            try:
                x, y = float(x), float(y)
            except (TypeError, ValueError):
                continue
            if 0 <= x <= 1000 and 0 <= y <= 1000:
                return x, y
    if any(re.search(r"\bnone\b\W*:\W*true|not visible", t, re.I) for t in texts):
        return "none"
    return None


def refine_point(img, size, px, py, target, llm, model, timeout=None):
    """One zoomed-in look at the guess (monitor px). -> (info dict, zoom image). info["status"] is
    "ok" (info["px"] = refined monitor px), or "none" / "timeout" / "error" / "garbage" (keep the guess)."""
    timeout = timeout or REFINE_TIMEOUT
    box = zoom_crop(size, px, py)
    zimg = make_zoom(img, box, (px, py))
    q = (f"This is a {ZOOM_SCALE}x magnified part of a screenshot, with a light grid; the numbers on the edges are "
         "coordinates 0-1000 in THIS image (0,0 = top-left, 1000,1000 = bottom-right). The red ring is a rough "
         f"first guess and may be off. Where exactly is: {target}? It may be an icon without text: pick the "
         "element that best fits the description. Reply with only JSON "
         '{"x": .., "y": ..} (the centre of that element, 0-1000 in this image). Reply {"none": true} only if '
         "nothing in this image can be it.")
    msgs = [{"role": "user", "content": [{"type": "text", "text": q},
                                         {"type": "image_url", "image_url": {"url": to_data_url(zimg)}}]}]
    res, t0 = {}, time.time()

    def work():
        try:
            if getattr(llm, "extra_kw", False):
                res["msg"] = llm(model, msgs, False, tools=None, max_tokens=REFINE_MAX_TOKENS, timeout=timeout)[0]
            else:
                res["msg"] = llm(model, msgs, False)[0]
        except Exception as e:  # noqa: BLE001
            res["err"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout)
    info = {"target": target, "crop": list(box), "latency_s": round(time.time() - t0, 2)}
    if th.is_alive():
        return {**info, "status": "timeout"}, zimg
    if "err" in res:
        return {**info, "status": "error", "error": str(res["err"])[:200]}, zimg
    info["raw"] = strip_think(getattr(res["msg"], "content", "") or "")[:200]
    pt = parse_point(res["msg"])
    if pt is None:
        return {**info, "status": "garbage"}, zimg
    if pt == "none":
        return {**info, "status": "none"}, zimg
    l, t, r, b = box
    return {**info, "status": "ok", "zoom_xy": [pt[0], pt[1]],
            "px": [l + pt[0] * (r - l) / 1000, t + pt[1] * (b - t) / 1000]}, zimg


def refine_args(name, args, obs, marks, llm, model, save=None):
    """Zoom-refine the x,y ends of a pointing action. -> (new args, list of log infos, note for the history).
    save(suffix, image) stores the zoom image. Id targets and bad coordinates are left alone."""
    if name not in REFINE_ACTIONS:
        return args, [], ""
    size, new, infos, notes = obs["size"], dict(args), [], []
    for p in (("from_", "to_") if name == "drag" else ("",)):
        if args.get(p + "id") not in (None, ""):
            continue
        try:
            px, py = resolve(args, {}, size, p)
        except BadAction:
            continue                                      # execute() reports it
        try:
            orig = [float(args[p + "x"]), float(args[p + "y"])]      # what the model said
        except (TypeError, ValueError, KeyError):
            orig = list(_to_norm(px, py, size))                     # e.g. list forms -> the point resolve() made
        info = {"end": p.rstrip("_") or "point", "orig": orig}
        m = box_at(marks, px, py)
        if m is not None:
            x1, y1, x2, y2 = m["box"]
            if x2 - x1 < SNAP_MAX and y2 - y1 < SNAP_MAX:  # small box: its centre is exact
                nx, ny = _to_norm((x1 + x2) / 2, (y1 + y2) / 2, size)
                new[p + "x"], new[p + "y"] = nx, ny
                infos.append({**info, "status": "snap", "box_id": m.get("id"), "refined": [nx, ny]})
                notes.append(f"snapped to the centre of '{str(m.get('name', ''))[:30]}'")
                continue
            if x2 - x1 <= EXACT_MAX_W and y2 - y1 <= EXACT_MAX_H:
                infos.append({**info, "status": "in_box", "box_id": m.get("id")})
                continue
        tgt = str(args.get("target") or "").strip() or str(args.get("reason") or "").strip() or "the target"
        if name == "drag":
            tgt = f"the {'start' if p == 'from_' else 'end'} point of: {tgt}"
        info_r, zimg = refine_point(obs["image"], size, px, py, tgt, llm, model)
        info.update(info_r)
        if save is not None:
            try:
                info["zoom"] = save(("_" + p.rstrip("_")) if p else "", zimg)
            except Exception:  # noqa: BLE001
                pass
        if info["status"] == "ok":
            nx, ny = _to_norm(*info.pop("px"), size)
            new[p + "x"], new[p + "y"] = nx, ny
            info["refined"] = [nx, ny]
            notes.append(f"zoom check moved {p or 'the point '}to ({nx:g},{ny:g})")
        else:
            info.pop("px", None)
            why = {"none": "target not found in the zoomed view", "timeout": "timed out"}.get(info["status"],
                                                                                              info["status"])
            notes.append(f"zoom check failed ({why}) - kept your guess")
        infos.append(info)
    return new, infos, "; ".join(notes)


# ---------------------------------------------------------------- hover check (what is really under the point?)
HOVER_CHECK = True                        # before a click: ask Windows what is under the pointer
HOVER_TIMEOUT = 0.3                       # WebView2/Chromium can be slow -> past this, just click
HOVER_UP = 15                             # how many UIA parents to walk up
HOVER_ACTIONS = {"click", "double_click", "right_click"}
RECT_TOL = 4


class Covered(BadAction):
    """Something else (another top-level window) is on top of the marked element right now."""


def root_at(x, y):
    """Top-level window handle at an absolute screen point (or None)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = wintypes.HWND
    u.GetAncestor.restype = wintypes.HWND
    h = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return u.GetAncestor(h, 2) if h else None


def window_title(hwnd):
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def own_window(hwnd):
    """Is this window ours (buddy cursor, chat window)? Then it tells nothing about the target app."""
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def mark_roots(obs):
    """{mark id: top-level window at its centre} - taken at observe time, compared at click time."""
    ox, oy = obs.get("origin", (0, 0))
    out = {}
    for m in obs["marks"]:
        try:
            x1, y1, x2, y2 = m["box"]
            out[int(m["id"])] = root_at((x1 + x2) / 2 + ox, (y1 + y2) / 2 + oy)
        except Exception:  # noqa: BLE001
            pass
    return out


def probe_point(x, y, match=None, up=HOVER_UP, timeout=None):
    """UIA element under the absolute screen point and its parents, in a worker thread with a hard timeout.
    match(element dict) -> True stops the walk early. -> {"root", "chain": [{"type","name","rect"}...],
    "matched", "done", "timed_out"}; on timeout the chain holds whatever was read so far."""
    timeout = timeout or HOVER_TIMEOUT
    out = {"root": None, "chain": [], "matched": False, "done": False, "timed_out": False}
    fin = threading.Event()

    def work():
        try:
            out["root"] = root_at(x, y)
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread():
                c = auto.ControlFromPoint(int(x), int(y))
                for _ in range(up):
                    if c is None or fin.is_set():
                        break
                    r = c.BoundingRectangle
                    el = {"type": (c.ControlTypeName or "").replace("Control", ""), "name": (c.Name or "").strip(),
                          "rect": [r.left, r.top, r.right, r.bottom]}
                    out["chain"].append(el)
                    if match is not None and match(el):
                        out["matched"] = True
                        break
                    c = c.GetParentControl()
            out["done"] = True
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:200]
        finally:
            fin.set()

    threading.Thread(target=work, daemon=True).start()
    if not fin.wait(timeout):
        out["timed_out"] = True
        fin.set()                                        # tell the worker to stop walking
    out["chain"] = list(out["chain"])
    return out


def _same_rect(a, b, tol=RECT_TOL):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def _inside(inner, outer, tol=RECT_TOL):
    return (inner[2] > inner[0] and inner[3] > inner[1] and inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _mon_rect(el, origin, size):
    """UIA rect (absolute px) -> monitor-relative and clamped, like the mark boxes."""
    (ox, oy), (w, h) = origin, size
    l, t, r, b = el["rect"]
    return [min(max(l - ox, 0), w), min(max(t - oy, 0), h), min(max(r - ox, 0), w), min(max(b - oy, 0), h)]


def mark_matcher(mark, origin, size):
    """-> fn(element) True if a UIA element (absolute rect) IS the marked element: same name+type or same box."""
    box, mname, mtype = mark["box"], str(mark.get("name", "")).strip(), str(mark.get("type", ""))

    def match(el):
        name = el["name"].replace("\n", " ")[:80]
        if el["type"] == mtype and (name == mname or (not name and mname in ("(edit)", "(document)"))):
            return True
        return _same_rect(_mon_rect(el, origin, size), box)
    return match


def _el_text(chain, n=4):
    """'Download report' (Button): the first named element of the hit and its first parents."""
    for el in chain[:n]:
        if el["name"]:
            return f"'{el['name'][:50]}' ({el['type']})"
    return f"an unnamed {chain[0]['type'] or 'element'}" if chain else ""


def hover_check(name, args, x, y, marks, origin, size, roots):
    """Pointer is at (x, y) (absolute px), button not pressed. -> info dict (info["note"] = text for the history).
    Raises Covered if a box-id target is hidden behind a different top-level window than at observe time."""
    mid = args.get("id")
    mark = marks.get(int(mid)) if str(mid).isdigit() else None
    t0 = time.time()
    p = probe_point(x, y, match=mark_matcher(mark, origin, size) if mark else None,
                    up=HOVER_UP if mark else 4)
    info = {"ms": round((time.time() - t0) * 1000), "hit": _el_text(p["chain"]), "matched": p["matched"],
            "timed_out": p["timed_out"]}
    if mark is None:                                     # coordinate click: never block, just tell the model
        if info["hit"]:
            verb = {"click": "clicked", "double_click": "double-clicked", "right_click": "right-clicked"}[name]
            info["note"] = f"{verb} on {info['hit']}"
        return info
    if p["matched"]:
        return info                                      # it's the element -> click
    then, now = (roots or {}).get(int(mid)), p["root"]
    if now is None and p["timed_out"]:                   # slow UIA (WebView2): the window check is cheap, do it anyway
        now = root_at(x, y)
    if then and now and then != now and not own_window(now) and not own_window(then):
        info["blocked"] = True                           # a different window covers it (popup, dialog, menu...)
        err = Covered(f"something else is on top: {info['hit'] or 'an element'} in '{window_title(now)[:60]}' "
                      f"covers #{mid} - look at the new screenshot")
        err.info = info
        raise err
    if p["timed_out"] or not p["done"]:
        return info                                      # same window but we can't tell more in time -> click
    info["inside"] = any(_inside(_mon_rect(el, origin, size), mark["box"]) for el in p["chain"])
    return info                                          # same window: a child of the box, or unclear -> click


def web_search(query, n=5):
    try:
        from ddgs import DDGS
    except ImportError:
        return "web_search unavailable (pip install ddgs)"
    try:
        rows = DDGS().text(query, max_results=n)
    except Exception as e:  # noqa: BLE001
        return f"search error: {e}"
    return "\n".join(f"- {r.get('title', '')}: {r.get('body', '')} ({r.get('href', '')})" for r in rows) \
        or "no results"


def execute(name, args, ex, marks, size, origin=(0, 0), roots=None, info=None, hover=True):
    """Run one tool call on the executor. Returns a short outcome text. origin = monitor offset of the screenshot.
    Clicks get the hover check (HOVER_CHECK): roots = mark_roots() at observe time; info (dict) receives what
    was under the pointer. Raises Covered (a BadAction) if a box target is covered by another window.
    hover=False skips the hover check (browser_exec: tab pixels, nothing on the real screen to probe).
    The method router (router.py) decides which executor gets the call; file commands never come here."""
    def at(prefix=""):
        x, y = resolve(args, marks, size, prefix)
        return x + origin[0], y + origin[1]

    if name == "press_and_hold":
        hold = getattr(ex, "hold", None)
        if not callable(hold):
            raise BadAction("press and hold is not available with this executor")
        try:
            secs = float(args.get("seconds") or 3.0)
        except (TypeError, ValueError):
            secs = 3.0
        hold(*at(), max(0.2, min(secs, 15.0)))
        return "ok"
    if name in HOVER_ACTIONS and HOVER_CHECK and hover:
        seen = {} if info is None else info

        def hook(x, y):
            try:
                seen.update(hover_check(name, args, x, y, marks, origin, size, roots))
            except Covered as e:
                seen.update(getattr(e, "info", {}))
                raise
            except Exception as e:  # noqa: BLE001  (a broken check must never stop a click)
                seen["error"] = str(e)[:200]
        ex.before_press = hook
        try:
            getattr(ex, name)(*at())
        finally:
            ex.before_press = None
        return "ok - " + seen["note"] if seen.get("note") else "ok"
    if name in ("click", "double_click", "right_click", "move"):
        getattr(ex, name)(*at())
    elif name == "scroll":
        has_target = any(args.get(k) not in (None, "") for k in ("id", "x", "y"))
        x, y = at() if has_target else (origin[0] + size[0] // 2, origin[1] + size[1] // 2)
        ex.scroll(x, y, int(args.get("clicks") or -3))
    elif name == "drag":
        ex.drag(*at("from_"), *at("to_"))
    elif name == "type_text":
        ex.type_text(str(args["text"]))
    elif name == "press_key":
        ex.press_key(str(args["key"]))
    elif name == "hotkey":
        keys = args["keys"]
        ex.hotkey(re.split(r"\s*\+\s*", keys) if isinstance(keys, str) else [str(k) for k in keys])
    elif name == "open_app":
        ex.open_app(str(args["name"]))
    elif name == "open_url":
        url = str(args["url"]).strip()
        if not re.match(r"^https?://", url):
            url = "https://" + url
        if not re.match(r"^https?://[\w.-]+", url):
            raise BadAction(f"not a web address: {url!r}")
        ex._check()                                        # kill switch / user-takeover pause
        opened = False
        if os.environ.get("BUDDY_BROWSER", "1") != "0":    # the buddy browser: pages read straight from the DOM
            try:
                from rbot.browser import browser_dom
                opened = browser_dom.open_url(url)
            except Exception:  # noqa: BLE001
                opened = False
        if not opened:
            os.startfile(url)
    elif name == "wait":
        ex.wait(max(0.0, min(float(args["seconds"]), 30.0)))
    elif name == "web_search":
        return web_search(str(args["query"]))[:1500]
    return "ok"


def describe(name, args, marks):
    """Short human-readable action text, e.g. "click #12 'Save'"."""
    if name in ("done", "fail"):
        return f"{name}: {args.get('summary') or args.get('reason') or ''}"[:80]
    s = name
    for p in ("", "from_", "to_"):
        mid = args.get(p + "id")
        if mid not in (None, ""):
            try:
                label = marks.get(int(mid), {}).get("name", "?")
            except (TypeError, ValueError):
                label = "?"
            s += f" {p}#{mid} '{label[:30]}'"
        elif args.get(p + "x") is not None:
            s += f" {p}({args.get(p + 'x')},{args.get(p + 'y')})"
            if p != "to_" and str(args.get("target") or "").strip():
                s += f" '{str(args['target']).strip()[:40]}'"
    for k in ("text", "key", "keys", "name", "seconds", "query", "clicks", "question",
              "folder", "pattern", "name_or_text", "sources", "paths", "path", "new_name", "dest_folder", "zip_path"):
        if k in args:
            s += f" {json.dumps(args[k], ensure_ascii=False)[:40]}"
    return s


def action_key(name, args):
    return name + json.dumps({k: v for k, v in args.items() if k not in ("reason", "target")}, sort_keys=True)


LOOP_ACTIONS = {"click", "double_click", "right_click", "move", "drag", "press_and_hold"}
LOOP_WINDOW, LOOP_REPEATS = 8, 3          # the same spot 3 times within 8 actions = going in circles


LOOP_NEAR = 25                            # 0-1000 units: clicks closer than this count as "the same spot"


def loop_key(name, args):
    """Key for loop detection: (action, box id) or (action, x, y) in 0-1000 coords."""
    if name not in LOOP_ACTIONS:
        return None
    if str(args.get("id", "")).isdigit():
        return (name, int(args["id"]))
    try:
        return (name, float(args.get("x")), float(args.get("y")))
    except (TypeError, ValueError):
        return None


def loop_count(lk, recent):
    """How many of the recent loop keys are the same action on the same element / spot as lk."""
    def same(a):
        if a[0] != lk[0] or len(a) != len(lk):
            return False
        if len(a) == 2:
            return a[1] == lk[1]
        return abs(a[1] - lk[1]) <= LOOP_NEAR and abs(a[2] - lk[2]) <= LOOP_NEAR
    return sum(1 for a in recent if same(a))


# ---------------------------------------------------------------- prompt
def to_data_url(img, max_w=MAX_IMG_W):
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_messages(task, history, obs, feedback=""):
    marks = "\n".join(f"[{m['id']}] {m.get('type', '')}: {str(m.get('name', ''))[:60]}" for m in obs["marks"])
    hist = "\n".join(history[-15:]) or "(none yet)"
    text = (f"TASK: {task}\n\nActive window: {obs.get('window', '')}\n\n"
            f"Previous actions:\n{hist}\n\n"
            + (f"IMPORTANT: {feedback}\n\n" if feedback else "")
            + f"Marked elements:\n{marks or '(no boxes found; use x,y)'}\n\n"
            "Call one tool for the next action.")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": text},
                                         {"type": "image_url", "image_url": {"url": to_data_url(obs["marked"])}}]}]


# ---------------------------------------------------------------- model
def make_llm():
    client = get_client(timeout=600)

    def llm(model, messages, thinking, tools=TOOLS, max_tokens=None, timeout=None):
        kw = {"tools": tools, "tool_choice": TOOL_CHOICE} if tools else {}
        if timeout:                                      # keep the client's short connect timeout (+ retries):
            import httpx                                 # new connections to the gateway sometimes hang at connect
            kw["timeout"] = httpx.Timeout(timeout, connect=4.0)
        r = client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens or (8192 if thinking else 1024),
            extra_body={"chat_template_kwargs": {"enable_thinking": thinking}}, **kw)
        return r.choices[0].message, r.usage
    llm.extra_kw = True                                  # takes tools/max_tokens/timeout (used by the zoom refine)
    return llm


def make_levels():
    levels = [(MODEL, False), (MODEL, True)]
    if ESCALATION_MODEL:
        levels.append((ESCALATION_MODEL, True))
    return levels


# ---------------------------------------------------------------- the loop
HUMAN_OK = ("done", "answered", "instructed", "retry")


def _human_end(res):
    """A human turn that ends the task -> run_task result tuple, else None."""
    out, kind = res.get("outcome"), res.get("kind", "")
    if out == "timeout":
        return "stopped", f"timeout waiting for human ({kind})"
    if out == "stopped":
        return "stopped", f"stopped by the user during the human turn ({kind}: {res.get('how', '')})"
    return None


IMAGE_GLOBS = ("step_*.jpg", "*_zoom*.jpg", "human_*/frame_*.jpg", "**/*.png")


def cleanup_images(run_dir):
    """Delete the screenshots a run produced (step_NN*.jpg, zoom crops, human-turn frames, any .png).
    Keeps steps.jsonl, summary.json, human_*/session.json and videos. -> number of files removed."""
    n, root = 0, Path(run_dir)
    for pat in IMAGE_GLOBS:
        for p in root.glob(pat):
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
            except OSError:
                pass
    return n


def run_task(task, executor, cursor=None, max_steps=25, *, observe=None, diff_ratio=None, llm=None,
             levels=None, run_dir=None, stop=None, settle=True, guard=None, hitl=None, keep_images=False,
             mode="live", invisible=None, browser_ex=None, observe_tab=None, allowed_folders=None):
    """Returns ("done"|"fail"|"max_steps"|"stopped"|"killed"|"error", text). observe/diff_ratio/llm are
    injectable for offline tests; by default they come from perception.py and the gateway.
    keep_images=False deletes the run's screenshots when the task ends (logs, summary.json, videos stay).
    hitl: a human_loop.HumanLoop (or None = fully autonomous, the old behaviour). With it: blockers
    (login/otp/captcha/payment/consent) are handed to the human before the model is asked, the ask_human tool
    reaches the user, "every level failed" asks the human instead of failing, and grabbing the mouse while the
    agent works is a takeover (HumanTakeover) instead of a 3 s pause.
    Method router (router.py): mode "live" | "background" (background: browser steps run in the buddy browser tab
    via browser_exec / browser_dom.observe_tab; browser_ex / observe_tab inject them). invisible: None = UI
    Automation clicks on when observing the real screen (BUDDY_INVISIBLE, default on), False = off, True = on,
    or a try_invisible callable. allowed_folders overrides the file commands' allow-list."""
    real_screen = observe is None
    if observe is None or diff_ratio is None:
        from rbot.desktop import perception
        observe, diff_ratio = observe or perception.observe, diff_ratio or perception.diff_ratio
    llm = llm or make_llm()
    levels = levels or make_levels()
    run_dir = Path(run_dir or RUNS / f"agent_{time.strftime('%Y%m%d_%H%M%S')}")
    run_dir.mkdir(parents=True, exist_ok=True)
    say = (lambda text, thinking=False: cursor.post("say", text, thinking)) if cursor else (lambda *a: None)
    from rbot.core import router as router_mod
    router = router_mod.Router(executor, cursor, task=task, mode=mode, observe=observe, real_screen=real_screen,
                               invisible=invisible, browser_ex=browser_ex, observe_tab=observe_tab,
                               ask=(lambda q, o: hitl.ask(q, o)) if hitl is not None else None,
                               say=lambda text: say(text), hitl=hitl, allowed=allowed_folders, stop=stop)
    observe = router.observe                    # the real screen, or the buddy browser tab in background mode

    t_start = time.time()
    history, keys = [], []                      # text lines for the prompt / executed action keys (loop check)
    loops = []                                  # coarse keys of executed clicks/moves (going-in-circles check)
    last_box = None                             # box of the last element we clicked (where typing lands)
    per_level = {f"L{i + 1}": {"attempts": 0, "ok": 0} for i in range(len(levels))}
    result, steps, streak, obs = ("max_steps", f"stopped after {max_steps} steps"), 0, 0, None
    humans, carry, goal = [], "", ""            # human turns / feedback for the next step / agent's current goal
    print(f"task: {task}\nlog:  {run_dir}", flush=True)
    log = open(run_dir / "steps.jsonl", "a", encoding="utf-8")

    def human(step, res):
        """Log a human interaction (steps.jsonl node + trace + prompt history). -> final result or None."""
        rv = res.get("review") or {}
        kind, out = res.get("kind", "?"), res.get("outcome", "?")
        n = {"step": step, "type": "human", "reason": res.get("reason", ""), "kind": kind, "outcome": out,
             "how": res.get("how", ""), "waited_s": res.get("waited_s"), "summary": res.get("summary", ""),
             "review": res.get("review"), "answer": res.get("answer"),
             "action": f"human:{kind}", "args": {"reason": str(res.get("answer") or rv.get("verdict") or out)},
             "ok": out in HUMAN_OK}                           # action/args/ok: readable by chat_app's summary
        log.write(json.dumps(n, ensure_ascii=False, default=str) + "\n")
        log.flush()
        humans.append({"step": step, "kind": kind, "outcome": out, "waited_s": n["waited_s"],
                       "verdict": rv.get("verdict")})
        print(f"[step {step} HUMAN] {kind}: {out} ({n['how']}) {float(n['waited_s'] or 0):.0f}s"
              + (f" — review: {rv.get('verdict')}" if rv else "")
              + (f" — answer: {res['answer']!r}" if res.get("answer") else ""), flush=True)
        if res.get("history"):
            history.append(f"{step}. {res['history']}")
        return _human_end(res)

    def takeover(o):
        return hitl.handover("user took over", "takeover", o, task, goal or "the agent's current step",
                             history=history)

    soft_streak = 0                              # steps in a row where nothing visibly changed at any level
    if hitl is not None:
        if router.mode == "background":          # human turns on the tab: its window comes to the front
            hitl.handover = router.wrap_handover(hitl.handover)
        hitl.bind(observe=observe, stop=stop, run_dir=run_dir)
        executor.takeover_handler = hitl.on_takeover
    try:
        for step in range(1, max_steps + 1):
            steps = step
            start = 1 if streak >= 2 and len(levels) > 1 else 0        # adaptive: L2 needed twice -> start at L2
            feedback, final, soft = carry, None, True   # soft: every failure so far was only "no visible change"
            carry, att, hres, node = "", 0, None, {"fail_reason": ""}

            if hitl is not None:                        # the human's turn comes before the model's
                if (stop is not None and stop.is_set()) or hitl.stop_requested():
                    raise KeyboardInterrupt
                try:
                    executor._check()                   # kill switch / mouse grabbed while the model was thinking
                    if obs is None:
                        obs = observe()
                    pre = hitl.check_blocker(obs, task, history)
                except Exception as e:  # noqa: BLE001
                    if type(e).__name__ != "HumanTakeover":
                        raise
                    pre = takeover(obs)
                if pre:
                    end = human(step, pre)
                    if end:
                        result = end
                        break
                    feedback = pre.get("feedback") or feedback
                    if pre.get("screen_changed"):
                        obs, keys, last_box = None, [], None
                        loops.clear()

            while True:                                 # the ladder; restarts at L1 when the user says "try again"
                for li in range(start, len(levels)):
                    if stop is not None and stop.is_set():
                        raise KeyboardInterrupt
                    model, thinking = levels[li]
                    att += 1
                    if obs is None:
                        obs = observe()
                    if HOVER_CHECK and "roots" not in obs and not obs.get("background"):   # which window is at
                        obs["roots"] = mark_roots(obs)    # each box NOW (before the model thinks): compared at click
                    obs["marked"].convert("RGB").save(
                        run_dir / (f"step_{step:02d}.jpg" if att == 1 else f"step_{step:02d}_a{att}.jpg"),
                        quality=85)
                    marks = {int(m["id"]): m for m in obs["marks"]}
                    node = {"step": step, "attempt": att, "level": li + 1, "model": model, "thinking": thinking,
                            "action": None, "args": None, "reason": "", "latency_s": None, "prompt_tokens": None,
                            "completion_tokens": None, "ok": False, "fail_reason": "", "diff": None, "method": None}
                    name, args, desc, outcome, executed, took_over, asked = None, {}, "", "", False, False, None
                    lk = None

                    say("Thinking hard" if thinking else "Thinking", True)
                    t0 = time.time()
                    try:
                        message, usage = llm(model, build_messages(task, history, obs, feedback), thinking)
                        node["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                        node["completion_tokens"] = getattr(usage, "completion_tokens", None)
                    except Exception as e:  # noqa: BLE001  (network / gateway error -> try the next level)
                        message = None
                        node["fail_reason"] = f"model call error: {e}"[:300]
                    node["latency_s"] = round(time.time() - t0, 2)

                    if message is not None:
                        try:
                            name, args = parse_reply(message)
                            check_args(name, args)
                            node.update(action=name, args=args, reason=str(args.get("reason", ""))[:100])
                            desc = describe(name, args, marks)
                            if name not in ("done", "fail"):
                                goal = f"{node['reason']} ({desc})" if node["reason"] else desc
                            key = action_key(name, args)
                            if name not in ("done", "fail", "wait") and keys[-2:] == [key, key]:
                                raise BadAction("same action 3 times in a row")
                            lk = loop_key(name, args)
                            if lk and loop_count(lk, loops[-LOOP_WINDOW:]) >= LOOP_REPEATS - 1:
                                raise BadAction("going in circles: you already did this "
                                                f"{LOOP_REPEATS - 1} times in the last steps and the task "
                                                "did not move forward. Try a DIFFERENT kind of action (press_and_hold if the page mentions holding, double_click, the keyboard) or ask_human")
                            if name == "fail" and (li < len(levels) - 1 or hitl is not None):
                                raise BadAction(f"gave up ({args.get('reason', '')}); a stronger level will retry")
                            if ZOOM_REFINE and name in REFINE_ACTIONS:     # x,y guess -> zoom in and aim
                                tag = f"step_{step:02d}" + ("" if att == 1 else f"_a{att}")

                                def save_zoom(suffix, im, tag=tag):
                                    fn = f"{tag}_zoom{suffix}.jpg"
                                    im.save(run_dir / fn, quality=85)
                                    return fn
                                if any(args.get(p + "id") in (None, "") and args.get(p + "x") is not None
                                       for p in (("from_", "to_") if name == "drag" else ("",))):
                                    say("Taking a closer look", True)
                                args, rinfo, rnote = refine_args(name, args, obs, marks, llm, model, save_zoom)
                                if rinfo:
                                    node["refine"] = rinfo
                                    for r in rinfo:
                                        if r["status"] not in ("in_box",):
                                            print(f"[step {step} L{li + 1}] refine {r['end']}: {r['status']} "
                                                  f"{r['orig']} -> {r.get('refined', r['orig'])}"
                                                  + (f" ({r['latency_s']:.1f}s)" if "latency_s" in r else ""),
                                                  flush=True)
                                if rnote:
                                    desc += f" [{rnote}]"
                            if guard and name not in ("done", "fail", "ask_human"):
                                guard(name, args, obs, marks)            # raises BadAction("BLOCKED: ...") to veto
                            if name in ("ask_human", "done", "fail"):
                                node["method"] = "none"
                            if name == "ask_human":
                                q, ta = str(args.get("question", "")), time.time()
                                ans = hitl.ask(q, args.get("options")) if hitl is not None else None
                                if not ans and hitl is not None and hitl.stop_requested():
                                    raise KeyboardInterrupt("the user clicked Stop")
                                if ans:
                                    outcome = f"USER ANSWERED: {ans}"
                                elif hitl is None:
                                    outcome = "no human available - decide yourself, or call fail if you cannot"
                                else:
                                    outcome = ("the user did not answer - decide yourself (choose the safest "
                                               "option), or call fail if you cannot")
                                if hitl is not None:
                                    asked = {"type": "human", "kind": "ask", "reason": q, "answer": ans,
                                             "outcome": "answered" if ans else "no_answer", "how": "ask_human",
                                             "waited_s": round(time.time() - ta, 1)}
                                executed = True
                                keys.append(key)
                            elif name not in ("done", "fail"):
                                say(node["reason"] or name)
                                before = obs["image"]
                                hov = {}
                                try:                                     # the router picks the method
                                    outcome = router.execute(name, args, marks, obs, info=hov)
                                except BadAction as e:
                                    if hov:
                                        node["hover"] = hov
                                    if isinstance(e, Covered):
                                        obs = None                       # something popped up: look again
                                    raise
                                except Exception as e:
                                    if type(e).__name__ == "KillSwitch":
                                        raise
                                    if type(e).__name__ == "HumanTakeover" and hitl is not None:
                                        took_over = True                 # nothing was executed
                                        raise BadAction("interrupted: the user took over the mouse") from None
                                    if router_mod.is_mouse_denied(e):    # the gate already waited / user said
                                        raise                            # Stop: the task ends ("stopped")
                                    executed = True
                                    obs = None
                                    raise BadAction(f"executor error: {e}") from None
                                finally:
                                    node["method"] = router.method
                                executed = True
                                if hov:
                                    node["hover"] = hov
                                keys.append(key)
                                if lk:
                                    loops.append(lk)
                                if not router.keeps_screen(name):
                                    obs = None                           # screen changed (or might have)
                                if name in VERIFY or name == "scroll":
                                    if settle:
                                        time.sleep(SETTLE.get(name, SETTLE_DEFAULT))
                                    obs = observe()
                                    d = diff_ratio(before, obs["image"])
                                    tid = args.get("id")                 # small changes (caret, typed text) get lost
                                    box = marks[int(tid)]["box"] if str(tid).isdigit() and int(tid) in marks \
                                        else last_box                    # in a full-screen diff -> also diff locally
                                    if box and before.size == obs["image"].size:
                                        W, H = before.size
                                        c = (max(0, box[0] - 120), max(0, box[1] - 120),
                                             min(W, box[2] + 120), min(H, box[3] + 120))
                                        d = max(d, diff_ratio(before.crop(c), obs["image"].crop(c)))
                                        if str(tid).isdigit() and marks.get(int(tid), {}).get("type") in TEXT_TYPES:
                                            last_box = box               # typing lands in the last TEXT box, not
                                                                         # in a popup item clicked after it
                                    node["diff"] = round(d, 4)
                                    if node["diff"] < DIFF_MIN:
                                        tgt = marks.get(int(tid)) if str(tid).isdigit() else None
                                        if name == "scroll":
                                            outcome = "nothing moved (maybe already at the end)"
                                        elif name == "click" and ((tgt and tgt.get("type") in ("Edit", "Document"))
                                                                  or (not tgt and not obs.get("background")
                                                                      and _is_textbox_at(args, marks, obs))):
                                            outcome = "ok: the text box is now active (no visible change is normal) - next, use type_text, do NOT click it again"
                                        else:
                                            raise BadAction("it did not change the screen")
                            node["ok"] = True
                            if hitl is not None and executed and name != "ask_human":
                                hitl.note_agent_window(obs)      # the window the agent really works in
                        except BadAction as e:
                            node["fail_reason"] = str(e)

                    per_level[f"L{li + 1}"]["attempts"] += 1
                    per_level[f"L{li + 1}"]["ok"] += node["ok"]
                    if router.after_attempt(node, executed, name, args, marks):
                        obs = None                           # background: switched between tab and real screen
                    confirms = router.take_confirms()
                    if confirms:
                        node["confirm"] = confirms
                    log.write(json.dumps(node, ensure_ascii=False, default=str) + "\n")
                    log.flush()
                    diff = f", diff {node['diff']:.3f}" if node["diff"] is not None else ""
                    status = "ok" if node["ok"] else f"FAIL: {node['fail_reason']}"
                    print(f"[step {step} L{li + 1}] {desc or '(no action)'} {status} ({node['latency_s']:.1f}s{diff})",
                          flush=True)
                    if executed:                             # the prompt history only shows what really happened
                        history.append(f"{step}. {desc} -> " + (outcome if node["ok"] else f"FAILED: {node['fail_reason']}"))
                    if asked:
                        human(step, asked)
                    if took_over:                            # the human's turn replaces the rest of this step
                        hres = takeover(obs)
                        break
                    if node["ok"]:
                        final = (li, name, args)
                        break
                    soft = soft and node["fail_reason"] == "it did not change the screen"
                    if hitl is not None and node["fail_reason"].startswith("going in circles"):
                        break                                # a loop: don't burn more levels, ask the human now
                    feedback = (f"Your last attempt ({desc or 'reply'}) failed: {node['fail_reason']}. "
                                "Try something else.")
                if hres is not None or final is not None or hitl is None or (soft and soft_streak == 0):
                    break                                # (soft no-change twice in a row with hitl -> ask the human)
                if obs is None:                              # every level really failed -> ask the human
                    obs = observe()
                hres = hitl.handle_stuck(goal or "the next step of the task", node["fail_reason"], obs, task, history)
                if hres.get("outcome") == "retry":           # "Try again" / a free-text hint -> L1 again
                    human(step, hres)
                    feedback = hres.get("feedback") or "The user asked you to try again."
                    if str(node.get("fail_reason", "")).startswith("going in circles"):
                        loops.clear()                    # a fresh try may repeat the first moves (open the menu...)
                        keys = []                        # but the step that failed must be done DIFFERENTLY
                        feedback += (" Last time the same clicks led nowhere. Redo the steps, but do the one that "
                                     "failed in a different way: press_and_hold if the page mentions holding, "
                                     "double_click, or the keyboard.")
                    start, soft, hres = 0, True, None
                    continue
                break

            if hres is not None:                         # a human turn took the rest of this step
                end = human(step, hres)
                if end:
                    result = end
                    break
                if hres.get("kind") == "stuck" and hres.get("outcome") != "done":
                    result = ("fail", f"step {step}: every level failed ({node['fail_reason']}); "
                                      f"{hres.get('note') or 'no answer from the user'}")
                    break
                carry = hres.get("feedback", "")
                if hres.get("screen_changed", True):
                    obs, keys, last_box = None, [], None
                    loops.clear()
                continue
            if final is None and soft:                   # nothing broke, the screen just didn't visibly change:
                soft_streak += 1
                history.append(f"{step}. (no visible effect at any level; continuing)")   # note it, keep going
                print(f"[step {step}] no visible change at any level -> continuing", flush=True)
                continue
            if final is None:
                result = ("fail", f"step {step}: every level failed ({node['fail_reason']})")
                break
            li, name, args = final
            soft_streak = 0
            streak = streak + 1 if li > start else 0
            if name == "done":
                result = ("done", str(args.get("summary", "")))
                say("Done!")
                break
            if name == "fail":
                result = ("fail", str(args.get("reason", "")))
                say("I couldn't do it")
                break
    except BaseException as e:
        result = end_result(e)
        if result[0] == "error":
            raise
        say("Stopped")
    finally:
        log.close()
        router.close()
        if hitl is not None:
            try:
                executor.takeover_handler = None
                if router.mode == "background":
                    del hitl.handover
            except Exception:  # noqa: BLE001
                pass
        summary = {"task": task, "result": result[0], "message": result[1], "steps": steps,
                   "attempts_per_level": per_level, "levels": [{"model": m, "thinking": t} for m, t in levels],
                   "total_time_s": round(time.time() - t_start, 1)}
        if humans:
            summary["human_turns"] = humans
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"result: {result[0]} - {result[1]} ({steps} steps, {summary['total_time_s']}s)", flush=True)
        if not keep_images:
            cleanup_images(run_dir)
    if cursor and result[0] == "done":
        time.sleep(1.5)                                  # let the user see "Done!"
    return result


# ---------------------------------------------------------------- entry point
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--no-cursor", action="store_true")
    ap.add_argument("--max-steps", type=int, default=25)
    a = ap.parse_args()
    from rbot.desktop.actions import Executor

    if a.no_cursor:
        ex = Executor()
        try:
            run_task(a.task, ex, None, a.max_steps)
        finally:
            ex.stop()
        return

    import signal
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from rbot.desktop.cursor_overlay import AgentCursor

    app = QApplication(sys.argv)
    cur = AgentCursor()
    cur.show()
    ex = Executor(cursor=cur)
    stop = threading.Event()
    worker = threading.Thread(target=run_task, args=(a.task, ex, cur, a.max_steps), kwargs={"stop": stop},
                              daemon=True)

    def on_sigint(*_):
        if stop.is_set():
            os._exit(1)
        stop.set()
        print("stopping after the current call... (Ctrl+C again to force quit)", flush=True)
        cur.post("say", "Stopping", False)

    signal.signal(signal.SIGINT, on_sigint)
    poll = QTimer()                                      # also gives Python a chance to handle Ctrl+C
    poll.timeout.connect(lambda: worker.is_alive() or app.quit())
    poll.start(200)
    worker.start()
    app.exec()
    ex.stop()


if __name__ == "__main__":
    main()
