# rbot-ai: Plan v1

2026-11-19 · prototype mode: build, run, look, improve

**Decisions (2026-11-19):**
- **Teams:** desktop Teams, all features, no specific workflow. No Teams web for now.
- **Accounts:** testing only, so no real accounts are needed.
- **Office:** no Office automation (COM). Office apps go through the UI.
- **Real mouse:** 6 s idle threshold; if you're busy, it asks right away.
- **Parallel tasks:** later, with a multi-level orchestrator. Get the single-task features solid first.

## 1. Goal and principles

rbot-ai is a personal agent you prompt in one always-running chat window (the **rbot-ai Hub**). It does the work on your Windows PC with the best method for each step, and you always see an animated rbot-ai doing it. Today it already works end to end on the company Qwen 3.8 27B model:
- Teams messages
- browser tasks
- human-in-the-loop (HITL) for sign-in, codes, CAPTCHA, payment and cookies

**Done looks like:** you type a task and keep working. rbot-ai finishes it in the background and steps in only when it needs your mouse or your decision.

Principles every part follows:

1. **Best method per step, not per app.** Use commands and files first, then the page read directly in the rbot-ai browser, then Windows invisible clicks. Use the real mouse only when nothing else works.
2. **Always visible.** Whatever the method, rbot-ai shows it: a real click, a ghost click, or a card.
3. **You stay in charge.** Your click takes control back at any time; the agent never fights you for the mouse.
4. **The human does the sensitive parts.** Passwords, codes, CAPTCHA and payment are always yours.
5. **One place to see everything.** Every task, question and result shows up in the Hub.
6. **Prototype pace.** Add no heavy architecture until a use case proves it needs it.

## 2. Use cases

All four areas are in scope for v1. Testing uses test data, so no real accounts are needed. Each row is one task we build and test end to end. Risk decides how much the human is involved:
- **Low** runs alone.
- **Medium** shows a preview or confirmation first.
- **High** always hands over.

| Area | Task | Main method | Human part | Risk |
| --- | --- | --- | --- | --- |
| Teams | Send a message to a named person | Desktop Teams: invisible click where it works, else real mouse | Only named, allow-listed people | Medium |
| Teams | Read the latest messages in a chat and summarise | Desktop Teams: UIA read + invisible clicks | None | Low |
| Teams | Reply in a thread with a drafted answer | Same as sending | Approve the draft before send | Medium |
| Teams | Any other Teams feature (search, calls UI, files tab, reactions, status…) | Desktop Teams, same ladder | As risk requires | Medium |
| Teams | Share a file into a chat | Real mouse (file picker) | Approve the file and the chat | Medium |
| Web | Fill a form from given data | rbot-ai browser | Sign-in, CAPTCHA if shown | Medium |
| Web | Research a topic across 3–5 sites and summarise | Web search + rbot-ai browser | None | Low |
| Web | Download a report and save it to a folder | rbot-ai browser + file commands | Sign-in if needed | Low |
| Web | Buy or book something | rbot-ai browser | Payment always by the human | High |
| Files | Sort, rename or move files (e.g. Downloads into folders) | Commands | Confirm before delete or overwrite | Medium |
| Files | Find a file by name or content and open it | Commands | None | Low |
| Files | Zip a folder and attach it somewhere | Commands, then the target app's method | As the target app | Medium |
| Office | Read an Excel sheet and answer a question | Excel UI: invisible click / real mouse | None | Low |
| Office | Fill a Word template from given data | Word UI: invisible click / real mouse | Review before saving over a file | Medium |
| Office | Draft an Outlook mail | Outlook UI: invisible click / real mouse | Always review before send | High |

**Chain example:** download the November report (web), rename it into the Reports folder (files), then send it to a named Teams chat (Teams).

> TODO (you): add your team's real tasks here. Each new task gets a row.

## 3. Method ladder and routing

Each **step** picks the first method that can do it. The model proposes the step, the router picks the method, and the method falls back down the ladder when it fails.

```
 step ──► 1. Command / file                       (no UI: fastest, most reliable)
            │ can't
            ▼
          2. rbot-ai browser (page read straight from the DOM, direct clicks into the tab)
            │ not a web page / fails
            ▼
          3. Invisible click (Windows UI Automation: Invoke / SetValue / Toggle / Select / Scroll)
            │ element refuses / no pattern
            ▼
          4. Real mouse + keyboard (mouse lock, only when you're idle; see §5)
            │ still stuck
            ▼
          5. Ask the human (stuck card in the Hub, "I'll do it" / hint / stop)
```

| Method | Used for | Seeing | Acting | Status |
| --- | --- | --- | --- | --- |
| 1. Commands / files | Files, folders, zip, search | Command output, file lists | Python (pathlib, shutil) | New |
| 2. rbot-ai browser | Websites | Tab screenshot + DOM marks | Direct input into the tab (mouse never moves) | DOM marks done; direct input new |
| 3. Invisible click | Settings, Explorer, Office, normal Windows apps, parts of Teams | Window picture (PrintWindow) + UIA marks | UIA patterns | New |
| 4. Real mouse | Desktop Teams, file pickers, anything else | Screen + UIA marks (today) | SendInput (today) | Done |
| 5. Human | Sign-in, codes, CAPTCHA, payment, stuck | – | HITL (today) | Done |

Routing rules:
- **App rule first:** a known app has a preferred method (Teams → 3 then 4; Office → 3 then 4; rbot-ai Edge → 2).
- **Fallback is automatic:** if a method fails twice for a step, the next one down is tried. The history tells the model which method was used.
- **Risky steps never fall back silently** (send, delete, buy). They go to a confirmation instead.
- The model sees one tool list. The router decides how to execute each tool, so the model doesn't need to know the methods.

## 4. What you see

Every action shows an animated rbot-ai, whatever method did the work. The animation is the main feature, not decoration.

| Method | On screen | Your real mouse |
| --- | --- | --- |
| Real mouse | rbot-ai glides to the target and clicks; the real pointer follows (as today) | Used |
| rbot-ai browser | **Ghost click**: rbot-ai glides to the element and plays the click, inside the rbot-ai browser window | Untouched |
| Invisible click | Ghost click on the element in its app window, if that window is visible | Untouched |
| Command / file | rbot-ai shows a small card, e.g. "12 files moved to Reports" | Untouched |
| Needs you | rbot-ai jumps, then walks to the bottom-left corner as the drop character (existing HITL motion) | Yours |

- A ghost click plays only where the target is visible. If its window is hidden, the Hub shows the step instead.
- **Two modes**, switched in the Hub:
  - **Live**: rbot-ai flies across the screen (today).
  - **Background**: rbot-ai stays inside the window it works in, and you keep the PC.
- Skins stay switchable: grape (default), mint, sunset.

## 5. Real mouse policy: idle 6 s, or ask (both at once)

When a step needs the real mouse, the agent doesn't sit and wait:

1. **You've been idle 6 s or more** → rbot-ai shows a 3 s notice, "Using the mouse…", with a **Not now** button, then goes.
2. **You're busy** (moving or typing) → it **asks right away**: "May I use the mouse for a moment?" with **Yes** and **Not now** buttons.
   - **Yes** → it goes now.
   - **Not now** → it waits.
   - **No answer** and you go idle for 6 s → it goes automatically, after the 3 s notice.
3. It takes the **mouse lock**, does one short burst (one step, about 1–3 s), then releases it and checks again for the next step.
4. Any real click from you during the burst is a **takeover**: it stops at once (as today).

- The 6 s threshold is a setting.
- Ctrl+Alt+Q still stops everything instantly.

## 6. rbot-ai Hub: the always-running chat + status window

```
┌─ 🍇 rbot-ai Hub──────────────────────────────────────┐
│  RUNNING                                             │
│  ● Download Nov report   🌐 rbot-ai browser 4/12      │ [watch] [pause] [stop]
│  ● Reply to Tharun       🖱 real mouse  waiting idle │ [watch] [pause] [stop]
│  NEEDS YOU                                           │
│  🔑 "Please sign in": Test Shop        [go there]    │
│  QUEUED                                              │
│  ○ Move PDFs to Reports  📁 commands                 │
│  DONE TODAY                                          │
│  ✓ Bought mug #TS-2001   ▶ video  📄 log             │
│ ──────────────────────────────────────────────────── │
│  💬 you: send the report to Tharun after download    │
│  🍇: Okay, queued after "Download Nov report".       │
│  [ type a task…                          ] [Send]   │
└──────────────────────────────────────────────────────┘
```

- **Always on:** a tray icon, a hotkey (Ctrl+Alt+B) and autostart with Windows.
- **Chat:**
  - type tasks
  - ask "what are you doing?"
  - answer questions
  - follow-ups chain tasks ("after that, send it to…")
  - voice later
- **Running:** live step, method badge and Watch / Pause / Stop. **One task at a time for now**; new tasks wait in Queued.
- **Needs you:** a single queue for every HITL request and mouse request, so several tasks never pop up at once.
- **Done:** result, smooth video, log, screenshots.
- **Settings:**
  - mode (Live / Background)
  - mouse policy
  - skin
  - recording on/off
  - screenshot retention
  - allow-lists (Teams people, folders)
- Built from today's `rbot/apps/chat_app.py` (PySide6).

## 7. Workers: where tasks run

The **Hub is the manager**; each task runs as a **worker** with its own agent loop.

```
            rbot-ai Hub (chat, queue, status, mouse lock, needs-you queue)
     ┌──────────────┬──────────────┬───────────────┬──────────────────┐
     ▼              ▼              ▼               ▼
  Commands   rbot-ai browser   My PC: UIA /    Windows Sandbox
  (files,      (DOM, tabs,      real mouse      (later: risky /
  Office)      background)      (live, lock)    test tasks)
     └──────────────┴──── same LLM gateway (Qwen) ─┴──────────────────┘
```

| Worker | Parallel? | Your PC usable? | Notes |
| --- | --- | --- | --- |
| Commands | Yes | Yes | Instant; cards only |
| rbot-ai browser | Yes, one tab per task | Yes | Own profile `.buddy_browser`; sign in once |
| My PC (UIA / real mouse) | Real mouse: one at a time (lock) | While idle, or after you say yes | Desktop Teams, Office, file pickers |
| Windows Sandbox | One sandbox | Yes | Later. Clean every run; reports back to the Hub through a shared folder |

- Only **Windows-built-in** VMs: Windows Sandbox first, Hyper-V only if needed. No cloud.
- **One task at a time for now.** Parallel tasks come later, with a multi-level orchestrator, once every single-task feature is solid.

## 8. Human in the loop, everywhere

This is already built (`rbot/human/`: hitl.py, hitl_ui.py, human_loop.py). The plan keeps it and moves it into the Hub:

| Situation | What happens | Resume |
| --- | --- | --- |
| Login / OTP / CAPTCHA | Handover: rbot-ai jumps, parks bottom-left, records | Auto when done (blocker gone + idle), or rbot-ai click |
| Payment | Handover, no screenshots saved | **Only** a rbot-ai click |
| Cookies, permissions, save-password | Question card with options | Agent acts on your answer |
| Stuck (all levels failed, or going in circles) | "I couldn't manage this step… can you help?" | I'll do it / Try again + hint / Stop |
| You take over (real click) | Agent parks | rbot-ai click; your actions are reviewed (1 fps) so it doesn't redo them |
| Mouse needed while you're busy | Asks right away (§5) | Yes / Not now / auto after 6 s idle |

- In background mode the handover also **brings the working window to the front**, then sends it back afterwards.
- All of these go into the **Needs you** queue, one at a time.

## 9. Safety and privacy

- **Never types** passwords, codes, CAPTCHA answers or card data. Keystrokes are counted, never recorded.
- **Allow-lists:**
  - Teams: only people you name.
  - Files: only folders you allow; delete/overwrite always asks.
  - Office: never sends mail or overwrites a file without review.
- **Blocked by default:**
  - the system tray
  - risky words (delete account, pay, …)
  - blocked apps and hotkeys
  - group chats
- **Recordings:** never record the `.env` / API key. Recording is opt-in, and screenshots older than N days are auto-deleted.
- **rbot-ai browser profile:** testing only, so no real accounts are needed (Edge signed it into the Work account by itself; fine for now). It stays out of git, and its 187 MB lives in the project folder.
- **Kill switch:** Ctrl+Alt+Q, plus Stop in the Hub.
- **Delete pending:** 4 old videos that show the API key (`runs/chat_20261118_134613*.mp4`, `runs/chat_20261118_134244*.mp4`).

## Status (2026-11-19, evening)

| Phase | Status |
| --- | --- |
| 0 Accuracy | Built ✅. Live stuck re-run still to do |
| 1a LangGraph loop | ✅ `rbot/core/agent_graph.py` (default engine; `BUDDY_ENGINE=legacy` = old loop) |
| 1 Hub | ✅ `rbot/hub/app.py` (+ core / widgets / views / runner / guard), run `python -m rbot`; hotkey Ctrl+Alt+B |
| 2 Router + file commands | ✅ `rbot/core/router.py`, `rbot/core/commands.py` (allow-listed folders, Recycle Bin only, asks before delete/overwrite) |
| 3 rbot-ai browser background | ✅ `rbot/browser/browser_exec.py` + `browser_dom.observe_tab` (CDP only, ghost cursor in the page) |
| 4 Mouse lock | ✅ `rbot/desktop/mouse_gate.py` (idle 6 s or ask; wired in the Hub) |
| 5 Invisible clicks | ✅ `rbot/desktop/uia_exec.py` (Invoke/Toggle/Select/Expand/SetValue/Scroll; never in Chrome-based text boxes) |
| 6 Live tests | ⏳ Needs the user at the screen |

Tests: `python -m pytest -q tests/` → 341 passed, 5 skipped.

## 10. Build phases

One task at a time, each phase usable on its own. Parallel work comes after all of these are solid.

| Phase | Builds | Done when |
| --- | --- | --- |
| **0. Finish accuracy** (in progress) | DOM marks, clutter cut, zoom refine, hover check (built); re-run the stuck test in the rbot-ai browser | Stuck test passes live |
| **1a. Agent loop as a LangGraph graph** | `rbot/core/agent_graph.py`: same behaviour as `run_task`, events for the Hub, screenshots deleted when the task ends (logs + video kept) | All loop tests pass on both engines; callers switched to the graph (`BUDDY_ENGINE=legacy` = old loop) |
| **1. Hub v1** | Always-on window: tray, hotkey, chat, Running / Needs-you / Queued / Done, one task at a time | A task runs from the Hub, HITL cards appear there |
| **2. Router + commands** | Method router; file/folder commands with cards | "Sort my Downloads" works without the UI, with cards |
| **3. rbot-ai browser background** | Direct input into the tab, tab screenshots, ghost click inside the page, window to front for HITL | A web task runs while you work in another app |
| **4. Mouse lock + idle/ask policy** | 6 s idle, ask-when-busy, 3 s notice, lock, takeover | A desktop Teams message is sent without stealing your mouse mid-typing |
| **5. Invisible clicks** | UIA patterns + PrintWindow for Teams, Office, Settings, Explorer | Teams and Office steps run without the real mouse where the app allows |
| **6. Teams + Office coverage** | Run all desktop Teams features and the Office rows of §2 through the ladder | Each §2 row passes its live test |
| **Later** | Multi-level orchestrator + parallel tasks, Windows Sandbox worker, voice, stronger model as escalation | – |

## 11. Testing and success measures

- **Local test site** (`tests/live/hitl_pages`, `python tests/live/run_hitl_live.py <scenario> [--buddy] [--record]`): every HITL scenario must pass live after each phase.
- **Automated tests:** `python -m pytest -q tests/` must stay green (now 92 passed, 3 skipped).
- **Per use case:** one scripted live run with a recording, judged on:
  - **Success**: done without help (target ≥ 8 of 10 runs).
  - **Steps**: no more than 1.5× the steps a person needs.
  - **Speed**: average step under 8 s.
  - **No wrong actions**: zero sends/deletes to anything outside the allow-lists.
- **Background mode:** you keep typing in another app during the whole run; no input is stolen.

## 12. Open questions

- [x] Teams: desktop, all features; no Teams web for now.
- [x] Accounts: testing only, so no real accounts are needed.
- [x] Office automation (COM): no. Office goes through the UI.
- [x] Real mouse: idle 6 s, otherwise ask right away.
- [x] Parallel tasks: later, with a multi-level orchestrator.
- [ ] Is Windows Sandbox enabled (needs the optional feature + virtualization)? Only matters for "Later".
