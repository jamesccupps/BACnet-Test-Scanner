"""Put the repo root on sys.path so the single-file scanner imports as a module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
