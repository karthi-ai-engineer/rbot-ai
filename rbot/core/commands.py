"""File / folder commands: method 1 of the ladder (PLAN §3). No screen, no mouse: pathlib + shutil.

  out = commands.run("move_files", {"sources": ["Downloads/*.pdf"], "dest_folder": "Documents/Reports"},
                     ask=hitl_ask, say=buddy_say)        # -> "moved 3 files to C:\\Users\\...\\Reports"

Safety (PLAN §9):
- Only inside the allowed folders: the user's Downloads, Documents, Desktop, Pictures, Videos, Music and the
  project's runs/. BUDDY_ALLOWED_FOLDERS (";"-separated, shortcuts allowed) or allowed=[...] replaces that list.
  Anything else -> BadAction("BLOCKED: ...").
- Never touches a file or folder named .env* (the API key lives there); such files are also hidden from lists.
- Delete only moves to the Recycle Bin (send2trash), never a permanent delete. Delete and overwrite ALWAYS ask the
  human first (ask(question, options) -> answer or None); without a human (ask=None) they are refused. A replaced
  file also goes to the Recycle Bin before the new one takes its place.
- The allowed folders themselves can be listed / searched / used as a destination, but never moved, renamed or
  deleted.
Paths: full paths, ~, %USERPROFILE%, or starting with Downloads / Documents / Desktop / Pictures / Videos / Music.
"""
import builtins
import fnmatch
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from rbot.core.agent import BadAction

from rbot import RUNS
TOOLS = {"list_files", "find_files", "move_files", "copy_files", "rename_file", "make_folder", "zip_files",
         "open_file", "delete_files"}
KEEPS_SCREEN = TOOLS - {"open_file"}          # these never change what is on screen
SHORTCUTS = ("Downloads", "Documents", "Desktop", "Pictures", "Videos", "Music")
ALIASES = {"my documents": "Documents", "download": "Downloads", "my downloads": "Downloads", "pics": "Pictures",
           "photos": "Pictures", "my pictures": "Pictures", "my music": "Music", "my videos": "Videos"}
KNOWN_IDS = {"Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
             "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
             "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
             "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
             "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
             "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}"}
LIST_MAX = 60
FIND_MAX_FILES, FIND_BUDGET_S = 50000, 6.0
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".htm", ".ini", ".cfg", ".yaml", ".yml", ".py",
            ".js", ".ts", ".sql", ".tsv", ".rtf"}
TEXT_MAX_BYTES = 512 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$recycle.bin", ".buddy_browser"}
BAD_NAME = re.compile(r'[\\/:*?"<>|]')
YES = re.compile(r"^\s*(yes|y|ok|okay|sure|go ahead|do it|replace|delete|move)\b", re.I)


# ---------------------------------------------------------------- folders
_known_cache = {}


def _known_folder(name):
    """Windows known folder (follows OneDrive / moved folders), else ~/<name>."""
    if name in _known_cache:
        return _known_cache[name]
    p = None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]
        u = uuid.UUID(KNOWN_IDS[name])
        g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            p = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001
        p = None
    p = p or Path.home() / name
    _known_cache[name] = p
    return p


def known_folders():
    """{"Downloads": Path, ...} (tests monkeypatch this)."""
    return {n: _known_folder(n) for n in SHORTCUTS}


def allowed_folders(allowed=None):
    """The allow-list as resolved Paths: the allowed arg, else $BUDDY_ALLOWED_FOLDERS, else the defaults."""
    if allowed is None:
        env = os.getenv("BUDDY_ALLOWED_FOLDERS", "").strip()
        allowed = [a for a in re.split(r"[;\n]", env) if a.strip()] if env else None
    if allowed is None:
        allowed = list(known_folders().values()) + [RUNS]
    out = []
    for a in allowed:
        try:
            out.append(_expand(str(a)).resolve())
        except BadAction:
            continue
    return out


def _expand(text):
    """Text from the model -> absolute Path (no allow-list check yet)."""
    t = str(text or "").strip().strip('"').strip("'").strip()
    if not t:
        raise BadAction("empty path")
    t = os.path.expanduser(os.path.expandvars(t))
    parts = re.split(r"[\\/]", t, maxsplit=1)
    head = parts[0].strip().lower()
    known = known_folders()
    name = next((n for n in known if n.lower() == head), None) or ALIASES.get(head)
    if name and not os.path.isabs(t):
        p = Path(known[name])
        return p / parts[1] if len(parts) > 1 and parts[1] else p
    p = Path(t)
    if not p.is_absolute():
        raise BadAction(f"'{text}' is not a full path - give a full path, or start it with "
                        f"{', '.join(SHORTCUTS)} or ~")
    return p


def _is_env(p):
    return any(part.lower().startswith(".env") for part in Path(p).parts)


def _inside(p, root):
    pc, rc = os.path.normcase(str(p)), os.path.normcase(str(root))
    return pc == rc or pc.startswith(rc.rstrip("\\/") + os.sep)


def _short(p):
    """C:\\Users\\me\\Downloads\\x.pdf -> x.pdf (for cards and questions)."""
    return Path(p).name or str(p)


class Ctx:
    def __init__(self, ask=None, say=None, allowed=None):
        self.ask, self.say = ask, say or (lambda text: None)
        self.roots = allowed_folders(allowed)

    def path(self, text, strict_inside=False, must_exist=False):
        """Model text -> resolved Path inside the allow-list (BadAction BLOCKED otherwise)."""
        p = _expand(text)
        try:
            p = p.resolve()
        except OSError:
            p = Path(os.path.abspath(p))
        if _is_env(p):
            raise BadAction(f"BLOCKED: {_short(p)} is a secrets file (.env); I never touch those")
        root = next((r for r in self.roots if _inside(p, r)), None)
        if root is None:
            raise BadAction(f"BLOCKED: {p} is outside the folders I may use ({', '.join(str(r) for r in self.roots)})")
        if strict_inside and os.path.normcase(str(p)) == os.path.normcase(str(root)):
            raise BadAction(f"BLOCKED: {p} is one of your main folders; I won't move, rename or delete it")
        if must_exist and not p.exists():
            raise BadAction(f"not found: {p}")
        return p

    def sources(self, items, strict_inside=True):
        """List of paths / wildcards -> existing resolved Paths (no .env*)."""
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, (list, tuple)) or not items:
            raise BadAction("give at least one file")
        out = []
        for it in items:
            t = str(it)
            if any(c in Path(t).name for c in "*?["):
                folder = self.path(str(_expand(t).parent), must_exist=True)
                hits = sorted(q for q in folder.glob(_expand(t).name) if not _is_env(q))
                if not hits:
                    raise BadAction(f"no files match {t}")
                out += [self.path(str(q), strict_inside) for q in hits]
            else:
                out.append(self.path(t, strict_inside, must_exist=True))
        seen, uniq = set(), []
        for p in out:
            k = os.path.normcase(str(p))
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        return uniq

    def confirm(self, question):
        """Yes/No question to the human. -> True only on a clear yes; None if there is no human."""
        if self.ask is None:
            return None
        ans = self.ask(question, ["Yes", "No"])
        return bool(ans) and bool(YES.match(str(ans)))


def _count(paths):
    nf = sum(1 for p in paths if Path(p).is_dir())
    n = len(paths) - nf
    bits = ([f"{n} file{'s' if n != 1 else ''}"] if n else []) + ([f"{nf} folder{'s' if nf != 1 else ''}"] if nf else [])
    return " and ".join(bits) or "0 files"


def _names(paths, n=5):
    names = [_short(p) for p in paths[:n]]
    return ", ".join(names) + (f" and {len(paths) - n} more" if len(paths) > n else "")


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _trash(p):
    """Recycle Bin only. Never a permanent delete."""
    try:
        from send2trash import send2trash
    except ImportError:
        raise BadAction("deleting is unavailable: send2trash is not installed (pip install send2trash)") from None
    send2trash(str(p))


# ---------------------------------------------------------------- the commands
def list_files(c, folder, pattern=None):
    d = c.path(folder, must_exist=True)
    if not d.is_dir():
        raise BadAction(f"not a folder: {d}")
    pat = str(pattern or "*").strip() or "*"
    items = [q for q in d.iterdir() if not _is_env(q) and fnmatch.fnmatch(q.name.lower(), pat.lower())]
    items.sort(key=lambda q: (not q.is_dir(), q.name.lower()))
    rows = []
    for q in items[:LIST_MAX]:
        try:
            st = q.stat()
            when = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
            rows.append(f"{q.name}/ (folder)" if q.is_dir() else f"{q.name} ({_size(st.st_size)}, {when})")
        except OSError:
            rows.append(q.name)
    more = f"\n... and {len(items) - LIST_MAX} more" if len(items) > LIST_MAX else ""
    c.say(f"{len(items)} item{'s' if len(items) != 1 else ''} in {d.name or d}")
    head = f"{len(items)} item{'s' if len(items) != 1 else ''} in {d}" + (f" matching {pat}" if pat != "*" else "")
    return head + (":\n" + "\n".join(rows) if rows else "") + more


def find_files(c, name_or_text, folder=None, max=20):  # noqa: A002  (the tool's argument name)
    q = str(name_or_text or "").strip()
    if not q:
        raise BadAction("find_files needs a name or text")
    try:
        limit = min(builtins.max(int(max or 20), 1), 100)
    except (TypeError, ValueError):
        limit = 20
    roots = [c.path(folder, must_exist=True)] if folder else [r for r in c.roots if r.exists()]
    wild = any(ch in q for ch in "*?[")
    ql = q.lower()
    by_name, by_text, scanned, t0 = [], [], 0, time.time()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.lower().startswith(".env")]
            for fn in filenames:
                if fn.lower().startswith(".env"):
                    continue
                scanned += 1
                p = Path(dirpath) / fn
                if (fnmatch.fnmatch(fn.lower(), ql) if wild else ql in fn.lower()):
                    by_name.append(p)
                elif not wild and len(by_name) + len(by_text) < limit and p.suffix.lower() in TEXT_EXT:
                    try:
                        if p.stat().st_size <= TEXT_MAX_BYTES and ql in p.read_text("utf-8", "ignore").lower():
                            by_text.append(p)
                    except OSError:
                        pass
                if len(by_name) >= limit or scanned >= FIND_MAX_FILES or time.time() - t0 > FIND_BUDGET_S:
                    break
            else:
                continue
            break
    hits = (by_name + by_text)[:limit]
    where = str(roots[0]) if len(roots) == 1 else "your folders"
    c.say(f"Found {len(hits)} file{'s' if len(hits) != 1 else ''}" if hits else f"No files found for '{q[:30]}'")
    if not hits:
        return f"no files found matching '{q}' in {where} (searched {scanned} files)"
    rows = [f"{p}" + ("  (text inside matches)" if p in by_text else "") for p in hits]
    return f"found {len(hits)} file{'s' if len(hits) != 1 else ''} for '{q}' in {where}:\n" + "\n".join(rows)


def _transfer(c, verb, sources, dest_folder, op):
    srcs = c.sources(sources)
    dest = c.path(dest_folder)
    if dest.exists() and not dest.is_dir():
        raise BadAction(f"{dest} is a file, not a folder")
    for s in srcs:
        if s.is_dir() and _inside(dest, s):
            raise BadAction(f"can't {verb} the folder {s.name} into itself")
    made = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    jobs = [(s, dest / s.name) for s in srcs]
    same = sum(1 for s, t in jobs if os.path.normcase(str(s)) == os.path.normcase(str(t)))
    clash = [(s, t) for s, t in jobs if t.exists() and os.path.normcase(str(s)) != os.path.normcase(str(t))]
    skip, note = False, ""
    if clash:
        ok = c.confirm(f"Replace {_count([t for _, t in clash])} that already exist in {dest.name}: "
                       f"{_names([t for _, t in clash])}? The old ones go to the Recycle Bin.")
        if not ok:
            skip = True
            note = (" (overwriting needs your OK and no one is there to ask)" if ok is None
                    else " (you said no to replacing them)")
    done = []
    for s, t in jobs:
        if os.path.normcase(str(s)) == os.path.normcase(str(t)):
            continue
        if t.exists():
            if skip:
                continue
            _trash(t)                                  # the old one goes to the Recycle Bin, never lost
        op(s, t)
        done.append(t)
    past = {"move": "moved", "copy": "copied"}[verb]
    msg = f"{past} {_count(done) if done else 'nothing'} to {dest}" + (" (new folder)" if made else "")
    if done and len(done) <= 5:
        msg += f": {_names(done)}"
    if clash and skip:
        msg += f"; skipped {len(clash)} that already exist{note}"
    if same:
        msg += f"; {same} already there"
    c.say(f"{past.capitalize()} {_count(done)} to {dest.name}" if done else f"Nothing {past}")
    return msg


def move_files(c, sources, dest_folder):
    return _transfer(c, "move", sources, dest_folder, lambda s, t: shutil.move(str(s), str(t)))


def copy_files(c, sources, dest_folder):
    def cp(s, t):
        if s.is_dir():
            shutil.copytree(s, t, ignore=lambda d, names: [n for n in names if n.lower().startswith(".env")])
        else:
            shutil.copy2(s, t)
    return _transfer(c, "copy", sources, dest_folder, cp)


def rename_file(c, path, new_name):
    src = c.sources([path])[0]
    nn = str(new_name or "").strip()
    if not nn or BAD_NAME.search(nn) or nn in (".", ".."):
        raise BadAction(f"'{new_name}' is not a valid name (just the new name, no folders or \\ / : * ? \" < > |)")
    if nn.lower().startswith(".env"):
        raise BadAction("BLOCKED: I never create .env files")
    added = ""
    if src.is_file() and src.suffix and not Path(nn).suffix:
        nn += src.suffix
        added = f" (kept the {src.suffix} ending)"
    dst = src.with_name(nn)
    if os.path.normcase(str(dst)) == os.path.normcase(str(src)) and dst.name == src.name:
        return f"{src.name} already has that name"
    if dst.exists() and os.path.normcase(str(dst)) != os.path.normcase(str(src)):
        ok = c.confirm(f"{dst.name} already exists in {dst.parent.name}. Replace it with {src.name}? "
                       "The old one goes to the Recycle Bin.")
        if not ok:
            raise BadAction(f"not renamed: {dst.name} already exists and "
                            + ("overwriting needs your OK" if ok is None else "you said no to replacing it"))
        _trash(dst)
    src.rename(dst)
    c.say(f"Renamed to {dst.name}")
    return f"renamed {src.name} to {dst.name} in {dst.parent}{added}"


def make_folder(c, path):
    p = c.path(path)
    if p.exists():
        return f"folder already exists: {p}" if p.is_dir() else f"{p} exists and is a file"
    p.mkdir(parents=True)
    c.say(f"New folder {p.name}")
    return f"made folder {p}"


def zip_files(c, sources, zip_path):
    srcs = c.sources(sources, strict_inside=False)
    z = c.path(zip_path)
    if z.suffix.lower() != ".zip":
        z = c.path(str(z) + ".zip")
    if z.exists():
        ok = c.confirm(f"{z.name} already exists. Replace it? The old one goes to the Recycle Bin.")
        if not ok:
            raise BadAction(f"not zipped: {z.name} already exists and "
                            + ("overwriting needs your OK" if ok is None else "you said no to replacing it"))
        _trash(z)
    z.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in srcs:
            if s.is_dir():
                for dirpath, dirnames, filenames in os.walk(s):
                    dirnames[:] = [d for d in dirnames if not d.lower().startswith(".env")]
                    for fn in filenames:
                        p = Path(dirpath) / fn
                        if fn.lower().startswith(".env") or os.path.normcase(str(p)) == os.path.normcase(str(z)):
                            continue
                        zf.write(p, p.relative_to(s.parent))
                        n += 1
            else:
                zf.write(s, s.name)
                n += 1
    c.say(f"Zipped {n} file{'s' if n != 1 else ''} into {z.name}")
    return f"zipped {n} file{'s' if n != 1 else ''} into {z} ({_size(z.stat().st_size)})"


def open_file(c, path):
    p = c.path(path, must_exist=True)
    os.startfile(str(p))  # noqa: S606  (opens with the default app, like a double-click)
    c.say(f"Opened {p.name}")
    return f"opened {p} with its default app" if p.is_file() else f"opened the folder {p}"


def delete_files(c, paths):
    srcs = c.sources(paths)
    q = f"Move {_count(srcs)} to the Recycle Bin: {_names(srcs)}?"
    ok = c.confirm(q)
    if ok is None:
        raise BadAction("BLOCKED: deleting always needs the user's OK and no one is there to ask")
    if not ok:
        c.say("Not deleted")
        return f"not deleted: the user said no ({_names(srcs)})"
    for s in srcs:
        _trash(s)
    c.say(f"{_count(srcs).capitalize()} moved to the Recycle Bin")
    return f"moved {_count(srcs)} to the Recycle Bin: {_names(srcs, 10)}"


FUNCS = {"list_files": (list_files, ("folder",), ("pattern",)),
         "find_files": (find_files, ("name_or_text",), ("folder", "max")),
         "move_files": (move_files, ("sources", "dest_folder"), ()),
         "copy_files": (copy_files, ("sources", "dest_folder"), ()),
         "rename_file": (rename_file, ("path", "new_name"), ()),
         "make_folder": (make_folder, ("path",), ()),
         "zip_files": (zip_files, ("sources", "zip_path"), ()),
         "open_file": (open_file, ("path",), ()),
         "delete_files": (delete_files, ("paths",), ())}


def run(name, args, ask=None, say=None, allowed=None):
    """Run one file command. -> short plain outcome for the history. Raises BadAction (BLOCKED / bad input).
    ask(question, options) -> answer|None reaches the human (None = no human: delete/overwrite refused);
    say(text) shows the buddy card; allowed = folder list overriding the allow-list."""
    fn, req, opt = FUNCS[name]
    a = args or {}
    missing = [k for k in req if a.get(k) in (None, "", [])]
    if missing:
        raise BadAction(f"{name} is missing {', '.join(missing)}")
    c = Ctx(ask, say, allowed)
    kw = {k: a[k] for k in req}
    kw.update({k: a[k] for k in opt if a.get(k) not in (None, "")})
    try:
        out = fn(c, **kw)
    except BadAction:
        raise
    except (OSError, shutil.Error, zipfile.BadZipFile) as e:
        raise BadAction(f"{name} failed: {e}") from None
    return str(out)[:1500]
