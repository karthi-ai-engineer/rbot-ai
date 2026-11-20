"""rbot-ai: a personal Windows agent you prompt from the always-running Hub (see README.md / PLAN.md).

Single source of truth for project paths.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent    # project root (.env, scripts/, .buddy_browser/)
RUNS = ROOT / "runs"                              # outputs only: task runs, videos, screenshots
DATA = ROOT / "data"                              # app state: settings.json, history.jsonl
