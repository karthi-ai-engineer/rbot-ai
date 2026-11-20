"""Puts the project root on sys.path once, so tests import the package as `rbot.*` (and `tests.live.*`)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
