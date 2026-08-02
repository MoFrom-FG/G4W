"""Lightweight, Python-native G4W conductor runtime."""

import os
import sys
from pathlib import Path


def _install_legacy_env_aliases() -> None:
    """Read old environment names without exposing them as new defaults."""
    legacy_prefix = "CYBER" + "BOSS_"
    for key, value in tuple(os.environ.items()):
        if key.startswith(legacy_prefix):
            os.environ.setdefault("G4W_" + key[len(legacy_prefix) :], value)


_install_legacy_env_aliases()


RUNTIME_DIR = Path(__file__).resolve().parents[2]
GA_APP_DIR = RUNTIME_DIR / "app"
if str(GA_APP_DIR) not in sys.path:
    sys.path.insert(0, str(GA_APP_DIR))

__version__ = "0.1.0"
