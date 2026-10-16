"""XDG replacements for the five places core reads %LOCALAPPDATA%.

Upstream keeps cache, prefs, its own log and the standalone-dlssnr log under
%LOCALAPPDATA%\\dlss5-autopilot. On Linux those belong under XDG_CACHE_HOME
and XDG_CONFIG_HOME. Patched at import so nothing downstream changes.
"""
from __future__ import annotations

import os
from pathlib import Path

from core import diagnose, log, net, prefs

APP = "dlss5-linux"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP


def install() -> None:
    net.CACHE = CACHE / "cache"
    prefs.FILE = CONFIG / "settings.json"
    log.DIR = STATE
    try:
        from core.diagnose import model as _model
        _model.STANDALONE_LOG = STATE / "standalone-dlssnr.log"
    except Exception:
        pass
    try:
        diagnose.STANDALONE_LOG = STATE / "standalone-dlssnr.log"
    except Exception:
        pass
    for d in (net.CACHE, CONFIG, STATE):
        d.mkdir(parents=True, exist_ok=True)
