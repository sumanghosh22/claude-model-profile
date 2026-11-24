"""Repository layout used by the tests (the source of truth, not the installed copy)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "src" / "bin"
AGENTS_DIR = ROOT / "src" / "agents"
PROFILE_CLI = BIN_DIR / "model-profile"
