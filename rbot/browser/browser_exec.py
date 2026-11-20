"""Background actions in the buddy browser (PLAN phase 3): the agent clicks / types / scrolls inside one tab via the
Chrome DevTools Protocol, so the real mouse never moves and the window doesn't need to be in front. The user keeps
their mouse and keyboard.

  ex = BrowserExecutor()                     # works in the most recently activated buddy-browser tab
  obs = ex.observe()                         # = browser_dom.observe_tab(ex.target_id): same schema as perception
  ex.click(x, y)                             # x, y in IMAGE px of that observation (like actions.Executor)

Mirrors actions.Executor (kind="browser"): click, double_click, right_click, move, drag, scroll(x, y, clicks),
type_text, press_key, hotkey, wait, hold(x, y, seconds), stop, _check, takeover_handler, human_turn, before_press.
Coordinates in = image px of browser_dom.observe_tab() -> CSS px (/ devicePixelRatio) for Input.dispatch*.

What it shows: a grape "ghost" buddy drawn INSIDE the page (closed shadow root, pointer-events:none, top layer)
glides to each target (ease-out 250-400 ms) and plays a click ripple; hold fills a ring; typing shows a caret pill.
observe_tab() hides it while it takes the screenshot, so the model never sees it; it is never a DOM mark.

Background-mode specifics (documented for the integration):
- before_press (the desktop hover check: UIA at SCREEN coords) is NOT called - the coords are tab image px. Use
  ex.hit_test(x, y) for a DOM hover check instead.
- Browser-UI shortcuts don't reach the page through CDP, so they are emulated: ctrl+l / alt+d / f6 / ctrl+t open an
  in-page "buddy address bar" (visible in screenshots; type, then Enter navigates this tab, Esc cancels);
  f5 / ctrl+r reload; alt+left / alt+right history; ctrl+w / ctrl+f / desktop keys (win, alt+tab...) raise.
- <select> clicks open an in-page option list (role=option marks) instead of the native popup (a native popup
  would float over the user's app). Right-click suppresses the browser's own context menu (page menus still work).
- JS alert/confirm/prompt: pending_dialog() / answer_dialog(accept, text). observe_tab() paints it with OK/Cancel
  marks; clicking those, Enter or Escape answers it. Typing while a prompt is open fills its text.
- Downloads go to download_dir (default ~/Downloads/Buddy) without a Save-as window: last_downloads().
- Permission prompts (camera, location, notifications...) are left to the page/browser: they show as browser
  bubbles that only UIA/HITL can see - the loop hands over (bring_to_front) when the page waits on one.
- Ctrl+Alt+Q (global hotkey, keyboard hook only) raises KillSwitch; a stop Event raises KeyboardInterrupt; a REAL
  click inside a buddy-browser window while the agent works is a takeover (HumanTakeover) when a takeover_handler
  is set. Clicks/typing anywhere else are the user's own business.
- The window must not be minimised (rendering stops); send_to_back() restores + puts it behind other windows.
"""
import ctypes
import json
import math
import threading
import time
from ctypes import wintypes
from pathlib import Path

from rbot.browser import browser_dom as bd

try:                                                    # same exception types the agent loop knows
    from rbot.desktop.actions import HumanTakeover, KillSwitch       # (importing actions also sets per-monitor DPI awareness)
except Exception:  # noqa: BLE001
    class KillSwitch(Exception):
        """User pressed Ctrl+Alt+Q."""

    class HumanTakeover(Exception):
        """The user grabbed the buddy browser while the agent was working."""

STEP = 0.05                       # pause between press / release so pages register each event
GLIDE_MIN_MS, GLIDE_MAX_MS = 250, 400
WHEEL_PX = 120                    # one wheel "click"
TAKEOVER_GRACE_S = 2.0
DOWNLOAD_DIR = Path.home() / "Downloads" / "rbot-ai"
_u32 = ctypes.WinDLL("user32")
_u32.WindowFromPoint.restype = wintypes.HWND
_u32.WindowFromPoint.argtypes = [wintypes.POINT]
_u32.GetAncestor.restype = wintypes.HWND
_u32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_u32.GetForegroundWindow.restype = wintypes.HWND


# ============================================================================ pure helpers (unit-tested)
def to_css(x, y, scale):
    """Image px (device px of the tab screenshot) -> CSS px for Input.dispatchMouseEvent."""
    s = float(scale or 1.0)
    return round(x / s, 2), round(y / s, 2)


def glide_ms(x0, y0, x1, y1):
    """Ghost glide duration: short hops 250 ms, long flights 400 ms."""
    if x0 is None:
        return GLIDE_MIN_MS
    d = math.hypot(x1 - x0, y1 - y0)
    if d < 3:                                           # already there: no flight
        return 0
    return int(max(GLIDE_MIN_MS, min(GLIDE_MAX_MS, GLIDE_MIN_MS + 0.15 * d)))


MOD_BITS = {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}
_ALIASES = {"esc": "escape", "return": "enter", "del": "delete", "control": "ctrl", "ctl": "ctrl", "win": "meta",
            "windows": "meta", "super": "meta", "cmd": "meta", "command": "meta", "option": "alt", "pgup": "page_up",
            "pageup": "page_up", "pgdn": "page_down", "pagedown": "page_down", "arrowup": "up", "arrowdown": "down",
            "arrowleft": "left", "arrowright": "right", "bksp": "backspace", "back_space": "backspace",
            "ins": "insert", "spacebar": "space", "apps": "menu", "contextmenu": "menu", "capslock": "caps_lock",
            "prtsc": "print_screen", "printscreen": "print_screen", "browserback": "browser_back",
            "browserforward": "browser_forward"}
# name -> (key, code, windowsVirtualKeyCode, text)
SPECIAL = {
    "enter": ("Enter", "Enter", 13, "\r"), "tab": ("Tab", "Tab", 9, None), "escape": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None), "delete": ("Delete", "Delete", 46, None),
    "insert": ("Insert", "Insert", 45, None), "space": (" ", "Space", 32, " "),
    "up": ("ArrowUp", "ArrowUp", 38, None), "down": ("ArrowDown", "ArrowDown", 40, None),
    "left": ("ArrowLeft", "ArrowLeft", 37, None), "right": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None), "end": ("End", "End", 35, None),
    "page_up": ("PageUp", "PageUp", 33, None), "page_down": ("PageDown", "PageDown", 34, None),
    "ctrl": ("Control", "ControlLeft", 17, None), "shift": ("Shift", "ShiftLeft", 16, None),
    "alt": ("Alt", "AltLeft", 18, None), "meta": ("Meta", "MetaLeft", 91, None),
    "caps_lock": ("CapsLock", "CapsLock", 20, None), "menu": ("ContextMenu", "ContextMenu", 93, None),
    **{f"f{i}": (f"F{i}", f"F{i}", 111 + i, None) for i in range(1, 13)},
}
_SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
            "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
            "~": "`"}
_PUNCT = {"-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219), "]": ("BracketRight", 221),
          "\\": ("Backslash", 220), ";": ("Semicolon", 186), "'": ("Quote", 222), ",": ("Comma", 188),
          ".": ("Period", 190), "/": ("Slash", 191), "`": ("Backquote", 192)}


def norm_key(name):
    """'Ctrl' -> 'ctrl', 'Esc' -> 'escape', 'PageDown' -> 'page_down', 'A' -> 'a'."""
    n = str(name).strip()
    if len(n) == 1:
        return n.lower() if n.isalpha() else n
    n = n.lower().replace(" ", "_").replace("-", "_")
    return _ALIASES.get(n.replace("_", ""), _ALIASES.get(n, n))


def char_info(ch):
    """US-layout key for one printable character -> (key, code, vk, text, needs_shift) or None."""
    if "a" <= ch <= "z":
        return ch, "Key" + ch.upper(), ord(ch.upper()), ch, False
    if "A" <= ch <= "Z":
        return ch, "Key" + ch, ord(ch), ch, True
    if "0" <= ch <= "9":
        return ch, "Digit" + ch, ord(ch), ch, False
    if ch == " ":
        return " ", "Space", 32, " ", False
    if ch in _PUNCT:
        code, vk = _PUNCT[ch]
        return ch, code, vk, ch, False
    if ch in _SHIFTED:
        code, vk = _PUNCT.get(_SHIFTED[ch]) or ("Digit" + _SHIFTED[ch], ord(_SHIFTED[ch]))
        return ch, code, vk, ch, True
    return None


def key_info(name):
    """Key name ('enter', 'a', 'F5', '/') -> (key, code, vk, text). KeyError for unknown names."""
    n = norm_key(name)
    if n in SPECIAL:
        return SPECIAL[n]
    if len(n) == 1:
        ci = char_info(n)
        if ci:
            return ci[:4]
    raise KeyError(f"unknown key {name!r}")


def _ev(kind, info, mods):
    key, code, vk, text = info
    if kind == "down":
        typed = bool(text) and not (mods & (MOD_BITS["ctrl"] | MOD_BITS["alt"] | MOD_BITS["meta"]))
        p = {"type": "keyDown" if typed else "rawKeyDown"}
    else:
        p, typed = {"type": "keyUp"}, False
    if mods & MOD_BITS["shift"] and len(key) == 1 and key.isalpha():
        key = key.upper()
        text = text.upper() if text else text
    p.update(key=key, code=code, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=mods)
    if code.endswith("Left") and key in ("Control", "Shift", "Alt", "Meta"):
        p["location"] = 1
    if typed:
        p["text"] = p["unmodifiedText"] = text
    return p


def combo_events(names):
    """['ctrl', 'a'] -> the Input.dispatchKeyEvent params: modifiers down (cumulative), keys down, keys up in
    reverse order. A single name is one key press."""
    names = [names] if isinstance(names, str) else list(names)
    keys = [norm_key(n) for n in names]
    mods_keys = [k for k in keys if k in MOD_BITS]
    main = [k for k in keys if k not in MOD_BITS]
    downs, ups, mods = [], [], 0
    for k in mods_keys:
        mods |= MOD_BITS[k]
        downs.append(_ev("down", SPECIAL[k], mods))
    for k in main:
        downs.append(_ev("down", key_info(k), mods))
    for k in reversed(main):
        ups.append(_ev("up", key_info(k), mods))
    for k in reversed(mods_keys):
        mods &= ~MOD_BITS[k]
        ups.append(_ev("up", SPECIAL[k], mods))
    return downs + ups


def char_events(ch):
    """Events that type one character like a real US keyboard; None -> use Input.insertText (other scripts)."""
    if ch == "\n":
        return combo_events(["enter"])
    if ch == "\t":
        return combo_events(["tab"])
    ci = char_info(ch)
    if ci is None:
        return None
    key, code, vk, text, shift = ci
    info = (key, code, vk, text)
    if not shift:
        return [_ev("down", info, 0), _ev("up", info, 0)]
    m = MOD_BITS["shift"]
    return [_ev("down", SPECIAL["shift"], m), _ev("down", info, m), _ev("up", info, m),
            _ev("up", SPECIAL["shift"], 0)]


# Browser-UI shortcuts never reach the page through CDP: emulated (address bar, reload, history) or refused.
_BROWSER_KEYS = {
    frozenset({"ctrl", "l"}): "address", frozenset({"alt", "d"}): "address", frozenset({"f6"}): "address",
    frozenset({"ctrl", "t"}): "address_new", frozenset({"ctrl", "n"}): "address_new",
    frozenset({"ctrl", "r"}): "reload", frozenset({"f5"}): "reload", frozenset({"ctrl", "f5"}): "reload_hard",
    frozenset({"ctrl", "shift", "r"}): "reload_hard",
    frozenset({"alt", "left"}): "back", frozenset({"browser_back"}): "back",
    frozenset({"alt", "right"}): "forward", frozenset({"browser_forward"}): "forward",
    frozenset({"ctrl", "w"}): "refuse", frozenset({"ctrl", "f4"}): "refuse", frozenset({"ctrl", "f"}): "refuse",
    frozenset({"ctrl", "shift", "t"}): "refuse", frozenset({"ctrl", "tab"}): "refuse",
    frozenset({"ctrl", "shift", "tab"}): "refuse", frozenset({"alt", "f4"}): "refuse",
    frozenset({"alt", "tab"}): "desktop", frozenset({"meta"}): "desktop", frozenset({"f11"}): "refuse",
    frozenset({"ctrl", "j"}): "refuse", frozenset({"ctrl", "h"}): "refuse", frozenset({"ctrl", "d"}): "refuse",
    frozenset({"f12"}): "refuse", frozenset({"ctrl", "shift", "i"}): "refuse", frozenset({"ctrl", "p"}): "refuse",
}


def browser_shortcut(names):
    """-> 'address' | 'address_new' | 'reload' | 'reload_hard' | 'back' | 'forward' | 'refuse' | 'desktop' | None."""
    names = [names] if isinstance(names, str) else list(names)
    keys = frozenset(norm_key(n) for n in names)
    hit = _BROWSER_KEYS.get(keys)
    if hit is None and "meta" in keys:                   # win+r, win+d, win+e ...: desktop, not this tab
        return "desktop"
    return hit


def dialog_answer_at(marks, x, y):
    """Click at image px (x, y) on a painted JS dialog -> True (OK) / False (Cancel) / 'edit' / None."""
    for m in marks or []:
        b = m["box"]
        if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            if m.get("type") == "Edit":
                return "edit"
            if "answer" in m:
                return bool(m["answer"])
    return None


def nav_url(text):
    """What the buddy address bar does with typed text: a URL as is, a host -> https://, else a web search."""
    t = (text or "").strip()
    if not t:
        return None
    if "://" in t or t.startswith(("about:", "data:", "file:")):
        return t
    if " " not in t and ("." in t or t.startswith("localhost") or t.split(":")[0].replace(".", "").isdigit()):
        return ("http://" if t.startswith(("localhost", "127.")) else "https://") + t
    import urllib.parse
    return "https://www.bing.com/search?q=" + urllib.parse.quote_plus(t)


# ============================================================================ the in-page ghost (JS)
GHOST_JS = r"""
(() => {
  const V = 5;
  const old = window.__buddyGhost;
  if (old && old.v === V) return 'ok';
  if (old && old.remove) { try { old.remove(); } catch (e) {} }
  const NS = 'http://www.w3.org/2000/svg', C1 = '#8B5CF6', C2 = '#EC4899', INK = '#2A1240';
  const CSS = `
    .cur { position:absolute; left:0; top:0; will-change:transform; opacity:0;
           transition: transform var(--d,320ms) cubic-bezier(.33,1,.68,1), opacity .22s ease-out; }
    .cur.on { opacity:1 }
    .bob { animation: bob 2.6s ease-in-out infinite; }
    @keyframes bob { 0%,100% { transform: translate(0,0) } 50% { transform: translate(0,-1.6px) } }
    .lean { transform-origin: 0 0; transition: transform .22s ease-out; }
    .sq { transform-origin: 0 0; }
    .sq.press { animation: squish .34s cubic-bezier(.3,1.5,.5,1); }
    @keyframes squish { 0% { transform: scale(1) } 28% { transform: scale(1.12,.82) } 100% { transform: scale(1) } }
    svg.body { position:absolute; left:-3px; top:-3px; width:40px; height:40px; overflow:visible;
               filter: drop-shadow(1px 2.5px 2px rgba(42,18,64,.34)) drop-shadow(0 0 1px rgba(42,18,64,.25)); }
    .happy { display:none } .joy .happy { display:inline } .joy .eyes { display:none }
    .rip { position:absolute; left:0; top:0; }
    .rip i, .rip b { position:absolute; border-radius:50%; box-sizing:border-box; }
    .rip i { left:-38px; top:-38px; width:76px; height:76px; border:3px solid ${C1};
             animation: ring .7s cubic-bezier(.15,.8,.25,1) forwards; }
    .rip i + i { border-color:${C2}; animation-delay:.08s; opacity:0; }
    .rip b { left:-16px; top:-16px; width:32px; height:32px;
             background: radial-gradient(circle, rgba(236,72,153,.55), rgba(139,92,246,.30) 58%, rgba(139,92,246,0) 72%);
             animation: dot .55s ease-out forwards; }
    @keyframes ring { from { transform:scale(.1); opacity:1 } to { transform:scale(1); opacity:0 } }
    @keyframes dot { from { transform:scale(.4); opacity:1 } to { transform:scale(1.6); opacity:0 } }
    svg.ring { position:absolute; left:-30px; top:-30px; width:60px; height:60px; overflow:visible; opacity:0;
               transform: rotate(-90deg); transition: opacity .15s; }
    .holding svg.ring { opacity:1 }
    .holding .sq { transform: scale(1.06,.9); transition: transform .2s; }
    .caret { position:absolute; left:30px; top:30px; height:20px; padding:0 7px; border-radius:10px; background:#fff;
             border:1.5px solid ${C1}; box-shadow:0 2px 7px rgba(42,18,64,.28); display:none; align-items:center; gap:3px; }
    .typing .caret { display:flex }
    .caret i { width:2px; height:12px; border-radius:1px; background:linear-gradient(${C1},${C2}); margin-right:2px;
               animation: blink 1s steps(1) infinite; }
    .caret b { width:4px; height:4px; border-radius:50%; background:${C1}; opacity:.35; animation: dots 1s infinite; }
    .caret b:nth-of-type(2) { animation-delay:.15s } .caret b:nth-of-type(3) { animation-delay:.3s }
    .caret.kp { animation: kp .16s ease-out; }
    @keyframes kp { 50% { transform: scale(1.18) } }
    @keyframes blink { 50% { opacity:0 } }
    @keyframes dots { 0%,60%,100% { opacity:.3; transform:translateY(0) } 30% { opacity:1; transform:translateY(-2px) } }
    .addr { position:absolute; left:50vw; top:12px; transform:translateX(-50%); width:min(760px, calc(100vw - 32px));
            box-sizing:border-box; background:#fff; border:2px solid ${C1}; border-radius:12px; padding:8px 12px 6px;
            box-shadow:0 10px 30px rgba(42,18,64,.30); font:15px/1.35 'Segoe UI', system-ui, sans-serif; color:#1d1430; }
    .addr .row { display:flex; align-items:center; gap:9px; }
    .addr .ic { flex:none; width:18px; height:18px; border-radius:50%; background:linear-gradient(135deg,${C1},${C2}); }
    .addr .t { flex:1; white-space:pre; overflow:hidden; }
    .addr .sel { background:#c7d2fe; }
    .addr .cr { display:inline-block; width:1.5px; height:18px; background:#1d1430; vertical-align:-4px;
                animation: blink 1s steps(1) infinite; }
    .addr .hint { font-size:11.5px; color:#6b5b87; margin-top:2px; }
  `;
  let host = null, root = null, cur = null, lean = null, sq = null, arc = null, caret = null, pos = null;
  let addrHost = null, addrRoot = null, armed = '', armT = 0, capT = 0, joyT = 0, typT = 0, picker = null;
  const mk = (tag, cls, parent) => { const e = document.createElement(tag); if (cls) e.className = cls; if (parent) parent.appendChild(e); return e; };
  const sv = (tag, a, parent) => { const e = document.createElementNS(NS, tag); for (const k in a) e.setAttribute(k, a[k]); if (parent) parent.appendChild(e); return e; };
  function sheet(r) {
    try { const s = new CSSStyleSheet(); s.replaceSync(CSS); r.adoptedStyleSheets = [s]; }
    catch (e) { const st = document.createElement('style'); st.textContent = CSS; r.appendChild(st); }
  }
  function mkHost(kind) {
    const h = document.createElement('buddy-ghost');
    h.setAttribute('data-buddy-ghost', kind);
    h.setAttribute('aria-hidden', 'true');
    const p = {position: 'fixed', left: '0', top: '0', right: 'auto', bottom: 'auto', width: '0', height: '0',
               margin: '0', padding: '0', border: '0', overflow: 'visible', background: 'transparent',
               'pointer-events': 'none', 'z-index': '2147483647', display: 'block', inset: '0 auto auto 0',
               'min-width': '0', 'min-height': '0', 'max-width': 'none', 'max-height': 'none'};
    for (const k in p) h.style.setProperty(k, p[k], 'important');
    try { h.setAttribute('popover', 'manual'); } catch (e) {}
    (document.documentElement || document.body).appendChild(h);
    const r = h.attachShadow({mode: 'closed'});
    sheet(r);
    return [h, r];
  }
  function top(h) {                         // top layer: above modal <dialog>s and popovers too
    if (!h || !h.showPopover) return;
    try { if (h.matches(':popover-open')) h.hidePopover(); h.showPopover(); } catch (e) {}
  }
  function build() {
    [host, root] = mkHost('cursor');
    cur = mk('div', 'cur', root);
    const bob = mk('div', 'bob', cur);
    lean = mk('div', 'lean', bob);
    sq = mk('div', 'sq', lean);
    const s = sv('svg', {class: 'body', viewBox: '-3 -3 48 48'}, sq);
    const defs = sv('defs', {}, s);
    const g = sv('linearGradient', {id: 'bg', x1: '0', y1: '0', x2: '42', y2: '42', gradientUnits: 'userSpaceOnUse'}, defs);
    sv('stop', {offset: '0', 'stop-color': C1}, g); sv('stop', {offset: '1', 'stop-color': C2}, g);
    // teardrop: tip at (0,0) flowing into a round head (centre 24,24, r 17) - same shape as the desktop buddy
    sv('path', {d: 'M0 0 L28.37 7.57 A17 17 0 1 1 7.57 28.37 Z', fill: 'url(#bg)', stroke: '#fff',
                'stroke-width': '2.6', 'stroke-linejoin': 'round'}, s);
    sv('ellipse', {cx: '18', cy: '15', rx: '6', ry: '3.2', fill: 'rgba(255,255,255,.32)'}, s);
    const f = sv('g', {transform: 'translate(24 25)'}, s);
    const eyes = sv('g', {class: 'eyes'}, f), happy = sv('g', {class: 'happy'}, f);
    for (const x of [-5.5, 5.5]) {
      sv('ellipse', {cx: x, cy: -1.5, rx: 2.1, ry: 2.9, fill: INK}, eyes);
      sv('circle', {cx: x - 0.7, cy: -2.6, r: 0.85, fill: '#fff'}, eyes);
      sv('path', {d: `M${x - 2.4} -0.3 Q${x} -4.1 ${x + 2.4} -0.3`, fill: 'none', stroke: INK, 'stroke-width': 1.8, 'stroke-linecap': 'round'}, happy);
      sv('ellipse', {cx: x * 1.78, cy: 3.4, rx: 2.8, ry: 1.7, fill: 'rgba(255,140,190,.62)'}, f);
    }
    sv('path', {d: 'M-2.6 3.6 Q0 6.5 2.6 3.6', fill: 'none', stroke: INK, 'stroke-width': 1.6, 'stroke-linecap': 'round', class: 'eyes'}, f);
    sv('path', {d: 'M-2.8 3.4 L2.8 3.4 Q0 8.2 -2.8 3.4 Z', fill: INK, class: 'happy'}, f);
    const r = sv('svg', {class: 'ring', viewBox: '-30 -30 60 60'}, cur);
    sv('circle', {r: 22, fill: 'none', stroke: 'rgba(139,92,246,.22)', 'stroke-width': 5}, r);
    arc = sv('circle', {r: 22, fill: 'none', stroke: C2, 'stroke-width': 5, 'stroke-linecap': 'round',
                        'stroke-dasharray': '138.2', 'stroke-dashoffset': '138.2', class: 'arc'}, r);
    caret = mk('div', 'caret', cur); mk('i', '', caret); mk('b', '', caret); mk('b', '', caret); mk('b', '', caret);
  }
  function ensure() { if (!host || !host.isConnected) { build(); pos = null; } }
  function place(x, y, ms) {
    if (!ms) { cur.style.transition = 'none'; cur.style.transform = `translate(${x}px,${y}px)`; void cur.offsetWidth; cur.style.transition = ''; }
    else { cur.style.setProperty('--d', ms + 'ms'); cur.style.transform = `translate(${x}px,${y}px)`; }
  }
  function ripple(x, y) {
    const r = mk('div', 'rip', root); r.style.transform = `translate(${x}px,${y}px)`;
    mk('b', '', r); mk('i', '', r); mk('i', '', r);
    setTimeout(() => r.remove(), 900);
  }
  function joy(ms) { cur.classList.add('joy'); clearTimeout(joyT); joyT = setTimeout(() => cur.classList.remove('joy'), ms); }
  function closePicker() { if (picker) { picker.remove(); picker = null; } }
  function openPicker(sel) {
    closePicker();
    const r = sel.getBoundingClientRect(), VH = innerHeight;
    const p = document.createElement('div');
    p.setAttribute('data-buddy-picker', '1'); p.setAttribute('role', 'listbox');
    p.setAttribute('aria-label', 'Options of ' + ((sel.labels && sel.labels[0] && sel.labels[0].innerText) || sel.name || 'list'));
    const below = VH - r.bottom - 8, above = r.top - 8, down = below >= 160 || below >= above;
    const st = {position: 'fixed', left: Math.max(4, r.left) + 'px', right: 'auto', top: 'auto', bottom: 'auto',
                width: 'auto', height: 'auto', 'min-width': Math.max(160, r.width) + 'px',
                'max-height': Math.max(80, Math.min(340, down ? below : above)) + 'px', overflow: 'auto',
                background: '#fff', border: '1.5px solid ' + C1, 'border-radius': '9px', padding: '4px',
                'box-shadow': '0 10px 28px rgba(42,18,64,.28)', 'z-index': '2147483646', margin: '0',
                font: "14px/1.3 'Segoe UI', system-ui, sans-serif", color: '#1d2330', 'box-sizing': 'border-box'};
    st[down ? 'top' : 'bottom'] = (down ? r.bottom + 4 : VH - r.top + 4) + 'px';
    for (const k in st) p.style.setProperty(k, st[k], 'important');
    [...sel.options].forEach((o, i) => {
      if (o.hidden) return;
      const b = document.createElement('div');
      b.setAttribute('role', 'option'); b.setAttribute('aria-selected', String(o.selected)); b.tabIndex = 0;
      b.textContent = (o.text || o.value || '(empty)') + (o.selected ? '  ✓' : '');
      const bs = {display: 'block', padding: '7px 10px', 'border-radius': '6px', cursor: 'pointer', 'white-space': 'nowrap',
                  background: o.selected ? '#ede9fe' : 'transparent', opacity: o.disabled ? '.45' : '1'};
      for (const k in bs) b.style.setProperty(k, bs[k], 'important');
      if (o.disabled) b.setAttribute('aria-disabled', 'true');
      b.addEventListener('mouseenter', () => { if (!o.disabled && !o.selected) b.style.setProperty('background', '#f5f3ff', 'important'); });
      b.addEventListener('mouseleave', () => { if (!o.selected) b.style.setProperty('background', 'transparent', 'important'); });
      b.addEventListener('click', ev => {
        ev.preventDefault(); ev.stopPropagation();
        if (o.disabled) return;
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event('input', {bubbles: true}));
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        closePicker(); try { sel.focus(); } catch (e) {}
      });
      p.appendChild(b);
    });
    try { p.setAttribute('popover', 'manual'); } catch (e) {}
    document.documentElement.appendChild(p);
    top(p);
    try { sel.focus(); } catch (e) {}
    picker = p;
  }
  window.addEventListener('mousedown', e => {
    if (!armed) return;
    const t = e.composedPath ? e.composedPath()[0] : e.target;
    if (picker && !(t instanceof Node && picker.contains(t))) closePicker();
    if (armed !== 'left') return;
    const sel = t && t.closest ? t.closest('select') : null;
    if (sel && !sel.multiple && !(sel.size > 1) && !sel.disabled) { e.preventDefault(); openPicker(sel); }
  }, true);
  window.addEventListener('contextmenu', e => { if (armed === 'right' && !e.defaultPrevented) e.preventDefault(); });
  window.addEventListener('keydown', e => { if (picker && e.key === 'Escape') closePicker(); }, true);
  const G = window.__buddyGhost = {
    v: V,
    pos: () => pos,
    move(x, y, ms, fx, fy) {
      ensure(); top(host);
      if (!pos) {                              // first appearance: pop in a little off the target, then glide
        const sx = (fx === undefined || fx === null) ? x - 64 : fx, sy = (fy === undefined || fy === null) ? y + 46 : fy;
        place(sx, sy, 0); pos = [sx, sy];
      }
      cur.classList.add('on');
      const dx = x - pos[0];
      lean.style.transform = `rotate(${Math.max(-12, Math.min(12, dx / 25))}deg)`;
      setTimeout(() => { lean.style.transform = ''; }, Math.max(80, ms * 0.65));
      place(x, y, ms); pos = [x, y];
      return ms;
    },
    click(n) {
      ensure(); if (!pos) return;
      for (let i = 0; i < (n || 1); i++) setTimeout(() => ripple(pos[0], pos[1]), i * 130);
      sq.classList.remove('press'); void sq.offsetWidth; sq.classList.add('press'); joy(520);
    },
    hold(ms) {
      ensure(); if (!pos) return;
      cur.classList.add('holding'); arc.style.transition = 'none'; arc.style.strokeDashoffset = '138.2';
      void arc.getBoundingClientRect();
      arc.style.transition = `stroke-dashoffset ${ms}ms linear`; arc.style.strokeDashoffset = '0';
    },
    holdEnd() { if (!cur) return; cur.classList.remove('holding'); if (pos) ripple(pos[0], pos[1]); joy(600); },
    typing(on) {
      ensure(); cur.classList.add('on'); clearTimeout(typT);
      if (on) cur.classList.add('typing'); else typT = setTimeout(() => cur.classList.remove('typing'), 450);
    },
    key() { if (!caret) return; caret.classList.remove('kp'); void caret.offsetWidth; caret.classList.add('kp'); },
    capture(hide) {
      clearTimeout(capT);
      for (const h of [host]) if (h) h.style.setProperty('visibility', hide ? 'hidden' : 'visible', 'important');
      if (hide) capT = setTimeout(() => G.capture(false), 2000);
    },
    arm(kind) { armed = kind || ''; clearTimeout(armT); if (armed) armT = setTimeout(() => { armed = ''; }, 1500); },
    addr(show, text, selAll) {
      if (!show) { if (addrHost) addrHost.remove(); addrHost = addrRoot = null; return; }
      if (!addrHost || !addrHost.isConnected) {
        [addrHost, addrRoot] = mkHost('address');
        const box = mk('div', 'addr', addrRoot), row = mk('div', 'row', box);
        mk('span', 'ic', row); mk('span', 't', row);
        mk('div', 'hint', box).textContent = 'rbot-ai address bar: Enter opens it in this tab · Esc cancels';
      }
      top(addrHost);
      const t = addrRoot.querySelector('.t'); t.textContent = '';
      if (selAll && text) mk('span', 'sel', t).textContent = text; else t.appendChild(document.createTextNode(text || ''));
      mk('span', 'cr', t);
    },
    hitTest(x, y) {
      const el = document.elementFromPoint(x, y);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {tag: el.tagName.toLowerCase(), id: el.id || '', role: el.getAttribute('role') || '',
              name: ((el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '') + '').replace(/\s+/g, ' ').trim().slice(0, 80),
              box: [r.left, r.top, r.right, r.bottom]};
    },
    remove() { closePicker(); for (const h of [host, addrHost]) if (h) h.remove(); host = addrHost = null; pos = null; },
  };
  return 'ok';
})();
"""


# ============================================================================ the executor
class BrowserExecutor:
    kind = "browser"

    def __init__(self, target_id=None, cursor=None, *, stop=None, download_dir=None, kill_hotkey=True,
                 ghost=True):
        self.cursor = cursor                           # desktop overlay: not used for movement in background mode
        self.target_id = target_id
        self.stop_event = stop
        self.ghost = ghost
        self.download_dir = Path(download_dir or DOWNLOAD_DIR)
        self.killed = False
        self.paused_until = 0.0
        self.before_press = None                       # NOT called (screen-coordinate hook), see module docstring
        self._handler = None
        self._human = False
        self._takeover = False
        self._grace_until = 0.0
        self._setup_for = None                         # target id whose session got our setup
        self._script_id = None
        self._ghost_pos = None                         # CSS px
        self._addr = None                              # {"text": str, "sel": bool} while the address bar is open
        self._prompt_text = None
        self._downloads = {}
        self._browser = None
        self._prev_fg = 0
        self.own_window = False                        # True: we opened the window (in_new_window / open_url)
        self._hotkeys = self._watch = None
        if kill_hotkey:
            try:
                from pynput import keyboard, mouse
                self._hotkeys = keyboard.GlobalHotKeys({"<ctrl>+<alt>+q": self._kill})
                self._hotkeys.start()
                self._watch = mouse.Listener(on_click=self._on_click)    # passive hook: never moves the mouse
                self._watch.start()
            except Exception:  # noqa: BLE001
                self._hotkeys = self._watch = None
        if self.target_id is None and bd.alive():
            t = bd.choose_target(bd.targets(timeout=1.0))
            self.target_id = t["id"] if t else None
        self._setup_downloads()

    # ---- safety (same contract as actions.Executor) ----------------------------------------------------------
    def _kill(self):
        print("KILL SWITCH (Ctrl+Alt+Q)")
        self.killed = True

    @property
    def takeover_handler(self):
        return self._handler

    @takeover_handler.setter
    def takeover_handler(self, fn):
        self._handler = fn
        self._takeover = False
        self._grace_until = time.time() + TAKEOVER_GRACE_S

    @property
    def human_turn(self):
        return self._human

    @human_turn.setter
    def human_turn(self, on):
        self._human = bool(on)
        self._takeover = False
        self.paused_until = 0.0
        if not on:
            self._grace_until = time.time() + TAKEOVER_GRACE_S

    @property
    def takeover_pending(self):
        return self._takeover and self._handler is not None and not self._human

    def _on_click(self, x, y, button, pressed, injected=False):
        """A REAL click inside a buddy-browser window while the agent works = the user takes over."""
        if not pressed or injected or self._human or self._handler is None or self._takeover \
                or time.time() < self._grace_until:
            return
        try:
            h = _u32.GetAncestor(_u32.WindowFromPoint(wintypes.POINT(int(x), int(y))), 2)   # GA_ROOT
            if not bd.is_buddy_window(int(h or 0)):
                return
        except Exception:  # noqa: BLE001
            return
        self._takeover = True
        try:
            self._handler()
        except Exception:  # noqa: BLE001
            pass

    def _check_takeover(self):
        if self.killed:
            raise KillSwitch()
        if self.stop_event is not None and self.stop_event.is_set():
            raise KeyboardInterrupt("stopped")
        if self.takeover_pending:
            self._takeover = False
            raise HumanTakeover()

    def _check(self):
        self._check_takeover()

    def _sleep(self, seconds):
        end = time.time() + seconds
        while True:
            self._check_takeover()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.04, left))

    def stop(self):
        """Stop listeners, remove the ghost + injected script, close our CDP sessions."""
        for lst in (self._hotkeys, self._watch):
            try:
                if lst:
                    lst.stop()
            except Exception:  # noqa: BLE001
                pass
        s = bd._sessions.get(self.target_id) if self.target_id else None
        if s is not None and not s.closed:
            try:
                s.call("Runtime.evaluate", {"expression": "window.__buddyGhost && window.__buddyGhost.remove()"},
                       timeout=0.5)
                if self._script_id:
                    s.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self._script_id}, timeout=0.5)
                s.call("Emulation.setFocusEmulationEnabled", {"enabled": False}, timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
            s.close()
            bd._sessions.pop(self.target_id, None)
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    # ---- session / target --------------------------------------------------------------------------------
    def _session(self):
        if self.target_id is None:
            t = bd.choose_target(bd.targets(timeout=1.0))
            if t is None:
                raise RuntimeError("the buddy browser has no open tab (open_url first)")
            self.target_id = t["id"]
        s = bd.get_session(self.target_id, timeout=1.0)
        if self._setup_for != (self.target_id, id(s)):
            self._setup(s)
            self._setup_for = (self.target_id, id(s))
        return s

    def _setup(self, s):
        """Once per session: page keeps 'focus' without the OS window being active; ghost on every new document."""
        try:
            s.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        if self.ghost:
            try:
                self._script_id = s.call("Page.addScriptToEvaluateOnNewDocument",
                                         {"source": GHOST_JS}, timeout=1.0).get("identifier")
            except Exception:  # noqa: BLE001
                self._script_id = None
        try:                                            # a background TAB doesn't render: make it the active tab
            if s.eval("document.visibilityState", timeout=0.5) != "visible":
                self._activate_tab()
        except Exception:  # noqa: BLE001
            pass

    def _browser_session(self):
        if self._browser is None or self._browser.closed:
            ver = bd._http("GET", "/json/version", timeout=1.0)
            self._browser = bd.CDPSession(ver["webSocketDebuggerUrl"], name="cdp-browser")
        return self._browser

    def _activate_tab(self):
        """Make our tab the active one in its window (CDP; checked: doesn't steal the OS foreground)."""
        try:
            self._browser_session().call("Target.activateTarget", {"targetId": self.target_id}, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass

    def _setup_downloads(self):
        if not bd.alive():
            return
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            b = self._browser_session()
            b.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(self.download_dir),
                                                   "eventsEnabled": True}, timeout=1.0)

            def begin(p):
                self._downloads[p["guid"]] = {"guid": p["guid"], "url": p.get("url", ""),
                                              "filename": p.get("suggestedFilename", ""), "state": "inProgress",
                                              "received": 0, "total": 0, "t": time.time(),
                                              "path": str(self.download_dir / p.get("suggestedFilename", ""))}

            def progress(p):
                d = self._downloads.get(p["guid"])
                if d:
                    d.update(state=p.get("state", d["state"]), received=p.get("receivedBytes", 0),
                             total=p.get("totalBytes", 0))
            b.on("Browser.downloadWillBegin", begin)
            b.on("Browser.downloadProgress", progress)
        except Exception:  # noqa: BLE001
            pass

    def last_downloads(self, n=5):
        """Newest first: {guid, url, filename, path, state ('inProgress'|'completed'|'canceled'), received, total}."""
        if self._browser is None:
            self._setup_downloads()
        return sorted(self._downloads.values(), key=lambda d: -d["t"])[:n]

    def _scale(self, s):
        sc = s.state.get("scale")
        if not sc:
            try:
                sc = float(s.eval("window.devicePixelRatio", timeout=0.5) or 1.0)
            except Exception:  # noqa: BLE001
                sc = 1.0
            s.state["scale"] = sc
        return sc

    def _g(self, s, call):
        """Ghost call, fire and forget (re-injects the ghost script first: idempotent, survives navigations)."""
        if not self.ghost:
            return
        try:
            s.send("Runtime.evaluate", {"expression": GHOST_JS + "\n;window.__buddyGhost." + call})
        except Exception:  # noqa: BLE001
            pass

    def observe(self, **kw):
        return bd.observe_tab(self.target_id, **kw)

    # ---- dialogs ---------------------------------------------------------------------------------------------
    def pending_dialog(self):
        """The open JS dialog of our tab {type, message, default, url} or None."""
        if self.target_id is None:
            return None
        s = bd._sessions.get(self.target_id)
        if s is None or s.closed:
            try:
                s = self._session()
            except Exception:  # noqa: BLE001
                return None
        return s.state.get("dialog")

    def answer_dialog(self, accept, text=None):
        s = self._session()
        p = {"accept": bool(accept)}
        if text is not None:
            p["promptText"] = str(text)
        s.call("Page.handleJavaScriptDialog", p, timeout=2.0)
        s.state.pop("dialog", None)
        self._prompt_text = None
        return True

    # ---- pointer ---------------------------------------------------------------------------------------------
    def _mouse(self, s, typ, x, y, button="none", buttons=0, count=0, timeout=1.5, **extra):
        p = {"type": typ, "x": x, "y": y, "button": button, "buttons": buttons, "clickCount": count,
             "modifiers": 0, "pointerType": "mouse", **extra}
        try:
            s.call("Input.dispatchMouseEvent", p, timeout=timeout)
        except TimeoutError:
            if not s.state.get("dialog"):              # a dialog opened by this very click blocks the ack: fine
                raise

    def _glide(self, x, y):
        """Image px -> CSS px; the ghost flies there (ease-out), then the pointer 'arrives' (mouseMoved)."""
        self._check()
        s = self._session()
        self._close_addr(s)
        cx, cy = to_css(x, y, self._scale(s))
        if self.ghost:
            p0 = self._ghost_pos
            ms = glide_ms(p0[0] if p0 else None, p0[1] if p0 else None, cx, cy)
            fx, fy = ("null", "null") if not p0 else (p0[0], p0[1])
            self._g(s, f"move({cx},{cy},{ms},{fx},{fy})")
            self._ghost_pos = (cx, cy)
            if ms:
                self._sleep(ms / 1000 + 0.02)
        self._mouse(s, "mouseMoved", cx, cy)
        self._check()                                  # killed / stopped during the glide -> no click
        return s, cx, cy

    def _dialog_click(self, x, y):
        s = self._session()
        d = s.state.get("dialog")
        if not d:
            return False
        ans = dialog_answer_at(s.state.get("dialog_marks"), x, y)
        if ans == "edit":
            return True                                # focus the prompt field: next type_text fills it
        if ans is None:
            raise RuntimeError(f"a page dialog is open ({d['type']}: {d['message'][:60]!r}) - click OK or Cancel")
        self.answer_dialog(ans, self._prompt_text if ans and d["type"] == "prompt" else None)
        return True

    def _click(self, x, y, button="left", count=1):
        if self._dialog_click(x, y):
            return
        s, cx, cy = self._glide(x, y)
        mask = {"left": 1, "right": 2, "middle": 4}[button]
        self._g(s, f"arm('{button}')")
        for i in range(1, count + 1):
            self._mouse(s, "mousePressed", cx, cy, button, mask, i)
            time.sleep(0.03 if count > 1 else STEP)
            self._mouse(s, "mouseReleased", cx, cy, button, 0, i)
            if i < count:
                time.sleep(0.03)
        self._g(s, f"click({count})")
        time.sleep(STEP)

    def click(self, x, y):
        self._click(x, y)

    def double_click(self, x, y):
        self._click(x, y, count=2)

    def right_click(self, x, y):
        self._click(x, y, "right")

    def move(self, x, y):
        if self.pending_dialog():
            return
        self._glide(x, y)

    def hold(self, x, y, seconds):
        """Press and hold (e.g. 'hold to confirm' buttons): mousePressed, wait, mouseReleased; the ghost fills a ring."""
        seconds = max(0.0, min(float(seconds), 30.0))
        s, cx, cy = self._glide(x, y)
        self._g(s, "arm('left')")
        self._mouse(s, "mousePressed", cx, cy, "left", 1, 1)
        self._g(s, f"hold({int(seconds * 1000)})")
        try:
            self._sleep(seconds)
        finally:                                       # never leave the button down (also on kill / stop)
            self._mouse(s, "mouseReleased", cx, cy, "left", 0, 1)
            self._g(s, "holdEnd()")
        time.sleep(STEP)

    def drag(self, x1, y1, x2, y2, steps=15):
        s, cx1, cy1 = self._glide(x1, y1)
        cx2, cy2 = to_css(x2, y2, self._scale(s))
        drags = []
        try:
            s.call("Input.setInterceptDrags", {"enabled": True}, timeout=1.0)
            s.on("Input.dragIntercepted", drags.append)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._mouse(s, "mousePressed", cx1, cy1, "left", 1, 1)
            time.sleep(0.1)
            ms = max(300, steps * 30)
            if self.ghost:
                self._g(s, f"move({cx2},{cy2},{ms})")
                self._ghost_pos = (cx2, cy2)
            for i in range(1, steps + 1):
                self._check()
                x, y = cx1 + (cx2 - cx1) * i / steps, cy1 + (cy2 - cy1) * i / steps
                self._mouse(s, "mouseMoved", x, y, "none", 1)
                time.sleep(ms / 1000 / steps)
            if drags:                                  # HTML5 drag & drop: deliver it to the drop target
                data = drags[0].get("data", {})
                for typ in ("dragEnter", "dragOver", "drop"):
                    s.call("Input.dispatchDragEvent", {"type": typ, "x": cx2, "y": cy2, "data": data}, timeout=1.5)
            time.sleep(0.1)
        finally:
            self._mouse(s, "mouseReleased", cx2, cy2, "left", 0, 1)
            s.off("Input.dragIntercepted", drags.append)
            try:
                s.call("Input.setInterceptDrags", {"enabled": False}, timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        self._g(s, "click(1)")
        time.sleep(STEP)

    def scroll(self, x, y, clicks):
        """clicks > 0 scrolls up, < 0 scrolls down (same as actions.Executor)."""
        s, cx, cy = self._glide(x, y)
        s.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": cx, "y": cy, "deltaX": 0,
                                            "deltaY": -int(clicks) * WHEEL_PX, "modifiers": 0,
                                            "pointerType": "mouse"}, timeout=1.5)
        time.sleep(STEP * 2)

    def hit_test(self, x, y):
        """What is under image px (x, y) in the page: {tag, id, role, name, box(CSS px)} or None (DOM hover check)."""
        s = self._session()
        cx, cy = to_css(x, y, self._scale(s))
        return s.eval(GHOST_JS + f"\n;window.__buddyGhost.hitTest({cx},{cy})", timeout=1.0)

    # ---- keyboard --------------------------------------------------------------------------------------------
    def _keys(self, s, events):
        for p in events:
            try:
                s.call("Input.dispatchKeyEvent", p, timeout=1.5)
            except TimeoutError:
                if not s.state.get("dialog"):          # e.g. Enter submitted a form that alert()s
                    raise

    def type_text(self, text):
        self._check()
        text = str(text)
        s = self._session()
        d = s.state.get("dialog")
        if d:
            if d["type"] != "prompt":
                raise RuntimeError(f"a page dialog is open ({d['type']}: {d['message'][:60]!r}) - answer it first")
            self._prompt_text = (self._prompt_text or "") + text
            return
        if self._addr is not None:
            a = self._addr
            a["text"] = text if a["sel"] else a["text"] + text
            a["sel"] = False
            self._show_addr(s)
            return
        self._g(s, "typing(true)")
        try:
            run = ""
            for i, ch in enumerate(text):
                ev = char_events(ch)
                if ev is None:                         # other scripts / emoji: insert as text
                    run += ch
                    continue
                if run:
                    s.call("Input.insertText", {"text": run}, timeout=1.5)
                    run = ""
                self._keys(s, ev)
                if i % 3 == 0:
                    self._g(s, "key()")
                    self._check()
                time.sleep(0.008)
            if run:
                s.call("Input.insertText", {"text": run}, timeout=1.5)
        finally:
            self._g(s, "typing(false)")
        time.sleep(STEP)

    def press_key(self, name):
        name = str(name)
        if "+" in name.strip("+") and len(name) > 1:   # 'ctrl+a' given to press_key
            return self.hotkey([p for p in name.split("+") if p.strip()])
        self.hotkey([name])

    def hotkey(self, names):
        self._check()
        names = [names] if isinstance(names, str) else [str(n) for n in names]
        s = self._session()
        keys = [norm_key(n) for n in names]
        if s.state.get("dialog"):
            if keys == ["enter"]:
                return self.answer_dialog(True, self._prompt_text)
            if keys == ["escape"]:
                return self.answer_dialog(False)
            raise RuntimeError("a page dialog is open - press enter (OK) or escape (Cancel)")
        if self._addr is not None and self._addr_key(s, keys):
            return
        what = browser_shortcut(keys)
        if what:
            return self._browser_key(s, what, names)
        self._g(s, "typing(true)")
        try:
            self._keys(s, combo_events(keys))
            self._g(s, "key()")
        finally:
            self._g(s, "typing(false)")
        time.sleep(STEP)

    def _browser_key(self, s, what, names):
        if what in ("address", "address_new"):
            url = "" if what == "address_new" else (s.eval("location.href", timeout=1.0) or "")
            self._addr = {"text": url, "sel": bool(url)}
            self._show_addr(s)
        elif what in ("reload", "reload_hard"):
            s.call("Page.reload", {"ignoreCache": what == "reload_hard"}, timeout=2.0)
        elif what in ("back", "forward"):
            h = s.call("Page.getNavigationHistory", {}, timeout=1.0)
            i = h["currentIndex"] + (-1 if what == "back" else 1)
            if 0 <= i < len(h["entries"]):
                s.call("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]}, timeout=2.0)
        elif what == "desktop":
            raise RuntimeError(f"{'+'.join(names)} is a desktop shortcut: not available while working in the "
                               "buddy browser in the background")
        else:
            raise RuntimeError(f"{'+'.join(names)} is a browser-window shortcut that doesn't work in background "
                               "mode (the agent works in one tab)")
        time.sleep(STEP)

    # ---- the in-page address bar (ctrl+l emulation) ------------------------------------------------------------
    def _show_addr(self, s):
        a = self._addr
        self._g(s, f"addr(true,{json.dumps(a['text'])},{'true' if a['sel'] else 'false'})")

    def _close_addr(self, s):
        if self._addr is not None:
            self._addr = None
            self._g(s, "addr(false)")

    def _addr_key(self, s, keys):
        a = self._addr
        if keys == ["enter"]:
            url = nav_url(a["text"])
            self._close_addr(s)
            if url:
                self.navigate(url)
            return True
        if keys == ["escape"]:
            self._close_addr(s)
            return True
        if keys == ["backspace"] or keys == ["delete"]:
            a["text"] = "" if a["sel"] else a["text"][:-1]
        elif keys == ["ctrl", "a"]:
            a["sel"] = True
        elif keys in (["left"], ["right"], ["home"], ["end"]):
            a["sel"] = False
        elif browser_shortcut(keys) in ("address", "address_new"):
            return False
        else:
            return False
        self._show_addr(s)
        return True

    # ---- navigation / misc -------------------------------------------------------------------------------------
    def navigate(self, url, wait_s=10.0):
        """Open url in OUR tab (no new tab, no window activation); waits for the load event (best effort)."""
        s = self._session()
        self._close_addr(s)
        loaded, seen = threading.Event(), set()        # 'load' lifecycle events per loaderId: a late load event
        want = []                                      # of the PREVIOUS page must not count (layout not ready)

        def on_life(p):
            if p.get("name") == "load":
                seen.add(p.get("loaderId"))
                if want and want[0] in seen:
                    loaded.set()
        try:
            s.call("Page.setLifecycleEventsEnabled", {"enabled": True}, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        s.on("Page.lifecycleEvent", on_life)
        try:
            res = s.call("Page.navigate", {"url": url}, timeout=wait_s)
            if res.get("errorText"):
                raise RuntimeError(f"could not open {url}: {res['errorText']}")
            if res.get("loaderId"):                    # (no loaderId: same-document navigation, nothing to wait for)
                want.append(res["loaderId"])
                if res["loaderId"] in seen:
                    loaded.set()
                if not loaded.wait(wait_s):            # lifecycle events unavailable? fall back to readyState
                    end = time.time() + 2.0
                    while time.time() < end and s.eval("document.readyState", timeout=1.0) != "complete":
                        time.sleep(0.1)
        finally:
            s.off("Page.lifecycleEvent", on_life)
        s.state.pop("scale", None)
        self._ghost_pos = None
        return True

    @classmethod
    def in_new_window(cls, url="about:blank", **kw):
        """Executor bound to a fresh buddy-browser window of its own (created behind the user's windows). The
        recommended way to start a background task when the buddy browser already runs: the agent's tab is then
        the only tab of its window, so it keeps rendering at full speed while covered."""
        if not bd.alive():
            if not bd.launch(url, front=False):
                raise RuntimeError("could not start the buddy browser")
            time.sleep(0.5)
            t = bd.choose_target(bd.targets(timeout=1.0))
            return cls(target_id=t["id"] if t else None, **kw)
        tid = bd.new_window(url)
        time.sleep(0.3)
        ex = cls(target_id=tid, **kw)
        ex.own_window = True
        return ex

    def open_url(self, url):
        """Background version of the agent's open_url: our tab navigates. No tab yet -> starts the buddy browser
        behind the current window, or opens a window of our own in the running one."""
        self._check()
        if self.target_id is None or bd.choose_target(bd.targets(timeout=1.0), self.target_id) is None:
            if not bd.alive():
                if not bd.launch(url, front=False):
                    raise RuntimeError("could not start the buddy browser")
                time.sleep(0.5)
                t = bd.choose_target(bd.targets(timeout=1.0))
                self.target_id = t["id"] if t else None
            else:
                self.target_id = bd.new_window(url)
                self.own_window = True
            if self._browser is None:
                self._setup_downloads()
            time.sleep(0.5)
            return True
        return self.navigate(url)

    def close_window(self):
        """Close our tab (and with it the window we opened). The buddy browser itself keeps running."""
        if self.target_id is None:
            return False
        try:
            ws = bd._http("GET", "/json/version", timeout=1.0)["webSocketDebuggerUrl"]
            bd._cdp(ws, "Target.closeTarget", {"targetId": self.target_id}, timeout=2.0)
        except Exception:  # noqa: BLE001
            return False
        bd._sessions.pop(self.target_id, None)
        self.target_id = None
        return True

    def open_app(self, name):
        raise RuntimeError("open_app is not available while working in the buddy browser (background mode)")

    def wait(self, seconds):
        self._check()
        self._sleep(max(0.0, float(seconds)))

    # ---- HITL: hand the window to the human and back ------------------------------------------------------------
    def _hwnd(self):
        t = bd.choose_target(bd.targets(timeout=1.0), self.target_id)
        return bd.buddy_hwnd_for(t.get("title", "") if t else "")

    def bring_to_front(self):
        """Handover: our tab becomes the active tab and its window comes to the front (no mouse/keyboard use)."""
        fg = int(_u32.GetForegroundWindow() or 0)
        if fg and not bd.is_buddy_window(fg):
            self._prev_fg = fg
        self._activate_tab()
        time.sleep(0.15)
        h = self._hwnd()
        return bd.focus(h) if h else False

    def send_to_back(self):
        """After the handover: give the foreground back to the window the user had, buddy window behind it
        (never minimised - minimised windows stop rendering)."""
        h = self._hwnd()
        ok = bd.send_window_to_back(h) if h else False
        prev = self._prev_fg
        if prev and _u32.IsWindow(prev):
            bd.focus(prev)
        self._prev_fg = 0
        return ok


# ============================================================================ blockers without the window in front
def detect_blocker_tab(obs):
    """hitl.detect_blocker for a background tab observation (browser_dom.observe_tab): same text cues on the DOM
    marks + window title, plus DOM facts (password / card inputs, captcha iframes, fixed cookie banners) from
    obs["flags"] instead of the foreground window's UIA. -> hitl.Blocker | None."""
    from rbot.human import hitl
    window = obs.get("window", "") or ""
    texts = [(hitl._norm(window), "title")]
    texts += [(hitl._norm(m.get("name")), m.get("type", "")) for m in obs.get("marks", []) or [] if m.get("name")]
    flags = obs.get("flags") or {}
    sc = {k: hitl._score(k, texts) for k in hitl.KINDS}
    clip, found = hitl._clip, {}
    h = sc["captcha"]
    if flags.get("captcha"):
        found["captcha"] = f"captcha frame '{clip(flags['captcha'][0], 40)}' in page '{clip(window, 50)}'"
    elif sum(v[0] for v in h.values()) >= 2:
        found["captcha"] = "captcha: " + hitl._cue_evidence(h, window)
    h = sc["otp"]
    if sum(v[0] for v in h.values()) >= 2:
        found["otp"] = "one-time code: " + hitl._cue_evidence(h, window)
    h = sc["payment"]
    card_field = next((v for g, v in h.items() if g in hitl.PAYMENT_FIELD and v[2] == "Edit"), None)
    if flags.get("card"):
        found["payment"] = f"card field '{clip(flags['card'][0], 30)}' in page '{clip(window, 50)}'"
    elif card_field:
        found["payment"] = f"card field '{clip(card_field[3], 30)}' in page '{clip(window, 50)}'"
    elif len(h) >= 2 and hitl.PAYMENT_HARD & set(h):
        found["payment"] = "payment: " + hitl._cue_evidence(h, window)
    pw = (flags.get("password") or [None])[0]
    if pw is None:                                       # the collector names password inputs '... (password)'
        pw = next((t for t, where in texts if where == "Edit" and t.endswith("(password)")), None)
    if pw is not None:
        nm = hitl._norm(pw)
        if any(rx.search(nm) for g, _, ps in hitl._CUE_RE["payment"] if g in hitl.PAYMENT_FIELD for _, rx in ps):
            found.setdefault("payment", f"card field '{clip(pw, 30)}' (masked) in page '{clip(window, 50)}'")
        else:
            found["login"] = f"password field '{clip(pw, 30) or '(unnamed)'}' in page '{clip(window, 50)}'"
    if "login" not in found:
        h = sc["login"]
        strong = ("password" in h and h["password"][2] in ("title", "Edit")) or \
                 ("signin" in h and h["signin"][2] in ("title", "Document"))
        if len(h) >= 2 and strong:
            found["login"] = "sign-in page: " + hitl._cue_evidence(h, window)
    h = sc["consent"]
    btns = {t for t, where in texts if where in ("Button", "SplitButton", "Hyperlink")}
    if btns & hitl.ALLOW_NAMES and btns & hitl.DENY_NAMES:
        found["consent"] = f"permission prompt with 'Allow'/'{sorted(btns & hitl.DENY_NAMES)[0]}' buttons; " \
                           f"page '{clip(window, 50)}'"
    elif sum(v[0] for v in h.values()) >= 2:
        found["consent"] = "consent banner: " + hitl._cue_evidence(h, window)
    elif flags.get("cookie"):
        found["consent"] = f"cookie banner '{clip(flags['cookie'][0], 40)}' in page '{clip(window, 50)}'"
    for k in hitl.KINDS:
        if k in found:
            return hitl.Blocker(kind=k, evidence=found[k], auto_resume=k in hitl.HUMAN_KINDS, window=window,
                                hwnd=int(obs.get("hwnd") or 0))
    return None
