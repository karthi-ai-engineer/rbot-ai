# rbot-ai

A personal Windows agent you prompt from one always-running chat window, the **rbot-ai Hub**. For each step it
picks the best method: file commands, the rbot-ai browser (page read from the DOM), invisible UI Automation clicks,
and the real mouse only as a last resort. An animated rbot-ai buddy always shows what it is doing, and it hands
sign-ins, codes, CAPTCHA and payment over to you (human in the loop). It runs on the company LLM gateway (Qwen).
See [PLAN.md](PLAN.md) for goals, decisions and status.

## Setup

```
pip install -r requirements.txt
copy .env.example .env              # then put your gateway key in .env
```

## Run (from the project root)

| What | Command |
| --- | --- |
| Hub (tray + chat, Ctrl+Alt+B shows it, Ctrl+Alt+Q stops everything) | `python -m rbot` |
| Hub demo (scripted fake run, nothing is clicked) | `python -m rbot --demo` |
| Hub screenshots, offscreen, into `runs/` | `python -m rbot --sheet` |
| Hub self-test, offscreen | `python -m rbot --selftest` |
| Old chat window | `python -m rbot.apps.chat_app` |
| Teams agent (allow-listed chats) | `python -m rbot.apps.teams_agent --chat "Name" "task"` |
| Agent loop from the command line | `python -m rbot.core.agent "open notepad and type hello"` |
| rbot-ai browser | `python -m rbot.browser.browser_dom launch <url>` (also `marks`, `observe`, `close`) |
| Perception once (saves `runs/observe_marked.png`) | `python -m rbot.desktop.perception` |
| Buddy cursor demo | `python -m rbot.desktop.cursor_overlay` |
| Grounding test | `python -m rbot.desktop.grounding --n 10` |
| Gateway smoke test | `python scripts/check_connection.py` |
| HITL UI preview | `python scripts/hitl_ui_preview.py [--sheet out.png \| --selftest]` |
| Video smart edit | `python scripts/smart_edit.py out.mp4 "clip.mp4\|caption" ...` |

## Folders

```
rbot/              the package (rbot.ROOT / RUNS / DATA = project root, runs/, data/)
  __main__.py      python -m rbot  ->  the Hub
  llm/             gateway client (reads .env)
  core/            agent loop (agent.py legacy, agent_graph.py LangGraph), method router, file commands
  desktop/         perception, grounding, real mouse/keyboard (actions), mouse lock, UIA invisible clicks, buddy overlay
  browser/         rbot-ai browser: DOM marks (browser_dom) and background tab actions (browser_exec)
  human/           human in the loop: blocker detection (hitl), cards + handover UI (hitl_ui), HumanLoop
  hub/             the Hub: app, core (settings / model, no Qt), guard, runner, widgets, views
  apps/            standalone chat window and Teams agent
scripts/           check_connection, smart_edit, hitl_ui_preview
tests/             offline pytest suite (conftest.py puts the project root on sys.path)
  live/            local HITL test site (hitl_pages, hitl_server.py), run_hitl_live.py, HITL_TEST_PLAN.md
data/              settings.json (Hub settings), history.jsonl (Hub task history); not in git
runs/              outputs only: task runs, videos, screenshots; not in git
.buddy_browser/    the rbot-ai browser profile; not in git
```

## Environment variables

| Variable | Meaning |
| --- | --- |
| `twave_api` | gateway API key (in `.env`) |
| `TWAVE_BASE_URL`, `TWAVE_MODEL` | gateway URL and model overrides |
| `ESCALATION_MODEL` | optional stronger model for the last escalation level |
| `TOOL_CHOICE` | `auto` (default) or `required` |
| `BUDDY_ENGINE` | `graph` (default, LangGraph loop) or `legacy` |
| `BUDDY_BROWSER` | `0` turns off DOM marks from the rbot-ai browser |
| `BUDDY_INVISIBLE` | `0` turns off UIA invisible clicks |
| `BUDDY_ALLOWED_FOLDERS` | folders the file commands may touch |
| `BUDDY_IDLE_S`, `BUDDY_MOUSE_POLICY` | real-mouse policy overrides (else `data/settings.json`) |
| `HUB_SCALE` | Hub UI scale |

## Tests

```
python -m pytest -q tests/                          # offline: no mouse, keyboard, screen or LLM
python tests/live/run_hitl_live.py --list           # live HITL scenarios (a person must sit at the PC)
python tests/live/run_hitl_live.py stuck --dry-run  # check wiring + page only
```

`BUDDY_LIVE=1` / `HITL_LIVE=1` also run the few live tests in the suite.
