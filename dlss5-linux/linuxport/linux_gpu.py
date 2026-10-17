"""Linux backend for core.gpu: nvidia-smi instead of the Windows registry.

core.gpu.detect() reads DriverDesc out of HKLM on Windows and returns
(name, sm) where sm is the CUDA compute capability as an int (120 = RTX 50,
89 = RTX 40, 86 = RTX 30, 75 = RTX 20). Everything downstream -- the route
steer in dlss.detect(), gpu.order_dlssnr(), the tier notes -- keys off that
int, so the shape must be preserved exactly.
"""
from __future__ import annotations

import shutil
import subprocess

from core import gpu as _gpu

_QUERY = ["--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader"]


def _smi() -> tuple[str, str, str] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, *_QUERY], capture_output=True, text=True,
                             timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    parts = [p.strip() for p in out.splitlines()[0].split(",")]
    return (parts + ["", "", ""])[:3]  # type: ignore[return-value]


def detect() -> tuple[str | None, int | None]:
    row = _smi()
    if not row:
        return None, None
    name, _driver, cap = row
    sm = None
    if cap:
        try:
            major, minor = cap.split(".")
            sm = int(major) * 10 + int(minor)
        except ValueError:
            sm = _gpu.sm_for_name(name)
    return name or None, sm


def driver_version() -> str | None:
    row = _smi()
    return row[1] or None if row else None


def install() -> None:
    install_gates()
    """Point core.gpu at this backend. Call once, before anything uses gpu."""
    _gpu.detect = detect
    _gpu.driver_version = driver_version


# --- driver version gates ------------------------------------------------------
# Upstream compares against Windows release numbers (616.56 = the first Game
# Ready driver that ships nvngx_dlssnr.dll). Linux numbering is a different
# series: the 610.x branch here runs the neural pass in every game tried, and
# the runtime comes from the archive beside the tools when the driver store
# lacks it. So the gate passes on any Linux driver from the 610 branch up,
# rather than warning "update to 616.56" against a number that will never be.
_orig_at_least = _gpu.driver_at_least if hasattr(_gpu, "driver_at_least") else None


def driver_at_least(want: str, have: str | None = None) -> bool | None:
    have = have or _gpu.driver_version()
    try:
        major = int(str(have).split(".")[0])
    except (TypeError, ValueError):
        return None
    if major >= 610:
        return True
    return _orig_at_least(want, have) if _orig_at_least else None


def install_gates() -> None:
    if _orig_at_least:
        _gpu.driver_at_least = driver_at_least
