"""Make core.dlss._ours() recognise files written by dlss5_proton.py.

Upstream decides "is this nvngx_dlss.dll the game's own?" by looking for its
own manifest / backup suffix. Games we set up with the Proton tool carry
`_dlss5_proton_state.json` instead, so a feeder install's nvngx_dlss.dll was
being read as native DLSS -- and a game with no DLSS got steered to `native`.
"""
from __future__ import annotations

import json
from pathlib import Path

from core import dlss as _dlss

OUR_STATE = "_dlss5_proton_state.json"
_orig_ours = _dlss._ours


def _proton_written(folder: Path, name: str) -> bool:
    for probe in (folder, *folder.parents[:3]):
        st = probe / OUR_STATE
        if not st.is_file():
            continue
        try:
            data = json.loads(st.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return False
        targets = {Path(r.get("target", "")).name.lower() for r in data.get("files", [])}
        return name.lower() in targets
    return False


def _ours(folder: Path, name: str) -> bool:
    return _orig_ours(folder, name) or _proton_written(folder, name)


def install() -> None:
    _dlss._ours = _ours
    install_manifest()
    install_diagnose()


# --- let installer see installs made by dlss5_proton.py --------------------
# Their _previous_manifest() reads only their own JSON. Synthesize one from our
# state file so _previously_ours(), _previous_route() and uninstall() treat our
# files as ours -- not as "the game's own" to be backed up and preserved.
from core import installer as _inst  # noqa: E402

_ROUTE_OF = {"optiscaler": "optiscaler", "minimal": "native", "full": "native",
             "feeder": "feeder"}
_orig_prev_manifest = _inst._previous_manifest


def _from_proton_state(root: Path) -> dict | None:
    st = root / OUR_STATE
    if not st.is_file():
        return None
    try:
        data = json.loads(st.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
    files = []
    for r in data.get("files", []):
        t = r.get("target")
        if not t:
            continue
        try:
            files.append(str(Path(t).relative_to(root)))
        except ValueError:
            files.append(Path(t).name)
    return {
        "version": 1, "complete": True,
        "exe": Path(data.get("exe", "")).name or None,
        "path": _ROUTE_OF.get(data.get("mode", ""), "native"),
        "proxy": data.get("loader", ""),
        "files": files,
        "components": {"optiscaler": data.get("optiscaler_version"),
                       "feeder": data.get("feeder_version")},
        "_source": OUR_STATE,
    }


def _previous_manifest(root: Path) -> dict | None:
    return _orig_prev_manifest(root) or _from_proton_state(root)


def install_manifest() -> None:
    _inst._previous_manifest = _previous_manifest


# diagnose.analyse() has its own manifest reader; patch it the same way, or a
# verify on one of our installs reports "(no manifest)" and picks the wrong log.
from core import diagnose as _diag  # noqa: E402

_orig_diag_manifest = _diag._manifest


def _diag_manifest(install_dir: Path) -> dict:
    return _orig_diag_manifest(install_dir) or _from_proton_state(install_dir) or {}


def install_diagnose() -> None:
    _diag._manifest = _diag_manifest
    # New core (2.x): the reader lives in diagnose.evidence; patch it too.
    try:
        import importlib as _il
        _ev = _il.import_module("core.diagnose.evidence")
        if hasattr(_ev, "_manifest"):
            _orig_ev = _ev._manifest

            def _ev_manifest(install_dir: Path) -> dict:
                return _orig_ev(install_dir) or _from_proton_state(install_dir) or {}

            _ev._manifest = _ev_manifest
    except Exception:
        pass
