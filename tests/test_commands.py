"""Offline tests for commands.py (file / folder commands, method "command") and their use inside both agent loops.

  python -m pytest -q tests/test_commands.py

Temp folders only: the allow-list is overridden (allowed=[...] / BUDDY_ALLOWED_FOLDERS) and the Downloads /
Documents / Desktop shortcuts point into a temp dir. The Recycle Bin is a fake send2trash module that only records,
so nothing is ever deleted for real. os.startfile is mocked.
"""
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from rbot import ROOT, RUNS  # noqa: E402
from rbot.core import agent  # noqa: E402
from rbot.core import agent_graph  # noqa: E402
from rbot.core import commands  # noqa: E402
from rbot.core.agent import BadAction  # noqa: E402
from test_agent_graph import both, check_events, go, of  # noqa: E402
from test_human_loop import C, Screen, setup  # noqa: E402


# ============================================================================ fixtures
@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake user profile: Downloads / Documents / Desktop / Pictures / Videos / Music under tmp_path/user."""
    user = tmp_path / "user"
    folders = {n: user / n for n in commands.SHORTCUTS}
    for p in folders.values():
        p.mkdir(parents=True)
    monkeypatch.setattr(commands, "known_folders", lambda: dict(folders))
    monkeypatch.setenv("BUDDY_ALLOWED_FOLDERS", ";".join(str(p) for p in folders.values()))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("HOME", str(user))
    return user


@pytest.fixture
def trash(monkeypatch, tmp_path):
    """Fake send2trash: records the paths and moves them into tmp_path/RecycleBin (like the real bin, nothing is
    destroyed)."""
    binned = []
    bin_dir = tmp_path / "RecycleBin"

    def send2trash(p):
        binned.append(str(p))
        bin_dir.mkdir(exist_ok=True)
        shutil.move(str(p), str(bin_dir / f"{len(binned)}_{Path(p).name}"))
    mod = types.ModuleType("send2trash")
    mod.send2trash = send2trash
    monkeypatch.setitem(sys.modules, "send2trash", mod)
    return binned


def mk(p, text="x"):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class Asker:
    """ask(question, options) stand-in: answers in order, records the questions."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, q, options=None):
        self.calls.append((q, options))
        return self.answers.pop(0) if self.answers else None


def run(name, args, **kw):
    cards = kw.pop("cards", [])
    return commands.run(name, args, say=cards.append, **kw)


# ============================================================================ paths + safety
def test_paths_shortcuts_tilde_envvars(home):
    mk(home / "Downloads" / "a.pdf")
    c = commands.Ctx()
    assert c.path("Downloads/a.pdf") == (home / "Downloads" / "a.pdf").resolve()
    assert c.path("downloads\\a.pdf") == (home / "Downloads" / "a.pdf").resolve()
    assert c.path("~/Downloads/a.pdf") == (home / "Downloads" / "a.pdf").resolve()
    assert c.path("%USERPROFILE%\\Downloads\\a.pdf") == (home / "Downloads" / "a.pdf").resolve()
    assert c.path("Documents") == (home / "Documents").resolve()
    assert c.path(str(home / "Desktop" / "new")) == (home / "Desktop" / "new").resolve()
    with pytest.raises(BadAction, match="not a full path"):
        c.path("a.pdf")


def test_outside_allow_list_is_blocked(home, tmp_path):
    secret = mk(tmp_path / "elsewhere" / "s.txt")
    c = commands.Ctx()
    for bad in (str(secret), "Downloads/../../elsewhere/s.txt", "C:\\Windows\\win.ini", str(ROOT / "rbot" / "core" / "agent.py")):
        with pytest.raises(BadAction, match="^BLOCKED"):
            c.path(bad)
    with pytest.raises(BadAction, match="^BLOCKED"):
        run("move_files", {"sources": [str(secret)], "dest_folder": "Downloads"})
    with pytest.raises(BadAction, match="^BLOCKED"):
        run("list_files", {"folder": str(tmp_path / "elsewhere")})
    assert secret.exists()


def test_allow_list_sources(home, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    mk(other / "f.txt")
    assert "1 item" in run("list_files", {"folder": str(other)}, allowed=[str(other)])      # function arg
    with pytest.raises(BadAction, match="^BLOCKED"):
        run("list_files", {"folder": str(other)})                                       # env list
    monkeypatch.setenv("BUDDY_ALLOWED_FOLDERS", f"{other};Downloads")
    assert commands.allowed_folders() == [other.resolve(), (home / "Downloads").resolve()]
    monkeypatch.delenv("BUDDY_ALLOWED_FOLDERS")
    defaults = commands.allowed_folders()                                               # the real defaults
    assert RUNS.resolve() in defaults and (home / "Downloads").resolve() in defaults
    assert len(defaults) == 7


def test_env_files_are_never_touched(home, trash):
    d = home / "Documents"
    env, keep = mk(d / ".env", "API_KEY=secret"), mk(d / "notes.txt")
    mk(d / ".env.local", "x")
    out = run("list_files", {"folder": "Documents"})
    assert ".env" not in out and "notes.txt" in out and out.startswith("1 item")
    for name, args in [("move_files", {"sources": ["Documents/.env"], "dest_folder": "Downloads"}),
                       ("delete_files", {"paths": ["Documents/.env"]}),
                       ("open_file", {"path": "Documents/.env"}),
                       ("copy_files", {"sources": ["Documents/.env.local"], "dest_folder": "Downloads"})]:
        with pytest.raises(BadAction, match="BLOCKED: .* secrets file"):
            run(name, args, ask=Asker("Yes"))
    with pytest.raises(BadAction, match="BLOCKED"):
        run("rename_file", {"path": "Documents/notes.txt", "new_name": ".env"})
    out = run("move_files", {"sources": ["Documents/*"], "dest_folder": "Downloads/all"})   # wildcards skip it
    assert "notes.txt" in out and env.exists() and not keep.exists()
    assert "API_KEY" not in run("find_files", {"name_or_text": "env"}) and not trash


def test_main_folders_cannot_be_moved_or_deleted(home, trash):
    with pytest.raises(BadAction, match="one of your main folders"):
        run("delete_files", {"paths": ["Downloads"]}, ask=Asker("Yes"))
    with pytest.raises(BadAction, match="one of your main folders"):
        run("move_files", {"sources": ["Pictures"], "dest_folder": "Documents"})
    assert not trash and (home / "Downloads").is_dir()


# ============================================================================ the commands
def test_list_files(home):
    d = home / "Downloads"
    mk(d / "b.pdf", "x" * 2048)
    mk(d / "a.txt")
    (d / "Reports").mkdir()
    cards = []
    out = run("list_files", {"folder": "Downloads"}, cards=cards)
    lines = out.splitlines()
    assert lines[0] == f"3 items in {d.resolve()}:"
    assert lines[1] == "Reports/ (folder)" and lines[2].startswith("a.txt (") and lines[3].startswith("b.pdf (2.0 KB")
    assert cards == ["3 items in Downloads"]
    assert run("list_files", {"folder": "Downloads", "pattern": "*.PDF"}).startswith("1 item in")
    with pytest.raises(BadAction, match="not found"):
        run("list_files", {"folder": "Downloads/nope"})
    with pytest.raises(BadAction, match="missing folder"):
        run("list_files", {})


def test_find_files_by_name_wildcard_and_text(home):
    mk(home / "Documents" / "2026" / "November report.pdf")
    mk(home / "Downloads" / "invoice-nov.txt", "Total due: 42 EUR, reference TS-2001")
    mk(home / "Desktop" / "todo.md", "nothing here")
    cards = []
    out = run("find_files", {"name_or_text": "november"}, cards=cards)
    assert out.startswith("found 1 file for 'november' in your folders") and "November report.pdf" in out
    assert cards == ["Found 1 file"]
    assert "invoice-nov.txt" in run("find_files", {"name_or_text": "*.txt"})
    out = run("find_files", {"name_or_text": "TS-2001"})
    assert "invoice-nov.txt  (text inside matches)" in out
    out = run("find_files", {"name_or_text": "report", "folder": "Downloads"})
    assert out.startswith("no files found matching 'report' in") and "Downloads" in out
    assert run("find_files", {"name_or_text": ".", "max": 2}).count("\n") == 2


def test_move_files_with_wildcards_into_new_folder(home):
    d = home / "Downloads"
    for n in ("a.pdf", "b.pdf", "c.png"):
        mk(d / n)
    cards = []
    out = run("move_files", {"sources": ["Downloads/*.pdf"], "dest_folder": "Documents/Reports"}, cards=cards)
    dest = (home / "Documents" / "Reports").resolve()
    assert out == f"moved 2 files to {dest} (new folder): a.pdf, b.pdf"
    assert cards == ["Moved 2 files to Reports"]
    assert (dest / "a.pdf").exists() and not (d / "a.pdf").exists() and (d / "c.png").exists()
    with pytest.raises(BadAction, match="no files match"):
        run("move_files", {"sources": "Downloads/*.doc", "dest_folder": "Documents"})   # a plain string works too
    with pytest.raises(BadAction, match="into itself"):
        run("move_files", {"sources": ["Documents/Reports"], "dest_folder": "Documents/Reports/sub"})
    assert "1 already there" in run("move_files", {"sources": ["Documents/Reports/a.pdf"],
                                                   "dest_folder": "Documents/Reports"})


@pytest.mark.parametrize("answer", ["Yes", "No", None])
def test_move_overwrite_asks_first(home, trash, answer):
    d, r = home / "Downloads", home / "Documents"
    mk(d / "a.pdf", "new")
    mk(d / "b.pdf", "new b")
    old = mk(r / "a.pdf", "old")
    ask = Asker(answer) if answer else None
    out = run("move_files", {"sources": ["Downloads/a.pdf", "Downloads/b.pdf"], "dest_folder": "Documents"},
              ask=ask)
    if ask:
        assert ask.calls == [("Replace 1 file that already exist in Documents: a.pdf? The old ones go to the "
                              "Recycle Bin.", ["Yes", "No"])]
    if answer == "Yes":
        assert trash == [str(old.resolve())]                 # the old one went to the Recycle Bin first
        assert out.startswith("moved 2 files to") and not (d / "a.pdf").exists()
    else:
        assert not trash and old.read_text() == "old" and (d / "a.pdf").exists()
        assert "moved 1 file to" in out and "skipped 1 that already exist" in out
        assert ("you said no" in out) if answer == "No" else ("needs your OK" in out)
    assert (r / "b.pdf").read_text() == "new b"


def test_copy_rename_make_folder(home, trash):
    src = home / "Pictures" / "trip"
    mk(src / "1.jpg")
    mk(src / ".env", "no")
    out = run("copy_files", {"sources": ["Pictures/trip"], "dest_folder": "Desktop"})
    assert out.startswith("copied 1 folder to") and (home / "Desktop" / "trip" / "1.jpg").exists()
    assert not (home / "Desktop" / "trip" / ".env").exists() and (src / "1.jpg").exists()
    cards = []
    out = run("rename_file", {"path": "Pictures/trip/1.jpg", "new_name": "beach"}, cards=cards)
    assert out == f"renamed 1.jpg to beach.jpg in {src.resolve()} (kept the .jpg ending)"
    assert cards == ["Renamed to beach.jpg"] and (src / "beach.jpg").exists()
    mk(src / "x.jpg")
    with pytest.raises(BadAction, match="already exists and overwriting needs your OK"):
        run("rename_file", {"path": "Pictures/trip/x.jpg", "new_name": "beach.jpg"})
    with pytest.raises(BadAction, match="you said no"):
        run("rename_file", {"path": "Pictures/trip/x.jpg", "new_name": "beach.jpg"}, ask=Asker("No"))
    assert "renamed x.jpg to beach.jpg" in run("rename_file", {"path": "Pictures/trip/x.jpg",
                                                               "new_name": "beach.jpg"}, ask=Asker("yes please"))
    assert len(trash) == 1
    with pytest.raises(BadAction, match="not a valid name"):
        run("rename_file", {"path": "Pictures/trip/beach.jpg", "new_name": "../up.jpg"})
    assert run("make_folder", {"path": "Documents/Taxes/2026"}).startswith("made folder")
    assert (home / "Documents" / "Taxes" / "2026").is_dir()
    assert run("make_folder", {"path": "Documents/Taxes"}).startswith("folder already exists")


def test_zip_files(home, trash):
    import zipfile
    f = home / "Documents" / "proj"
    mk(f / "a.txt")
    mk(f / "sub" / "b.txt")
    mk(f / ".env", "secret")
    cards = []
    out = run("zip_files", {"sources": ["Documents/proj"], "zip_path": "Desktop/proj"}, cards=cards)
    z = home / "Desktop" / "proj.zip"
    assert out.startswith(f"zipped 2 files into {z.resolve()}") and cards == ["Zipped 2 files into proj.zip"]
    assert sorted(n.replace("\\", "/") for n in zipfile.ZipFile(z).namelist()) == ["proj/a.txt", "proj/sub/b.txt"]
    with pytest.raises(BadAction, match="already exists and overwriting needs your OK"):
        run("zip_files", {"sources": ["Documents/proj/a.txt"], "zip_path": "Desktop/proj.zip"})
    run("zip_files", {"sources": ["Documents/proj/a.txt"], "zip_path": "Desktop/proj.zip"}, ask=Asker("Yes"))
    assert trash == [str(z.resolve())]


def test_open_file(home, monkeypatch):
    opened = []
    monkeypatch.setattr(commands.os, "startfile", lambda p: opened.append(p), raising=False)
    f = mk(home / "Documents" / "r.pdf")
    assert run("open_file", {"path": "Documents/r.pdf"}).startswith("opened") and opened == [str(f.resolve())]
    with pytest.raises(BadAction, match="not found"):
        run("open_file", {"path": "Documents/missing.pdf"})
    assert len(opened) == 1


def test_delete_goes_to_recycle_bin_and_always_asks(home, trash):
    d = home / "Downloads"
    files = [mk(d / n) for n in ("a.pdf", "b.pdf", "c.pdf")]
    ask = Asker("No")
    out = run("delete_files", {"paths": ["Downloads/*.pdf"]}, ask=ask)
    assert ask.calls == [("Move 3 files to the Recycle Bin: a.pdf, b.pdf, c.pdf?", ["Yes", "No"])]
    assert out.startswith("not deleted: the user said no") and not trash
    with pytest.raises(BadAction, match="BLOCKED: deleting always needs the user's OK"):
        run("delete_files", {"paths": ["Downloads/a.pdf"]})                       # no human -> refused
    cards = []
    out = run("delete_files", {"paths": ["Downloads/a.pdf", "Downloads/b.pdf"]}, ask=Asker("Yes"), cards=cards)
    assert out == "moved 2 files to the Recycle Bin: a.pdf, b.pdf" and cards == ["2 files moved to the Recycle Bin"]
    assert trash == [str(files[0].resolve()), str(files[1].resolve())]
    assert not files[0].exists() and files[2].exists()
    assert sorted(p.name for p in (home.parent / "RecycleBin").iterdir()) == ["1_a.pdf", "2_b.pdf"]
    (d / "old").mkdir()
    ask = Asker("Yes")
    run("delete_files", {"paths": ["Downloads/old", "Downloads/c.pdf"]}, ask=ask)
    assert ask.calls[0][0] == "Move 1 file and 1 folder to the Recycle Bin: old, c.pdf?"


def test_delete_without_send2trash_never_deletes(home, monkeypatch):
    monkeypatch.setitem(sys.modules, "send2trash", None)     # import fails
    f = mk(home / "Downloads" / "a.pdf")
    with pytest.raises(BadAction, match="send2trash is not installed"):
        run("delete_files", {"paths": ["Downloads/a.pdf"]}, ask=Asker("Yes"))
    assert f.exists()


def test_os_errors_become_bad_actions(home, monkeypatch):
    mk(home / "Downloads" / "a.pdf")

    def boom(*a, **k):
        raise PermissionError("file is open in another program")
    monkeypatch.setattr(commands.shutil, "move", boom)
    with pytest.raises(BadAction, match="move_files failed: file is open"):
        run("move_files", {"sources": ["Downloads/a.pdf"], "dest_folder": "Documents"})


def test_tools_and_prompt():
    names = {t["function"]["name"] for t in agent.TOOLS}
    assert commands.TOOLS <= names and "press_and_hold" in names
    for n in commands.TOOLS:
        assert n in agent.SYSTEM
    assert "Recycle Bin" in agent.SYSTEM
    name, args = agent.parse_reply(C("move_files", sources=["Downloads/*.pdf"], dest_folder="Documents"))
    assert name == "move_files" and args["sources"] == ["Downloads/*.pdf"]
    assert agent.describe("move_files", args, {}) == 'move_files ["Downloads/*.pdf"] "Documents"'


# ============================================================================ inside the agent loops
def _files_run(home, trash, engine, answers):
    d = home / "Downloads"
    for n in ("a.pdf", "b.pdf", "c.png"):
        mk(d / n)
    s = Screen()
    n_obs = {"n": 0}

    def observe():
        n_obs["n"] += 1
        return s.observe()
    loop = setup(s, answers=answers)[0] if answers is not None else None
    script = [C("list_files", folder="Downloads"),
              C("move_files", sources=["Downloads/*.pdf"], dest_folder="Documents/Reports"),
              C("delete_files", paths=["Downloads/c.png"]),
              C("done", summary="sorted")]
    r = go(engine, script, s, loop=loop, observe=observe)
    r["n_obs"] = n_obs["n"]
    return r


@pytest.mark.parametrize("answers", [["Yes"], ["No"], None])
def test_commands_in_both_loops(home, trash, answers):
    def make(e):
        for p in (home / "Downloads").iterdir():
            p.unlink()
        shutil.rmtree(home / "Documents" / "Reports", ignore_errors=True)
        trash.clear()
        return _files_run(home, trash, e, answers)
    a, b = both(make)
    assert b["res"] == ("done", "sorted")
    nodes = b["nodes"]
    acts = [n for n in nodes if n.get("type") != "human"]
    assert [n["method"] for n in acts] == ["command", "command", "command", "none"]
    assert [n["ok"] for n in acts] == [True, True, answers is not None, True]
    hist = b["seen"][-1][2]                                      # the last prompt's history
    assert "1. list_files \"Downloads\" -> 3 items in" in hist
    assert "2. move_files [\"Downloads/*.pdf\"] \"Documents/Reports\" -> moved 2 files to" in hist
    if answers == ["Yes"]:
        assert "3. delete_files" in hist and "moved 1 file to the Recycle Bin: c.png" in hist
        assert len(trash) == 1 and acts[2]["confirm"] == [{"question": "Move 1 file to the Recycle Bin: c.png?",
                                                           "answer": "Yes"}]
    elif answers == ["No"]:
        assert "not deleted: the user said no" in hist and not trash
    else:
        assert "failed: BLOCKED: deleting always needs the user's OK" in hist and not trash   # L2 feedback
        assert "confirm" not in acts[2]                              # nobody to ask: refused without a question
    assert b["n_obs"] <= 2                                       # file commands don't need a new screenshot
    ev = b["events"]
    check_events(ev)
    assert [e["method"] for e in of(ev, "action")] == ["command", "command", "command", "none"]
    says = [e["text"] for e in of(ev, "say")]
    assert "3 items in Downloads" in says and "Moved 2 files to Reports" in says
    if answers is not None:                                     # the delete question is a paired needs_you
        ny = of(ev, "needs_you")
        assert [(e["kind"], e["detail"]) for e in ny] == [("ask", "Move 1 file to the Recycle Bin: c.png?")]
        assert of(ev, "human_done")[0]["note"] == answers[0]


def test_command_blocked_outside_allow_list_in_loop(home, tmp_path):
    secret = mk(tmp_path / "outside" / "x.txt")

    def make(e):
        return go(e, [C("move_files", sources=[str(secret)], dest_folder="Downloads"),
                      C("done", summary="k")], Screen())
    a, b = both(make)
    assert b["nodes"][0]["fail_reason"].startswith("BLOCKED:") and b["nodes"][0]["method"] == "command"
    assert secret.exists()


def test_allowed_folders_kwarg_reaches_commands(home, tmp_path, monkeypatch):
    other = tmp_path / "proj"
    mk(other / "f.txt")
    for e in ("legacy", "graph"):
        r = go(e, [C("list_files", folder=str(other)), C("done", summary="k")], Screen(),
               allowed_folders=[str(other)])
        assert r["nodes"][0]["ok"], r["nodes"][0]
    assert json.dumps(r["nodes"])
