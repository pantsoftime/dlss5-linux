"""The out-of-process route: bmitch87/DLSS5VKLayer.

A Linux Vulkan implicit layer (VK_LAYER_NV_dlssnr) captures the presented
swapchain image and hands it, over shared memory, to a separate Windows helper
(dlssnr_helper.exe) that runs under a Proton/Wine runner and drives
nvngx_dlssnr.dll through its VULKAN NGX exports. Motion vectors are synthesised
with VK_NV_optical_flow, depth is zero, the frame comes back composed.

Why it is a route of its own here (measured 2026-09-10/11, RTX 5090, 615.71):

* nothing is written into the game folder -- no proxy DLL, no ReShade, no
  OptiScaler -- and the game presents its own frames if the helper is absent;
* anything that presents through Vulkan is covered: DXVK D3D9/10/11,
  vkd3d-proton D3D12, and native Linux Vulkan games, which no other route
  reaches;
* on a D3D12 game WITHOUT DLSS the model never touches the game's device, so
  the feeder's same-device CreateFeature fault (FF7 Remake, vkd3d-proton +
  the NVAPI cubin path) is not on the path. Remake runs on it.

Costs: post-present (the HUD gets the model too), synthetic vectors and no
depth (a game WITH DLSS is still better served by OptiScaler, which keeps real
vectors and depth and runs before the UI), no upscaling, one shared-memory
copy each way per frame, and the present thread blocked while the model
evaluates (15.35 ms per frame measured on FF7 Remake at 5120x1440 and full
working scale, 10.06 ms of it inference; workingscale is the lever). Zero-copy
dma-buf cannot engage under a Wine runner: winevulkan exposes no fd-based
external-memory extension. Controls are out-of-process too (dlssnr-gui,
dlssnr-shmctl, an evdev hotkey that needs 0.3.0-2+): there is no overlay,
because nothing runs inside the game.

This module knows the install, reads its logs, writes the one launch token,
and names the runtime by hash. It never installs the layer itself: the
upstream tarball's install.sh does that (see examples/vklayer/README.md).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import proton

ENV = "VKLayer_DLSS5=1"
UPSTREAM = "https://github.com/bmitch87/DLSS5VKLayer"
# The wrapper beside the port: starts the helper before the game and stops it
# after, because the layer does neither (see vklayer-run's header).
WRAPPER = Path(__file__).resolve().parents[1] / "vklayer-run"

_HOME = Path.home()
RUNTIME_DIR = Path(f"/tmp/dlssnr-{os.getuid()}")      # not $XDG_RUNTIME_DIR: Steam's container makes that private
SHM = RUNTIME_DIR / "shm.bin"
PID = RUNTIME_DIR / "helper.pid"
STATE = Path(os.environ.get("XDG_STATE_HOME") or _HOME / ".local/state") / "dlssnr"
LOG = STATE / "helper.log"
DATA = Path(os.environ.get("XDG_DATA_HOME") or _HOME / ".local/share") / "dlssnr"
BINARIES = DATA / "binaries"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or _HOME / ".config") / "dlssnr" / "config.ini"
MANIFESTS = [
    Path(os.environ.get("XDG_DATA_HOME") or _HOME / ".local/share") / "vulkan/implicit_layer.d/VK_LAYER_NV_dlssnr.x86_64.json",
    Path("/usr/local/share/vulkan/implicit_layer.d/VK_LAYER_NV_dlssnr.x86_64.json"),
    Path("/usr/share/vulkan/implicit_layer.d/VK_LAYER_NV_dlssnr.x86_64.json"),
]

# nvngx_dlssnr.dll by sha256. The first is NVIDIA's own signed 310.8 build
# for RTX 50 (content digest matches its Authenticode signature); the second
# is ShortFuse's cross-generation build the wilsjo2 guide prescribes for RTX
# 20/30/40; the third circulates widely, carries NVIDIA's signature over
# different content, and works -- but is not the file to hand a helper.
KNOWN_RUNTIMES = {
    "e16bcf15e16e13f527491cdf7845b2fe6521a738d8f7c9c721866a8496e1fc8e": "NVIDIA-signed 310.8 (RTX 50)",
    "e67dee209320cdafe0e93e45675d7aa34323a53acc57a72b2e40a181581c989a": "ShortFuse cross-generation 310.8 (RTX 20/30/40)",
    "8270b350cd82de5ce89806872cdd6b6a9249b80836b91bbeb3573470744cc206": "patched 310.8.0 (signature does not cover its content; prefer the signed file)",
}

CONTROLS = [
    "no in-game overlay on this route: nothing runs inside the game. Settings take effect on the next frame.",
    "dlssnr-gui  -- every setting as a row (style, intensity, tone, structure, passes, working scale, HDR mode, "
    "white point, motion quality), profiles, split-screen compare, frame hold, debug views. Closing it stops the helper.",
    f"dlssnr-shmctl {SHM} settings | set <key> <value> | toggle enabled   -- the same from a shell",
    "toggle hotkey: DLSSNR_TOGGLE_KEY=F10 in the launch options, or 'Toggle key' in the GUI "
    "(evdev: your user in the 'input' group). Needs DLSS5VKLayer 0.3.0-2 or newer: before it a bound key "
    "stalled the present thread ~122 ms once a second (upstream #12).",
    "A/B without a second run: set compare 1 (split screen) or hold 1 (freeze the model's input)",
    "the helper is a daemon that spins ~18% of one core while idle, and the layer never starts or stops it: "
    "put `vklayer-run` in front of %command% (launch-options --vklayer does) and it starts the helper, opens "
    "dlssnr-gui beside the game (alt-tab to it; minimise, never close: closing stops the helper), and stops "
    "both after. DLSSNR_GUI=0 skips the window, DLSSNR_KEEP=1 keeps everything up. Without the wrapper: "
    "`vklayer start` before, `vklayer stop` after.",
]


# --- install state -----------------------------------------------------------
def manifest() -> Path | None:
    return next((m for m in MANIFESTS if m.is_file()), None)


def helper_exe() -> str | None:
    return shutil.which("dlssnr-helper")


def shmctl() -> str | None:
    return shutil.which("dlssnr-shmctl")


def installed() -> bool:
    return manifest() is not None and helper_exe() is not None


def helper_running() -> bool:
    try:
        pid = int(PID.read_text().split()[0])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def patched_install() -> str | None:
    """A locally patched layer/helper is in place when the installer left the shipped
    files beside them as *.orig-<version> (see install-patched.sh)."""
    lib = _HOME / ".local/lib/dlssnr"
    origs = sorted(lib.glob("helper/dlssnr_helper.exe.orig-*")) + sorted(lib.glob("layer/libVkLayer_NV_dlssnr.so.orig-*"))
    if not origs:
        return None
    return "patched build in place (shipped " + ", ".join(sorted({o.name.split(".orig-")[-1] for o in origs})) + " kept beside it)"


def runtime() -> tuple[str | None, str]:
    """(sha256, label) of the nvngx_dlssnr.dll the helper will load."""
    p = BINARIES / "nvngx_dlssnr.dll"
    if not p.is_file():
        return None, "missing -- dlssnr-helper import-binaries <dir with nvngx_dlssnr.dll>"
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    return digest, KNOWN_RUNTIMES.get(digest, f"unknown build ({p.stat().st_size} bytes)")


def _kv(cmd: list[str]) -> dict[str, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def shm_status() -> dict[str, str]:
    return _kv([shmctl(), str(SHM), "status"]) if shmctl() and SHM.exists() else {}


def shm_settings() -> dict[str, str]:
    return _kv([shmctl(), str(SHM), "settings"]) if shmctl() and SHM.exists() else {}


# --- launch options --------------------------------------------------------
def enabled_in(options: str | None) -> bool:
    """The token, or the wrapper (which exports the token itself)."""
    return bool(options) and bool(re.search(r"(^|\s)VKLayer_DLSS5=1(\s|$)|(^|[\s/])vklayer-run(\s|$)", options))


def with_layer(line: str | None, on: bool = True) -> str:
    """Add or remove the route in a launch-options string.

    On: the wrapper in front of %command% when it exists beside the port (it
    starts and stops the helper around the game), else the bare token. Off:
    both forms removed."""
    line = re.sub(r"\s*VKLayer_DLSS5=\S+", "", line or "")
    line = re.sub(r"(^|\s)\S*vklayer-run(?=\s|$)", "", line).strip()
    if "%command%" not in line:
        line = (line + " %command%").strip()
    if not on:
        return line
    if WRAPPER.is_file():
        return line.replace("%command%", f"{WRAPPER} %command%", 1)
    return f"{ENV} {line}"


def launch_line(g, on: bool = True, indicator: bool | None = None) -> str:
    """The current options with the token added (or removed). Nothing else is touched:
    in-process overrides stay if the game still has that payload."""
    cur = proton.current_launch_options(g) or ""
    line = with_layer(cur, on)
    if indicator is not None:
        line = proton.with_indicator(line, indicator)
    return line


def stale_overrides(g) -> str | None:
    """WINEDLLOVERRIDES still in the options after the in-process payload was removed."""
    cur = proton.current_launch_options(g) or ""
    m = re.search(r'WINEDLLOVERRIDES="([^"]*)"', cur)
    if m and not proton.installed_proxy(g):
        return m.group(1)
    return None


# --- when to suggest it -------------------------------------------------------
def suggestion(g, native_dlss: bool) -> str | None:
    """One line for recommend/report, or None when the in-process routes are the better answer."""
    api = (g.api or "").upper()
    state = "installed" if installed() else f"not installed -- see examples/vklayer ({UPSTREAM})"
    if not native_dlss and api == "DX12":
        return (f"DLSS5VKLayer ({state}): the feeder's same-device create faults on D3D12 under Proton; "
                f"the layer runs the model out of process and works here (FF7 Remake). "
                f"launch-options --vklayer --apply, then dlssnr-helper start.")
    if api in ("VULKAN", "VK"):
        return f"DLSS5VKLayer ({state}): a Vulkan title presents straight through the layer; no file in the game folder."
    if not native_dlss:
        return (f"DLSS5VKLayer ({state}): alternative to the feeder with nothing in the game folder "
                f"(synthetic vectors, no depth, post-present).")
    return None


# --- verify ----------------------------------------------------------------
def _proton_log(g) -> Path | None:
    appid = proton.appid_for(g)
    if not appid:
        return None
    cands = [Path(os.environ.get("PROTON_DEBUG_DIR", "/tmp")) / f"steam-{appid}.log",
             _HOME / f"steam-{appid}.log", Path("/tmp") / f"steam-{appid}.log"]
    have = [p for p in cands if p.is_file()]
    return max(have, key=lambda p: p.stat().st_mtime) if have else None


def verify(g) -> list[tuple[str, str, str]]:
    """[(level, title, detail)] for the layer route -- install, helper, runtime, the last run."""
    out: list[tuple[str, str, str]] = []
    if not installed():
        out.append(("BAD", "vk layer", f"not installed (no VK_LAYER_NV_dlssnr manifest or dlssnr-helper on PATH) -- {UPSTREAM}"))
        return out
    out.append(("OK", "vk layer", f"installed: {manifest()}"))
    if patched_install():
        out.append(("INFO", "vk layer build", patched_install()))
    cur = proton.current_launch_options(g)
    out.append(("OK" if enabled_in(cur) else "BAD", "launch token",
                f"{ENV} present" if enabled_in(cur) else f"{ENV} missing -- the layer stays inert: launch-options --vklayer --apply"))
    stale = stale_overrides(g)
    if stale:
        out.append(("INFO", "overrides", f'WINEDLLOVERRIDES="{stale}" is still set with no in-process payload; harmless, remove when convenient'))
    wrapped = bool(cur and "vklayer-run" in cur)
    out.append(("OK" if helper_running() or wrapped else "BAD", "helper",
                "running" if helper_running() else
                ("stopped; vklayer-run in the launch options starts it with the game and stops it after" if wrapped
                 else "not running -- dlssnr-helper start (it must be up before the game), or use vklayer-run")))
    if helper_running() and not (cur and "vklayer-run" in cur):
        out.append(("INFO", "helper idle cost", "the helper is a daemon: it outlives the game and its wait loop spins "
                    "~18% of one core with no frames coming (measured on 0.3.0-3). `vklayer stop` after playing, "
                    "or put vklayer-run in the launch options and it is stopped for you."))
    digest, label = runtime()
    lvl = "OK" if digest and digest.startswith("e16bcf15") or digest and digest.startswith("e67dee20") else ("WARN" if digest else "BAD")
    out.append((lvl, "NR runtime", f"{label}" + (f"  sha256 {digest[:12]}..." if digest else "")))

    st = shm_status()
    if st:
        frames, hf = int(st.get("layer_frames", 0) or 0), int(st.get("helper_frames", 0) or 0)
        up = st.get("model_up") == "1"
        out.append(("OK" if frames and up else ("WARN" if frames else "INFO"), "frames",
                    f"{frames} through the layer, {hf} answered, model {'up' if up else 'down'}, "
                    f"{st.get('width', '?')}x{st.get('height', '?')}" if frames else "none yet (counters reset when the helper restarts)"))
        hk = {"0": "none", "1": "float16 (linear)", "2": "PQ10 (HDR10)"}.get(st.get("hdr_detected", "0"), st.get("hdr_detected", "?"))
        proxy = {"1": "8-bit", "2": "float16"}.get(st.get("proxy_format", ""), st.get("proxy_format", "?"))
        if st.get("hdr_detected", "0") != "0":
            out.append(("WARN" if proxy == "8-bit" else "OK", "HDR",
                        f"swapchain {hk}, proxy {proxy}" + (" -- the model saw PQ code values as an SDR picture; "
                        "the helper only builds the float proxy when GetFeatureRequirements reports HDR, "
                        "and that query fails on this runtime (upstream). Play in SDR for now." if proxy == "8-bit" else "")))
    st_set = shm_settings()
    if st_set and st_set.get("togglekey", "0") != "0":
        out.append(("INFO", "toggle hotkey", f"togglekey={st_set.get('togglekey')} is bound; fine on DLSS5VKLayer "
                    "0.3.0-2 or newer. Older builds re-open every /dev/input node once a second on the present "
                    "thread (~122 ms stall here, upstream #12) -- upgrade, or set it to 0"))
    if st_set:
        out.append(("INFO", "settings", f"enabled={st_set.get('enabled')} passes={st_set.get('passes')} "
                    f"workingscale={st_set.get('workingscale')} transfer={st_set.get('transfer')} "
                    f"mvec={st_set.get('mvec')}/{st_set.get('mvecquality')} hdrmode={st_set.get('hdrmode')} "
                    f"togglekey={st_set.get('togglekey')}"))

    text = LOG.read_text(encoding="utf8", errors="replace")[-2_000_000:] if LOG.is_file() else ""
    if text:
        m = re.search(r"VULKAN_CreateFeature\(18\) pass 0 -> (0x[0-9a-f]+).*?size=(\d+x\d+) in (\d+) ms", text)
        if m:
            ok = m.group(1) == "0x1"
            out.append(("OK" if ok else "BAD", "NR feature", f"create -> {m.group(1)} at {m.group(2)} in {m.group(3)} ms"
                        + ("" if ok else " (0xbad0000b = core rejected, 0xbad00002 = snippet init rejected)")))
        elif "context ready" in text:
            out.append(("INFO", "NR feature", "helper up, no create yet -- the first frame from a game creates it"))
        if "[fd] dma-buf exchange ready" in text:
            out.append(("OK", "transport", "dma-buf zero-copy"))
        elif "transport imported" in text:
            out.append(("INFO", "transport", "shared memory (dma-buf needs fd external-memory extensions winevulkan does not expose)"))
        if re.search(r"GetFeatureRequirements -> 0xbad", text):
            out.append(("INFO", "requirements", "GetFeatureRequirements rejected on this runtime -- HDR capability unknown, helper stays 8-bit"))
        if "[helper] neural ready" in text:
            out.append(("OK", "neural", re.findall(r"\[helper\] neural ready (\S+)", text)[-1]))

    plog = _proton_log(g)
    if plog:
        ptext = plog.read_text(encoding="utf8", errors="replace")[-4_000_000:]
        lines = [l for l in ptext.splitlines() if "[dlssnr-layer]" in l]
        if lines:
            builds = sum("[comp] building" in l for l in lines)
            sw = re.findall(r"swapchain \S+ (\d+x\d+) fmt=(\d+) hdr=(\d)", ptext)
            if sw:
                out.append(("INFO", "swapchain", f"{sw[-1][0]} format {sw[-1][1]} hdr={sw[-1][2]} (0 none, 1 float, 2 PQ10)"))
            if builds > 10:
                out.append(("WARN", "rebuild", f"composition rebuilt {builds} times (once per frame) -- upstream compare bug on PQ10 "
                            "swapchains with the 8-bit proxy (composition.cpp Prepare); play in SDR until it is fixed"))
            elif builds:
                out.append(("OK", "composition", f"built {builds} time(s)"))
            if any("no answer" in l for l in lines):
                out.append(("BAD", "helper answer", "the layer waited on the helper and passed frames through -- helper down, or a different shm mapping"))
            if any("composition cannot run" in l for l in lines):
                out.append(("BAD", "composition", next(l for l in lines if "composition cannot run" in l)[-140:]))
        else:
            out.append(("WARN", "proton log", f"{plog.name}: no [dlssnr-layer] lines -- launched without {ENV}, or before the layer was installed"))
    return out
