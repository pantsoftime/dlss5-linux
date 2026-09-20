#!/usr/bin/env python3
"""dlss5-proton -- install the RenoDX DLSS5 ReShade add-on into Proton games.

Linux counterpart to the Windows "DLSS5 Easy Installer". Same shape: resolve a
game, run a compatibility scan, back up every file it touches, install ReShade
(add-on build) plus the add-on and the DLSS runtime, and offer one-click
restore. The Proton-specific work is prefix discovery, WINEDLLOVERRIDES, and
knowing that on this machine the DLSS runtime usually lives in the prefix's
system32 rather than beside the game executable.

No NVIDIA binaries are bundled; you supply the DLSS ZIP.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADDON_NAME = "renodx-dlss5++.addon64"
ADDON_SHA256 = "49369954948b720e3154dbef58409084b5253ae7cc26f5c3fcdf22eb3acd63c9"

RESHADE_VERSION = "6.8.0"
RESHADE_URL = f"https://reshade.me/downloads/ReShade_Setup_{RESHADE_VERSION}_Addon.exe"
RESHADE_SHA256 = "afe4c8f13048306307983b8b3d41d5bf00a86820440b0e57dea10950e1176445"

STATE_NAME = "_dlss5_proton_state.json"
BACKUP_DIR_NAME = "_dlss5_proton_backup"

CACHE_ROOT = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / "dlss5-proton"

# Minimal mode: the add-on plus the neural-rendering runtime only. This leaves
# the game's (or Proton's) existing DLSS stack alone.
MINIMAL_FILES = [ADDON_NAME, "nvngx_dlssnr.dll"]

# Advanced mode: the whole Streamline/DLSS set from the user ZIP.
FULL_FILES = [
    ADDON_NAME,
    "nvngx_dlssnr.dll",
    "sl.dlss_nr.dll",
    "sl.common.dll",
    "sl.interposer.dll",
    "sl.deepdvc.dll",
    "sl.dlss.dll",
    "sl.dlss_d.dll",
    "sl.dlss_g.dll",
    "sl.nis.dll",
    "sl.pcl.dll",
    "sl.reflex.dll",
    "nvngx_dlss.dll",
    "nvngx_dlssd.dll",
    "nvngx_dlssg.dll",
]

# --- OptiScaler + DLSS Neural Rendering (default engine) ---------------------
# https://github.com/Dagherbou/OptiScaler_DLSSNR
# Runs the neural-rendering model straight after the game's own upscaler, on the
# same command list, before the UI is drawn. No ReShade involved at all, and it
# ships its own nvngx caller shim -- which is what makes it work under Proton,
# where the Windows driver-store layout the RenoDX add-on expects doesn't exist.
# DEFAULT BUILD: the y4my4my4m fork's nightly of 2026-09-06 (7b7220bb).
# https://github.com/y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG -- a fork of
# Dagherbou's fork on the OptiScaler-master base, carrying Proton fixes (overlay
# drawn on the game's queue under vkd3d-proton, DLSS-G menu interlock, no DXGI
# adapter enumeration in Vulkan device creation) plus NR multipass / per-pass
# settings / MFG. Promoted 2026-09-06 after passing on FF7 Rebirth (dxgi), 007
# First Light (dxgi -- the title that deadlocked on every v0.2.x build) and
# Horizon (winmm), all with HDR on, and with `Upscaler support ... dlss: true`
# where v0.1.2 was forced onto XeSS. Write-up: FINDINGS.md
#
# The "nightly" tag is ROLLING: upstream replaces its asset daily, so the URL
# below will stop matching OPTISCALER_SHA256 as soon as it does. That is by
# design -- an unverified build must not slide in as the default. Keep the
# archive in DLSS5_work/ (the lookup below finds it there); to move the pin,
# test the new archive on ONE game with `install --optiscaler-zip <archive>`,
# then update VERSION/ZIP/URL/SHA256 together.
OPTISCALER_VERSION = "y4my4m-nightly-20260906"
OPTISCALER_TAG = "nightly"
OPTISCALER_BUILD = "7b7220bb"
# Local name carries the fork + date; upstream's asset is just OptiScaler_v10.0.0-pre1_20260906.7z
OPTISCALER_ZIP = "OptiScaler_v10.0.0-pre1_20260906_y4my4m-nightly.7z"
OPTISCALER_URL = ("https://github.com/y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG/releases/download/"
                  f"{OPTISCALER_TAG}/OptiScaler_v10.0.0-pre1_20260906.7z")
OPTISCALER_SHA256 = "206c52cd59d6108f012968f00a492d049c78e77ad2e850eb7770ad8c29c444dd"

# Fallback: Dagherbou/OptiScaler_DLSSNR v0.1.2, the last good build before the
# v0.2.x deadlocks. Not used automatically; pass it with --optiscaler-zip if the
# default regresses a game (tag "v0.1.2-dIssnr" -- the capital-I typo is upstream's).
OPTISCALER_FALLBACK_ZIP = "OptiScaler-DLSSNR-v0.1.2.zip"
OPTISCALER_FALLBACK_SHA256 = "4ecf3c3d4a1c23637144855fc327f4b33079370219188ca0e198c7cb6ed58f36"

# OptiScaler.dll is renamed to one of these; it exports all of their surfaces,
# so the usable set is wider than ReShade's.
OPTISCALER_PROXY_NAMES = ["dxgi.dll", "winmm.dll", "version.dll", "dbghelp.dll",
                          "d3d12.dll", "wininet.dll", "winhttp.dll"]

# Everything from the archive that belongs in the game folder. The loader
# (OptiScaler.dll) is handled separately since it gets renamed.
OPTISCALER_FILES = [
    "OptiScaler.ini",
    "nvngx.dll_dlssnr.dll",
    "OptiScaler/amd_fidelityfx_framegeneration_dx12.dll",
    "OptiScaler/amd_fidelityfx_loader_dx12.dll",
    "OptiScaler/amd_fidelityfx_upscaler_dx12.dll",
    "OptiScaler/amd_fidelityfx_vk.dll",
    "OptiScaler/libxell.dll",
    "OptiScaler/libxess.dll",
    "OptiScaler/libxess_dx11.dll",
    "OptiScaler/libxess_fg.dll",
    "OptiScaler/D3D12_OptiScaler/D3D12Core.dll",
]
OPTISCALER_LOADER_SOURCE = "OptiScaler.dll"

# WINEDLLOVERRIDES is ALWAYS required for the proxy DLL. Proton marking these
# names "native" only chooses native over builtin -- it does not make the game
# folder outrank system32, where Proton puts DXVK/VKD3D. Verified: without the
# override Wine loaded builtin WINMM and system32 dxgi, ignoring both app-local
# proxies.


# --- DLSS5-Feeder -----------------------------------------------------------
# https://github.com/jlrouzies-fr/DLSS5-Feeder
# Synthesises a DLAA contract from ReShade's depth buffer plus estimated motion
# vectors and drives the neural-rendering add-on through a private D3D12
# device. That is what makes games with NO DLSS of their own -- including D3D11
# titles like most Unity games -- viable targets.
# Pin moved 2026-09-07 from v0.12.0 to 0.14.0-beta.5 after Dreamfall Chapters
# (D3D11, private-device session) ran clean on it with VORT motion vectors and
# the classic RenoDX add-on. The 0.14 line names the failing create step,
# retries once with DRED disarmed and refuses to re-enter an add-on whose
# locks an unwind skipped. NOTE: on a D3D12 game (same-device session) the
# 0.14.0-beta.4 feed came out black on a 10-bit PQ swapchain where 0.12.0's
# was visible -- see FINDINGS.md; that route is broken there anyway.
FEEDER_VERSION = "v0.14.0-beta.5"
FEEDER_ZIP = "DLSS5-Feeder-0.14.0-beta.5.zip"
FEEDER_URL = ("https://github.com/jlrouzies-fr/DLSS5-Feeder/releases/download/"
              f"{FEEDER_VERSION}/{FEEDER_ZIP}")
FEEDER_ZIP_SHA256 = "5f641b1b625ba0fdd23a8e3b16b5d6b173d7bdc34b4405fd8992b78fb1482ab0"
FEEDER_PREVIOUS = ("v0.12.0", "DLSS5-Feeder-0.12.0.zip",
                   "e970537996f6e73dce9a510b9e015fad19f148ee736dc4f518ccdebf6f012558")
FEEDER_ADDON = "dlss5-feed.addon64"
FEEDER_SHADER = "DLSS5_Feed.fx"
# Where each lives inside the archive (0.12.0 and 0.14.x nest the shader).
FEEDER_ZIP_MEMBERS = {FEEDER_ADDON: "dlss5-feed.addon64",
                      FEEDER_SHADER: "reshade-shaders/Shaders/DLSS5_Feed.fx"}
FEEDER_CFG_NAME = "dlss5-feed.cfg"
# The feeder locates the neural-rendering add-on by this exact filename
# (hard-coded in dlss5-feed.addon64). The bundled build is named
# "renodx-dlss5++.addon64" by the Windows installer, which the feeder does
# not recognise -- so in feeder mode it is installed under the name the
# feeder expects, and only that name, or ReShade would register it twice.
FEEDER_NR_ADDON = "renodx-dlss5.addon64"
FEEDER_ARTIFACTS = {
    FEEDER_ADDON: "19adbbec1669c1f2500e8ba9241493e1f16d718c43b30ba02f2f52a4e55e8c94",
    FEEDER_SHADER: "8c427a7d08864151be40fa7426f02543b38cd086cff5efa79e911afc0d37ad09",
}

# ReShade's stock headers. Practically every effect starts with
# `#include "ReShade.fxh"`, and the Windows installer fetches the shader
# collection as a separate step -- we only extract ReShade64.dll, so without
# these the feeder's effect and every motion-vector provider fail to compile
# with "could not open included file".
RESHADE_FXH_BASE = "https://raw.githubusercontent.com/crosire/reshade-shaders/slim/Shaders"
RESHADE_FXH = {
    "ReShade.fxh": "6dabfbbaf968c3871905d2ea17f96572ff7b1cec01310b5d0e5252b66b30174f",
    "ReShadeUI.fxh": "78adf672df47460297eb9fe6dd238d2aafa24510b52b84feb1a745dff70eb901",
}

# In feeder mode the game supplies no DLSS of its own, so the Super Resolution
# runtime has to be installed too -- the feeder drives it directly.
FEEDER_DLSS_FILES = [ADDON_NAME, "nvngx_dlssnr.dll", "nvngx_dlss.dll"]

# Upstream defaults, written out so the file exists before the first run and
# can be diffed later. The add-on rewrites it live from its overlay page.
FEEDER_CFG = """\
enabled=1
mode=2
hdr=-1
depth_inverted=-1
flags=-1
reset_every=0
warmup_rebuild=180
rebuild=0
log_frames=3
create_delay=60
preset=0
mv_scale_x=1.0
mv_scale_y=1.0
"""

# Any effect writing this shared texture works as the motion-vector source.
MV_TEXTURE = "texMotionVectors"

# Config files the runtime owns and rewrites on exit. Comparing their hash
# against install time always reports a spurious change.
RUNTIME_OWNED = {"ReShade.ini", "ReShadePreset.ini", "dlss5-feed.cfg",
                 "OptiScaler.ini"}

# Proxy DLL names ReShade can be loaded under. This list is not arbitrary: a
# proxy DLL has to export the functions the game imports from it, and
# ReShade64.dll only implements these API surfaces. winmm/version are NOT
# valid -- ReShade exports none of their functions, so the game would fail to
# start with an unresolved import.
LOADER_NAMES = ["dxgi.dll", "d3d11.dll", "d3d12.dll", "dinput8.dll",
                "d3d9.dll", "ddraw.dll", "opengl32.dll"]

# Files that belong to the DLSS runtime rather than to ReShade. Only these obey
# --dlss-dest; the add-on and loader always go beside the game executable.
DLSS_RUNTIME_PREFIXES = ("nvngx_", "sl.")

# Windows driver version the supplied runtime declares as its minimum. The
# NVIDIA Linux driver uses its own numbering, so this is a warning, not a gate.
WINDOWS_MIN_DRIVER = "615.00"

RESHADE_INI = """\
[GENERAL]
EffectSearchPaths=.\\reshade-shaders\\Shaders\\**
TextureSearchPaths=.\\reshade-shaders\\Textures\\**
PresetPath=.\\ReShadePreset.ini
PerformanceMode=0

[INPUT]
KeyOverlay=36,0,0,0

[OVERLAY]
TutorialProgress=4

[ADDON]
AddonPath=.
DisabledAddons=
"""

SKIP_DIRS = {
    "Content", "Paks", "Archive", "archives", "Mods", "Movies", "Localization",
    "Engine", "Tools", "EasyAntiCheat", "BattlEye", "Redist",
    "Redistributables", "Support", "_CommonRedist", "DirectX",
    "reshade-shaders", BACKUP_DIR_NAME,
}

SKIP_EXE_RE = re.compile(
    r"^(launcher|updater|update|unins|uninstall|setup|install|crashreport|crash|"
    r"crashsender|unrealcefsubprocess|unitycrashhandler|easyanticheat|anticheat|"
    r"beservice|battleye|epicwebhelper|steamwebhelper|dxsetup|vc_redist)",
    re.IGNORECASE,
)

# Extra roots to search for Proton prefixes beyond each library's compatdata.
EXTRA_PREFIX_ROOTS = [
    Path.home() / "proton-prefixes",
    Path.home() / "Games" / "umu",
]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_LEVEL_COLORS = {"INFO": "\033[0m", "OK": "\033[32m", "WARN": "\033[33m",
                 "ERROR": "\033[31m", "STEP": "\033[36m"}


def log(message: str, level: str = "INFO") -> None:
    if _COLOR:
        color = _LEVEL_COLORS.get(level, "\033[0m")
        print(f"{color}[{level:<5}]\033[0m {message}")
    else:
        print(f"[{level:<5}] {message}")


class Fail(Exception):
    """A user-facing error that should abort without a traceback."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_x64_pe(path: Path) -> bool:
    """True when path is a 64-bit Windows PE (EXE or DLL)."""
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return False
            handle.seek(0x3C)
            offset = struct.unpack("<i", handle.read(4))[0]
            if offset <= 0:
                return False
            handle.seek(offset)
            if handle.read(4) != b"PE\0\0":
                return False
            return struct.unpack("<H", handle.read(2))[0] == 0x8664
    except (OSError, struct.error):
        return False


def binary_contains(path: Path, needle: str) -> bool:
    """Stream-search a binary for an ASCII needle, spanning chunk boundaries."""
    probe = needle.lower().encode("ascii")
    tail = b""
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                window = tail + chunk.lower()
                if probe in window:
                    return True
                tail = window[-len(probe):]
    except OSError:
        return False
    return False


def pe_imported_dlls(path: Path) -> set[str]:
    """Lowercased DLL names in a PE's import and delay-import tables.

    A proxy DLL is only ever loaded from the game folder if the executable
    actually imports that name. Anything pulled in later by LoadLibrary from a
    system path (RE Engine gets d3d12.dll that way, via nvapi64) can never be
    proxied this way, which is worth catching before a launch rather than after.
    """
    try:
        data = path.read_bytes()
        pe = struct.unpack_from("<i", data, 0x3C)[0]
        magic = struct.unpack_from("<H", data, pe + 24)[0]
        optional = pe + 24
        directories = optional + (112 if magic == 0x20B else 96)
        section_count = struct.unpack_from("<H", data, pe + 6)[0]
        sections_at = optional + struct.unpack_from("<H", data, pe + 20)[0]
        sections = [struct.unpack_from("<III", data, sections_at + i * 40 + 12)
                    for i in range(section_count)]

        def to_offset(rva: int) -> int | None:
            for virtual, size, raw in sections:
                if virtual <= rva < virtual + size:
                    return raw + (rva - virtual)
            return None

        found: set[str] = set()
        # (directory index, offset of the Name field within each descriptor)
        for index, name_field, stride in ((1, 12, 20), (13, 4, 32)):
            entry = struct.unpack_from("<I", data, directories + index * 8)[0]
            offset = to_offset(entry) if entry else None
            while offset is not None:
                record = data[offset:offset + stride]
                if len(record) < stride or not any(record):
                    break
                name_rva = struct.unpack_from("<I", data, offset + name_field)[0]
                if name_rva:
                    at = to_offset(name_rva)
                    if at is not None:
                        end = data.index(b"\0", at)
                        found.add(data[at:end].decode("ascii", "replace").lower())
                offset += stride
        return found
    except (OSError, ValueError, struct.error, IndexError):
        return set()


def shaders_dir(target: "Target") -> Path:
    return target.folder / "reshade-shaders" / "Shaders"


def find_mv_providers(target: "Target") -> list[Path]:
    """Effects in the game's shader folder that write texMotionVectors.

    The feeder consumes exactly one interface -- the shared texMotionVectors
    texture -- so any provider (ReshadeMotionEstimation, qUINT, Launchpad)
    satisfies it. Detecting one avoids the most common "it does nothing"
    failure, where the feed has no motion to work from.
    """
    found: list[Path] = []
    root = shaders_dir(target)
    if not root.is_dir():
        return found
    # Providers often declare the shared texture in a header (.fxh), not the
    # .fx itself -- ReshadeMotionEstimation puts it in MotionVectors.fxh.
    candidates = sorted(list(root.rglob("*.fx")) + list(root.rglob("*.fxh")))
    for effect in candidates:
        if effect.name == FEEDER_SHADER:
            continue
        try:
            text = effect.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # A provider *declares* the shared texture; consumers only sample it.
        if MV_TEXTURE in text and re.search(
                r"texture2?D?\s+" + MV_TEXTURE + r"\b", text):
            found.append(effect)
    return found


D3D11_NEEDLES = ("d3d11.dll", "d3d11createdevice", "d3d11createdeviceandswapchain")


def looks_like_d3d11(target: "Target") -> str | None:
    """Evidence the game renders with D3D11 (what most Unity titles use)."""
    if "d3d11.dll" in pe_imported_dlls(target.exe):
        return "d3d11.dll in the import table"
    for needle in D3D11_NEEDLES:
        if binary_contains(target.exe, needle):
            return f"{needle!r} in the executable"
    if (target.folder / "UnityPlayer.dll").is_file():
        return "UnityPlayer.dll beside the executable"
    return None


D3D12_NEEDLES = ("d3d12.dll", "d3d12core.dll", "d3d12createdevice",
                 "d3d12sdkversion", "agilitysdk")


def looks_like_d3d12(target: "Target") -> str | None:
    """Return the evidence that this game renders with D3D12, or None.

    Engines that load D3D12 dynamically (RE Engine among them) never store the
    literal "d3d12.dll", so the import-name check alone produces false
    negatives. Shipped Agility SDK files are the stronger signal.
    """
    for name in ("D3D12Core.dll", "d3d12core.dll"):
        if (target.folder / name).is_file():
            return f"{name} beside the executable"
    for directory in ("D3D12", "d3d12", "dx12agility"):
        if (target.folder / directory).is_dir():
            return f"Agility SDK folder {directory}/"
    for needle in D3D12_NEEDLES:
        if binary_contains(target.exe, needle):
            return f"{needle!r} in the executable"
    return None


def is_optiscaler_dll(path: Path) -> bool:
    """True for an OptiScaler proxy DLL.

    Must be tested before is_reshade_dll: OptiScaler's binary also contains the
    string "reshade", so the naive check misidentifies it.
    """
    if not path.is_file() or not is_x64_pe(path):
        return False
    return binary_contains(path, "DlssNr_Dx12") or binary_contains(path, "OptiScaler.ini")


def leftover_reshade_artifacts(target: "Target") -> list[Path]:
    """ReShade files sitting in the game folder that this tool did not install."""
    found = []
    for name in ("ReShade.ini", "ReShadePreset.ini", "ReShade.log",
                 "dlss5-feed.cfg", "dlss5-feed.log"):
        candidate = target.folder / name
        if candidate.is_file():
            found.append(candidate)
    for addon in sorted(target.folder.glob("*.addon64")):
        found.append(addon)
    shaders = target.folder / "reshade-shaders"
    if shaders.is_dir():
        found.append(shaders)
    return found


def is_reshade_dll(path: Path) -> bool:
    """Cheap identity check for a ReShade loader DLL."""
    if not path.is_file() or not is_x64_pe(path):
        return False
    return binary_contains(path, "reshade")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _ancestor_pids() -> set[int]:
    """This process and everything that spawned it."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            break
        match = re.search(r"^PPid:\s*(\d+)", status, re.MULTILINE)
        if not match:
            break
        pid = int(match.group(1))
    return pids


def process_running(exe_name: str) -> bool:
    """True when a process is actually running the given executable.

    A plain `pgrep -f` would match this tool's own command line -- the game
    path is one of our arguments -- so ancestors are filtered out and each
    remaining match is confirmed against its argv[0].
    """
    result = run(["pgrep", "-f", re.escape(exe_name)])
    if result.returncode != 0:
        return False
    skip = _ancestor_pids()
    for line in result.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid in skip:
            continue
        try:
            argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]
        except OSError:
            continue
        if argv0.decode("utf-8", "replace").lower().endswith(exe_name.lower()):
            return True
    return False


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------------------
# Steam library / VDF
# ---------------------------------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Parse Valve's KeyValues text format into nested dicts."""
    tokens = []
    for match in _VDF_TOKEN.finditer(text):
        string, brace = match.groups()
        tokens.append(("s", string.replace(r"\"", '"')) if string is not None
                      else ("b", brace))

    def build(index: int) -> tuple[dict, int]:
        result: dict = {}
        while index < len(tokens):
            kind, value = tokens[index]
            if kind == "b" and value == "}":
                return result, index + 1
            if kind != "s":
                index += 1
                continue
            key = value
            index += 1
            if index >= len(tokens):
                break
            next_kind, next_value = tokens[index]
            if next_kind == "b" and next_value == "{":
                child, index = build(index + 1)
                result[key] = child
            else:
                result[key] = next_value
                index += 1
        return result, index

    return build(0)[0]


@dataclass
class Game:
    appid: str
    name: str
    install_dir: Path
    library: Path
    prefix: Path | None = None


def steam_roots() -> list[Path]:
    candidates = [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".steam/root",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
    ]
    seen, roots = set(), []
    for candidate in candidates:
        if not (candidate / "steamapps").is_dir():
            continue
        real = candidate.resolve()
        if real not in seen:
            seen.add(real)
            roots.append(real)
    return roots


def steam_libraries() -> list[Path]:
    """Every library folder that currently exists on disk."""
    libraries: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        steamapps = path / "steamapps"
        if not steamapps.is_dir():
            return
        real = steamapps.resolve()
        if real not in seen:
            seen.add(real)
            libraries.append(real)

    for root in steam_roots():
        add(root)
        vdf_path = root / "steamapps" / "libraryfolders.vdf"
        if not vdf_path.is_file():
            continue
        try:
            data = parse_vdf(vdf_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        folders = data.get("libraryfolders", data.get("LibraryFolders", {}))
        for value in folders.values():
            path = value.get("path") if isinstance(value, dict) else value
            if isinstance(path, str) and path:
                add(Path(path))
    return libraries


def installed_games() -> dict[str, Game]:
    """appid -> Game for everything installed across all libraries."""
    games: dict[str, Game] = {}
    for steamapps in steam_libraries():
        for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
            try:
                data = parse_vdf(manifest.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            state = data.get("AppState", {})
            appid = state.get("appid")
            install_dir = state.get("installdir")
            if not appid or not install_dir:
                continue
            path = steamapps / "common" / install_dir
            if not path.is_dir():
                continue
            games[appid] = Game(
                appid=appid,
                name=state.get("name") or install_dir,
                install_dir=path,
                library=steamapps,
            )
    return games


def prefix_from_path(path: Path) -> Path | None:
    """Derive the Wine prefix from a game path that lives inside one.

    Non-Steam games are usually installed *into* a prefix, so the prefix is an
    ancestor: <prefix>/drive_c/Program Files/Game/game.exe. Walking up beats
    making the user pass --prefix for the common case.
    """
    for parent in path.resolve().parents:
        if parent.name == "drive_c" and parent.parent != parent:
            return parent.parent
    # Or the game sits beside a prefix directory, e.g. ~/Games/Foo/{pfx,game}
    for parent in [path.resolve(), *path.resolve().parents][:4]:
        for name in ("pfx", "prefix", "wineprefix"):
            candidate = parent / name
            if (candidate / "drive_c/windows/system32").is_dir():
                return candidate
    return None


def find_native_d3dcompiler() -> Path | None:
    """A real Microsoft d3dcompiler_47.dll already installed in some prefix.

    Wine's built-in stub is ~390 KB; the redistributable is ~4 MB. Reusing one
    the user already has avoids needing protontricks (and a wine binary on
    PATH) for prefixes it cannot address, such as non-Steam games.
    """
    roots = [steamapps / "compatdata" for steamapps in steam_libraries()]
    roots.extend(EXTRA_PREFIX_ROOTS)
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            candidate = entry / "pfx/drive_c/windows/system32/d3dcompiler_47.dll"
            try:
                if candidate.is_file() and candidate.stat().st_size > 2_000_000:
                    return candidate
            except OSError:
                continue
    return None


def find_prefix(appid: str) -> Path | None:
    """Locate the Wine prefix for an appid across compatdata and custom roots.

    Steam creates a stub compatdata directory for games it has never actually
    run, so a populated prefix elsewhere always wins over the first match.
    """
    roots = [steamapps / "compatdata" for steamapps in steam_libraries()]
    roots.extend(EXTRA_PREFIX_ROOTS)
    fallback: Path | None = None
    for root in roots:
        candidate = root / appid / "pfx"
        if not candidate.is_dir():
            continue
        if (candidate / "drive_c/windows/system32").is_dir():
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback


def launch_options_for(appid: str) -> str | None:
    """Read the current Steam launch options for an appid, if any."""
    for root in steam_roots():
        for config in (root / "userdata").glob("*/config/localconfig.vdf"):
            try:
                text = config.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            stack: list[str] = []
            for raw in text.splitlines():
                line = raw.strip()
                key_only = re.fullmatch(r'"([^"]+)"', line)
                if key_only:
                    stack.append(key_only.group(1))
                elif line == "}" and stack:
                    stack.pop()
                elif line.startswith('"LaunchOptions"') and stack and stack[-1] == appid:
                    value = re.fullmatch(r'"LaunchOptions"\s+"(.*)"', line)
                    if value:
                        # localconfig.vdf stores the string escaped; return it in
                        # the form the Steam properties dialog shows and accepts.
                        return value.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return None


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


@dataclass
class Target:
    root: Path
    exe: Path
    folder: Path
    detection: str
    appid: str | None = None
    name: str | None = None
    prefix: Path | None = None
    system32: Path | None = None
    dlss_path: Path | None = None
    dlss_where: str = ""
    dlss_dirs: list[Path] = field(default_factory=list)

    def describe(self) -> str:
        label = self.name or self.root.name
        return f"{label} ({self.appid})" if self.appid else label


def candidate_exes(root: Path, max_depth: int = 6) -> list[Path]:
    found: list[Path] = []
    queue = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix.lower() == ".exe":
                if SKIP_EXE_RE.match(entry.stem):
                    continue
                if is_x64_pe(entry):
                    found.append(entry)
            elif entry.is_dir() and depth < max_depth and entry.name not in SKIP_DIRS:
                queue.append((entry, depth + 1))
    return found


def normalized(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name).lower()


def pick_exe(root: Path, system32: Path | None) -> tuple[Path, str]:
    """Score candidate executables and return the most likely renderer."""
    candidates = candidate_exes(root)
    if not candidates:
        raise Fail(f"No 64-bit Windows executable found under {root}")

    root_name = normalized(root.name)
    # A DLSS runtime that lives in the prefix (Proton's PROTON_DLSS_UPGRADE
    # puts it there) is not a per-directory signal, so only game-local copies
    # get to break ties between executables.
    ranked = []
    for candidate in candidates:
        relative = candidate.relative_to(root)
        parts = [part.lower() for part in relative.parts[:-1]]
        stem = normalized(candidate.stem)
        score = 0
        if candidate.parent == root:
            score += 40
        if len(parts) >= 2 and parts[-2:] == ["binaries", "win64"]:
            score += 65
        elif parts and parts[-1] in ("win64", "x64"):
            score += 45
        if re.search(r"(win64-shipping|shipping|game)$", candidate.stem, re.IGNORECASE):
            score += 70
        if stem == root_name:
            score += 110
        elif len(root_name) >= 5 and root_name in stem:
            score += 70
        if (candidate.parent / "nvngx_dlss.dll").is_file():
            score += 220
        if (candidate.parent / "sl.interposer.dll").is_file():
            score += 60
        ranked.append((score, str(relative), candidate))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    score, relative, best = ranked[0]
    if score < 40:
        preview = "; ".join(item[1] for item in ranked[:5])
        raise Fail(f"Could not identify the rendering executable. Candidates: {preview}")
    return best, f"auto-selected {relative} (score {score})"


def resolve_target(spec: str, prefix_override: Path | None) -> Target:
    """Resolve an appid, a game-name substring, or a filesystem path."""
    games = installed_games()
    game: Game | None = None

    if spec.isdigit() and spec in games:
        game = games[spec]
    elif not Path(spec).exists():
        needle = spec.lower()
        matches = [g for g in games.values() if needle in g.name.lower()]
        if not matches:
            matches = [g for g in games.values() if needle in g.install_dir.name.lower()]
        if not matches:
            raise Fail(f"No installed Steam game matches {spec!r}. Try: dlss5-proton scan")
        if len(matches) > 1:
            exact = [g for g in matches if g.name.lower() == needle]
            if len(exact) == 1:
                matches = exact
            else:
                listing = "\n  ".join(f"{g.appid}  {g.name}" for g in sorted(
                    matches, key=lambda g: g.name))
                raise Fail(f"{spec!r} matches several games:\n  {listing}")
        game = matches[0]

    if game is not None:
        root, appid, name = game.install_dir, game.appid, game.name
    else:
        path = Path(spec).expanduser().resolve()
        if path.is_file():
            if path.suffix.lower() != ".exe":
                raise Fail("Pass a game folder, an appid, a game name, or a .exe")
            root, appid, name = path.parent, None, path.parent.name
        elif path.is_dir():
            root, appid, name = path, None, path.name
        else:
            raise Fail(f"{spec} does not exist")
        # Recover the appid when the path sits inside a known library.
        for candidate in installed_games().values():
            if candidate.install_dir == root or candidate.install_dir in root.parents:
                appid, name = candidate.appid, candidate.name
                root = candidate.install_dir
                break

    prefix = prefix_override or (find_prefix(appid) if appid else None)
    if prefix is None:
        # Non-Steam games usually live inside their own prefix.
        prefix = prefix_from_path(root)
    system32 = None
    if prefix:
        candidate = prefix / "drive_c/windows/system32"
        system32 = candidate if candidate.is_dir() else None

    if game is None and Path(spec).is_file():
        exe, detection = Path(spec).expanduser().resolve(), "executable supplied directly"
    else:
        exe, detection = pick_exe(root, system32)

    target = Target(
        root=root, exe=exe, folder=exe.parent, detection=detection,
        appid=appid, name=name, prefix=prefix, system32=system32,
    )
    target.dlss_path, target.dlss_where = find_existing_dlss(target)
    # A game can ship DLSS in more than one plugin folder (FF7 Rebirth has both
    # DLSSSubset and StreamlineSubset, and loads from the latter). Collect them
    # all rather than betting on which one the engine actually uses.
    seen: list[Path] = []
    for match in sorted(root.rglob("nvngx_dlss.dll")):
        if match.parent not in seen:
            seen.append(match.parent)
    target.dlss_dirs = seen
    return target


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

MAX_ENTRIES = 5000
MAX_ENTRY_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def safe_extract(zip_path: Path, destination: Path) -> None:
    """Extract a ZIP (or 7z), rejecting traversal, absolute paths, and zip bombs.

    The y4my4m fork ships .7z. Those go through the system 7z binary, which
    refuses absolute paths and strips ".." on its own; the ZIP path keeps the
    explicit checks below.
    """
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if zip_path.suffix.lower() == ".7z":
        tool = next((t for t in ("7z", "7za", "7zr") if shutil.which(t)), None)
        if tool is None:
            raise Fail(f"{zip_path.name} is a .7z archive and no 7z/7za/7zr binary is on PATH "
                       "(pacman -S 7zip), or convert it to .zip and pass that")
        result = run([tool, "x", "-y", f"-o{root}", str(zip_path)])
        if result.returncode != 0:
            raise Fail(f"7z extraction failed for {zip_path.name}: {result.stderr.strip()[:300]}")
        return
    with zipfile.ZipFile(zip_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise Fail("ZIP contains too many entries")
        total = 0
        seen: set[str] = set()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise Fail(f"ZIP contains an absolute path: {entry.filename}")
            if any(part == ".." for part in name.split("/")):
                raise Fail(f"ZIP contains a traversal path: {entry.filename}")
            key = name.lower()
            if key in seen:
                raise Fail(f"ZIP contains a duplicate path: {entry.filename}")
            seen.add(key)
            if entry.file_size > MAX_ENTRY_BYTES:
                raise Fail(f"ZIP entry exceeds 512 MiB: {entry.filename}")
            total += entry.file_size
            if total > MAX_TOTAL_BYTES:
                raise Fail("ZIP expands beyond the 2 GiB safety limit")

        for entry in entries:
            name = entry.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            out = (root / name).resolve()
            if root not in out.parents:
                raise Fail(f"ZIP resolves outside the extraction folder: {entry.filename}")
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, out.open("wb") as sink:
                shutil.copyfileobj(source, sink)


def default_addon() -> Path | None:
    """Locate the neural-rendering add-on, newest build first.

    Prefers the bundled binary-patched "++" v3 -- the "classic" engine -- because
    the newer v4.x build (0.2026.828.517) is NOT viable under Proton: it hangs or
    crashes in DLSSNR CreateFeature in every game tested, including one that works
    on classic. Pass --addon explicitly to override this.
    """
    here = Path(__file__).resolve().parent
    for candidate in [
        here / ADDON_NAME,
        here / "payload" / ADDON_NAME,
        here.parent / "DLSS5-Easy-Installer-v1.0.1-public-safe" / "payload" / ADDON_NAME,
        here / FEEDER_NR_ADDON,
        here.parent / FEEDER_NR_ADDON,
    ]:
        if candidate.is_file():
            return candidate
    return None


def default_mv_provider() -> Path | None:
    """A motion-vector provider kept alongside the tool, if one is present.

    Feeder mode without a texMotionVectors provider still runs, but the feeder
    logs "motion vectors will be zero (still images only)" and the image smears
    whenever the camera moves -- an easy footgun to walk into, since the install
    otherwise succeeds. If the user dropped a provider in mv-providers/, use it.
    """
    here = Path(__file__).resolve().parent
    for root in (here / "mv-providers", here.parent / "mv-providers"):
        if not root.is_dir():
            continue
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir():
                continue
            for shader in candidate.rglob("*.fx*"):
                try:
                    text = shader.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if re.search(r"texture2?D?\s+" + MV_TEXTURE + r"\b", text):
                    return candidate
    return None


def default_zip() -> Path | None:
    """Pick the best DLSS payload ZIP near the project.

    Ranked by how much of the DLSS/Streamline set each archive carries, not
    just by date: a research or tooling archive can contain nvngx_dlssnr.dll
    while having none of the rest, and picking it silently breaks --full.
    """
    here = Path(__file__).resolve().parent
    wanted = {name.lower() for name in FULL_FILES if name != ADDON_NAME}
    best: tuple[int, float, Path] | None = None
    for candidate in {p for directory in (here, here.parent)
                      for p in directory.glob("*.zip")}:
        try:
            with zipfile.ZipFile(candidate) as archive:
                names = {n.replace("\\", "/").rsplit("/", 1)[-1].lower()
                         for n in archive.namelist()}
        except (zipfile.BadZipFile, OSError):
            continue
        if "nvngx_dlssnr.dll" not in names:
            continue
        score = (len(wanted & names), candidate.stat().st_mtime, candidate)
        if best is None or score[:2] > best[:2]:
            best = score
    return best[2] if best else None


@dataclass
class Payload:
    folder: Path
    zip_path: Path
    zip_hash: str


def prepare_payload(zip_path: Path, addon_path: Path | None,
                    explicit: bool = False,
                    nr_runtime: Path | None = None) -> Payload:
    """Extract the user's DLSS ZIP into a cache and drop the add-on beside it."""
    if not zip_path.is_file():
        raise Fail(f"DLSS ZIP not found: {zip_path}")
    if zip_path.suffix.lower() != ".zip":
        raise Fail("The DLSS payload must be a .zip file")
    # OptiScaler mode needs no RenoDX add-on -- it drives the model itself.
    if addon_path is None:
        addon_hash = None
    elif not addon_path.is_file():
        raise Fail(f"Add-on not found: {addon_path}")
    else:
        addon_hash = sha256_file(addon_path)
    if addon_hash is not None and addon_hash != ADDON_SHA256:
        # The pin guards the bundled build. A deliberately supplied --addon is
        # the user's choice -- newer RenoDX builds exist (the "v4.x" engine, vs
        # the bundled "classic" one) and refusing them would be actively
        # unhelpful. Still insist it is a real 64-bit add-on.
        if not explicit and addon_path.name == ADDON_NAME:
            raise Fail(
                f"Add-on failed its pinned SHA-256 check.\n"
                f"  expected {ADDON_SHA256}\n  got      {addon_hash}\n"
                "Pass --addon explicitly if you meant to use a different build."
            )
        if not is_x64_pe(addon_path):
            raise Fail(f"{addon_path} is not a 64-bit PE")
        log(f"Using a non-bundled add-on: {addon_path.name}", "WARN")
        log(f"  sha256 {addon_hash}", "WARN")

    zip_hash = sha256_file(zip_path)
    # Key the cache on the override too. Writing a supplied runtime into the
    # shared payload directory would silently contaminate every later install
    # from the same ZIP.
    suffix = ""
    if nr_runtime is not None and Path(nr_runtime).is_file():
        suffix = "-nr" + sha256_file(Path(nr_runtime).expanduser())[:8]
    cache = CACHE_ROOT / f"payload-{zip_hash[:12]}{suffix}"
    runtime = next(iter(sorted(cache.rglob("nvngx_dlssnr.dll"))), None) if cache.is_dir() else None

    if runtime is None:
        if cache.is_dir():
            shutil.rmtree(cache)
        log(f"Extracting {zip_path.name} into {cache}", "STEP")
        try:
            safe_extract(zip_path, cache)
        except Exception:
            shutil.rmtree(cache, ignore_errors=True)
            raise
        runtime = next(iter(sorted(cache.rglob("nvngx_dlssnr.dll"))), None)

    if runtime is None or not is_x64_pe(runtime):
        shutil.rmtree(cache, ignore_errors=True)
        raise Fail("The ZIP does not contain a valid 64-bit nvngx_dlssnr.dll")

    folder = runtime.parent
    if nr_runtime is not None:
        # Override the ZIP's neural-rendering model with a specific build. The
        # add-on reports whether what it loaded is a "reference match" or a
        # "custom build", and those behave differently.
        nr_runtime = nr_runtime.expanduser()
        if not nr_runtime.is_file():
            raise Fail(f"--nr-runtime not found: {nr_runtime}")
        if not is_x64_pe(nr_runtime):
            raise Fail(f"{nr_runtime} is not a 64-bit PE")
        shutil.copy2(nr_runtime, folder / "nvngx_dlssnr.dll")
        log(f"Using a supplied NR runtime: {nr_runtime.name} "
            f"({nr_runtime.stat().st_size} bytes)", "WARN")
        log(f"  sha256 {sha256_file(nr_runtime)}", "WARN")
    if addon_path is not None:
        shutil.copy2(addon_path, folder / ADDON_NAME)
    return Payload(folder=folder, zip_path=zip_path, zip_hash=zip_hash)


def fetch_reshade() -> Path:
    """Download the official ReShade add-on setup and extract ReShade64.dll."""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    dll = CACHE_ROOT / f"ReShade64-{RESHADE_VERSION}.dll"
    if dll.is_file() and is_x64_pe(dll):
        return dll

    setup = CACHE_ROOT / f"ReShade_Setup_{RESHADE_VERSION}_Addon.exe"
    if not (setup.is_file() and sha256_file(setup) == RESHADE_SHA256):
        setup.unlink(missing_ok=True)
        log(f"Downloading ReShade {RESHADE_VERSION} (add-on build) from reshade.me", "STEP")
        result = run(["curl", "-sSL", "--fail", "--max-time", "300",
                      "-o", str(setup), RESHADE_URL])
        if result.returncode != 0 or not setup.is_file():
            raise Fail(f"ReShade download failed: {result.stderr.strip()}")
        actual = sha256_file(setup)
        if actual != RESHADE_SHA256:
            setup.unlink(missing_ok=True)
            raise Fail(
                f"ReShade download failed its pinned SHA-256 check.\n"
                f"  expected {RESHADE_SHA256}\n  got      {actual}"
            )

    if shutil.which("7z") is None and shutil.which("7za") is None:
        raise Fail("7z is required to unpack the ReShade setup (install p7zip)")
    seven = shutil.which("7z") or shutil.which("7za")

    workdir = CACHE_ROOT / "reshade-extract"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    result = run([seven, "e", "-y", f"-o{workdir}", str(setup), "ReShade64.dll"])
    extracted = workdir / "ReShade64.dll"
    if result.returncode != 0 or not extracted.is_file():
        raise Fail(f"Could not extract ReShade64.dll from the setup: {result.stderr.strip()}")
    if not is_x64_pe(extracted):
        raise Fail("Extracted ReShade64.dll is not a 64-bit PE")

    shutil.move(str(extracted), dll)
    shutil.rmtree(workdir, ignore_errors=True)
    return dll


def fetch_pinned(base_url: str, artifacts: dict[str, str], tag: str) -> dict[str, Path]:
    """Download hash-pinned files into the cache, reusing valid copies."""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, expected in artifacts.items():
        cached = CACHE_ROOT / f"{tag}-{name}"
        if cached.is_file() and sha256_file(cached) == expected:
            out[name] = cached
            continue
        cached.unlink(missing_ok=True)
        log(f"Downloading {name}", "STEP")
        result = run(["curl", "-sSL", "--fail", "--max-time", "300",
                      "-o", str(cached), f"{base_url}/{name}"])
        if result.returncode != 0 or not cached.is_file():
            raise Fail(f"Download failed for {name}: {result.stderr.strip()}")
        actual = sha256_file(cached)
        if actual != expected:
            cached.unlink(missing_ok=True)
            raise Fail(
                f"{name} failed its pinned SHA-256 check.\n"
                f"  expected {expected}\n  got      {actual}"
            )
        out[name] = cached
    return out


def fetch_reshade_headers() -> dict[str, Path]:
    """ReShade.fxh / ReShadeUI.fxh, which nearly every effect includes."""
    return fetch_pinned(RESHADE_FXH_BASE, RESHADE_FXH, "reshade-fxh")


def fetch_optiscaler(override: str | Path | None = None) -> Path:
    """Return a directory holding the extracted, hash-verified OptiScaler build.

    Looks for the archive beside the tool first so it need not be re-downloaded;
    falls back to the pinned GitHub release.

    `override` pins one game to a specific archive. Upstream ships several times
    a day and a build that fixes one title can hang another, so a per-game pin
    beats flipping the global default back and forth. An overridden archive is
    used as given -- its hash is not the pinned one by definition.
    """
    if override is not None:
        archive = Path(override).expanduser()
        if not archive.is_file():
            raise Fail(f"--optiscaler-zip does not exist: {archive}")
        extracted = CACHE_ROOT / f"optiscaler-custom-{sha256_file(archive)[:16]}"
        if not (extracted / OPTISCALER_LOADER_SOURCE).is_file():
            shutil.rmtree(extracted, ignore_errors=True)
            log(f"Extracting {archive.name}", "STEP")
            safe_extract(archive, extracted)
        if not (extracted / OPTISCALER_LOADER_SOURCE).is_file():
            raise Fail(f"{OPTISCALER_LOADER_SOURCE} missing from {archive.name}")
        log(f"Using OptiScaler override: {archive.name}", "WARN")
        return extracted

    here = Path(__file__).resolve().parent
    name = OPTISCALER_ZIP
    archive = None
    for candidate in (here / name, here.parent / name,
                      CACHE_ROOT / name):
        if candidate.is_file() and sha256_file(candidate) == OPTISCALER_SHA256:
            archive = candidate
            break

    if archive is None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        archive = CACHE_ROOT / name
        archive.unlink(missing_ok=True)
        log(f"Downloading OptiScaler {OPTISCALER_VERSION} (~56 MB)", "STEP")
        result = run(["curl", "-sSL", "--fail", "--max-time", "900",
                      "-o", str(archive), OPTISCALER_URL])
        if result.returncode != 0 or not archive.is_file():
            raise Fail(f"OptiScaler download failed: {result.stderr.strip()}")
        actual = sha256_file(archive)
        if actual != OPTISCALER_SHA256:
            archive.unlink(missing_ok=True)
            raise Fail(
                f"OptiScaler archive failed its pinned SHA-256 check.\n"
                f"  expected {OPTISCALER_SHA256}\n  got      {actual}\n"
                f"  The default is a nightly snapshot ({OPTISCALER_BUILD}) and upstream's "
                f"'{OPTISCALER_TAG}' tag rolls daily. Put the verified archive back in "
                f"DLSS5_work/ as {OPTISCALER_ZIP}, or pin this game with --optiscaler-zip."
            )

    extracted = CACHE_ROOT / f"optiscaler-{OPTISCALER_VERSION}"
    if not (extracted / OPTISCALER_LOADER_SOURCE).is_file():
        shutil.rmtree(extracted, ignore_errors=True)
        log(f"Extracting {archive.name}", "STEP")
        # The archive uses backslash separators; safe_extract normalises them.
        safe_extract(archive, extracted)
    if not (extracted / OPTISCALER_LOADER_SOURCE).is_file():
        raise Fail(f"{OPTISCALER_LOADER_SOURCE} missing from the OptiScaler archive")
    return extracted


def fetch_feeder(override: str | Path | None = None) -> dict[str, Path]:
    """Download and unpack the pinned DLSS5-Feeder release.

    0.12.0 ships one archive instead of loose assets, with the shader nested
    under reshade-shaders/Shaders/, so the members are looked up by path.

    `override` pins one game to a specific archive, the same way
    `--optiscaler-zip` does. Upstream is beta-heavy and ships several times a
    day, so testing a new build on one game must not move the default. An
    overridden archive is used as given -- its hash is not the pinned one.
    """
    if override is not None:
        archive = Path(override).expanduser()
        if not archive.is_file():
            raise Fail(f"--feeder-zip does not exist: {archive}")
        extracted = CACHE_ROOT / f"feeder-custom-{sha256_file(archive)[:16]}"
        if not (extracted / FEEDER_ZIP_MEMBERS[FEEDER_ADDON]).is_file():
            shutil.rmtree(extracted, ignore_errors=True)
            log(f"Extracting {archive.name}", "STEP")
            safe_extract(archive, extracted)
        log(f"Using DLSS5-Feeder override: {archive.name}", "WARN")
        return _feeder_members(extracted, archive)

    here = Path(__file__).resolve().parent
    archive = None
    for candidate in (here / FEEDER_ZIP, here.parent / FEEDER_ZIP,
                      CACHE_ROOT / FEEDER_ZIP):
        if candidate.is_file() and sha256_file(candidate) == FEEDER_ZIP_SHA256:
            archive = candidate
            break

    if archive is None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        archive = CACHE_ROOT / FEEDER_ZIP
        archive.unlink(missing_ok=True)
        log(f"Downloading DLSS5-Feeder {FEEDER_VERSION}", "STEP")
        result = run(["curl", "-sSL", "--fail", "--max-time", "300",
                      "-o", str(archive), FEEDER_URL])
        if result.returncode != 0 or not archive.is_file():
            raise Fail(f"DLSS5-Feeder download failed: {result.stderr.strip()}")
        actual = sha256_file(archive)
        if actual != FEEDER_ZIP_SHA256:
            archive.unlink(missing_ok=True)
            raise Fail(
                f"DLSS5-Feeder archive failed its pinned SHA-256 check.\n"
                f"  expected {FEEDER_ZIP_SHA256}\n  got      {actual}"
            )

    extracted = CACHE_ROOT / f"feeder-{FEEDER_VERSION}"
    if not (extracted / FEEDER_ZIP_MEMBERS[FEEDER_ADDON]).is_file():
        shutil.rmtree(extracted, ignore_errors=True)
        safe_extract(archive, extracted)
    return _feeder_members(extracted, archive)


def _feeder_members(extracted: Path, archive: Path) -> dict[str, Path]:
    """Pick the files the installer needs out of an unpacked feeder tree."""
    out: dict[str, Path] = {}
    for name, member in FEEDER_ZIP_MEMBERS.items():
        path = extracted / member
        if not path.is_file():
            raise Fail(f"{member} missing from {archive.name}")
        out[name] = path
    if not is_x64_pe(out[FEEDER_ADDON]):
        raise Fail(f"{FEEDER_ADDON} is not a 64-bit PE")
    return out

# ---------------------------------------------------------------------------
# Compatibility scan
# ---------------------------------------------------------------------------


@dataclass
class Report:
    info: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    target: Target | None = None
    payload: Payload | None = None

    @property
    def ok(self) -> bool:
        return not self.fatal

    def show(self) -> None:
        for line in self.info:
            log(line, "OK")
        for line in self.warnings:
            log(line, "WARN")
        for line in self.fatal:
            log(line, "ERROR")
        if self.ok:
            log("Scan passed. Runtime rendering compatibility is still game-dependent.",
                "OK")
        else:
            log("Scan failed. Nothing was changed.", "ERROR")


def gpu_info() -> tuple[list[str], list[str]]:
    if shutil.which("nvidia-smi") is None:
        return [], []
    result = run(["nvidia-smi", "--query-gpu=name,driver_version",
                  "--format=csv,noheader"])
    if result.returncode != 0:
        return [], []
    names, drivers = [], []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 2:
            names.append(parts[0])
            drivers.append(parts[1])
    return names, drivers


def game_upscalers(target: "Target") -> list[str]:
    """Upscaler runtimes shipped *by the game*, which OptiScaler can intercept.

    A DLSS runtime sitting in the prefix's system32 (Proton's DLSS upgrade puts
    one there) is not evidence the game uses DLSS -- only files inside the game
    tree are.
    """
    patterns = {
        "DLSS": ("nvngx_dlss.dll",),
        "DLSS-RR": ("nvngx_dlssd.dll",),
        "Streamline": ("sl.interposer.dll",),
        "FSR": ("amd_fidelityfx_upscaler_dx12.dll", "ffx_fsr2_api_x64.dll"),
        "XeSS": ("libxess.dll",),
    }
    found = []
    for label, names in patterns.items():
        for name in names:
            hits = [h for h in target.root.rglob(name)
                    if "OptiScaler" not in h.parts]
            if hits:
                found.append(label)
                break
    return found

def find_existing_dlss(target: Target) -> tuple[Path | None, str]:
    """Locate the DLSS runtime already in use, game folder first."""
    local = target.folder / "nvngx_dlss.dll"
    if local.is_file():
        return local, "game folder"
    for match in sorted(target.root.rglob("nvngx_dlss.dll")):
        return match, "game tree"
    if target.system32:
        candidate = target.system32 / "nvngx_dlss.dll"
        if candidate.is_file():
            return candidate, "prefix system32"
    return None, ""


def scan(target: Target, payload: Payload | None, full: bool,
         install_reshade: bool, dlss_dest: str, loader: str = "dxgi.dll",
         feeder: bool = False, mv_provider_supplied: bool = False,
         optiscaler: bool = False) -> Report:
    report = Report(target=target, payload=payload)

    report.info.append(f"Game: {target.describe()}")
    report.info.append(f"Game root: {target.root}")
    report.info.append(f"Rendering EXE: {target.exe}")
    report.info.append(f"Install folder: {target.folder}")
    report.info.append(f"Detection: {target.detection}")

    if not is_x64_pe(target.exe):
        report.fatal.append("The selected executable is not a 64-bit Windows PE")

    # --- payload -----------------------------------------------------------
    if payload:
        report.info.append(f"DLSS ZIP: {payload.zip_path.name}")
        report.info.append(f"DLSS ZIP SHA-256: {payload.zip_hash}")
        if optiscaler:
            required, mode_name = ["nvngx_dlssnr.dll"], "OptiScaler"
        elif feeder:
            required, mode_name = FEEDER_DLSS_FILES, "feeder"
        elif full:
            required, mode_name = FULL_FILES, "advanced"
        else:
            required, mode_name = MINIMAL_FILES, "minimal"
        missing = [name for name in required if not (payload.folder / name).is_file()]
        if missing:
            report.fatal.append("Payload is missing: " + ", ".join(missing))
        else:
            report.info.append(f"Payload complete for {mode_name} mode")

    # --- prefix ------------------------------------------------------------
    if target.prefix:
        report.info.append(f"Proton prefix: {target.prefix}")
        if not target.system32:
            # Only a blocker when we actually need to write into the prefix.
            message = ("Prefix is empty (no drive_c/windows/system32) -- launch "
                       "the game once under Proton first")
            if dlss_dest in ("system32", "both"):
                report.fatal.append(message)
            else:
                report.warnings.append(
                    message + ". Installing into the game folder only; "
                    "protontricks will be skipped"
                )
    elif target.appid:
        report.fatal.append(
            f"No Proton prefix found for appid {target.appid}. Launch the game once "
            "under Proton, or pass --prefix /path/to/pfx"
        )
    else:
        report.warnings.append("No Proton prefix known; pass --prefix to enable prefix checks")

    # --- DX12 / DLSS -------------------------------------------------------
    evidence = looks_like_d3d12(target)
    if feeder:
        # The feeder supports D3D11 and D3D12 (not D3D10), so either is fine.
        d3d11 = looks_like_d3d11(target)
        if evidence:
            report.info.append(f"Direct3D 12 confirmed: {evidence}")
        elif d3d11:
            report.info.append(f"Direct3D 11 detected: {d3d11}")
        else:
            report.warnings.append(
                "Could not confirm D3D11 or D3D12. The feeder supports both, but "
                "not D3D10; Vulkan needs ReShade installed as a layer instead"
            )
    elif evidence:
        report.info.append(f"Direct3D 12 confirmed: {evidence}")
    elif optiscaler and (d3d11 := looks_like_d3d11(target)):
        # OptiScaler runs D3D11 games through its dx11on12 bridge; the model is
        # D3D12-only, so the bridged DLSS upscaler has to carry it
        # (Dx11Upscaler=dlss_12 in the y4my4m line; fsr22_12/xess_12 on older
        # builds). FFXIV is the reference case -- see FINDINGS.md (Final Fantasy XIV).
        report.info.append(
            f"Direct3D 11 detected: {d3d11}. OptiScaler will bridge it to D3D12; "
            "set [Upscalers] Dx11Upscaler=dlss_12 (or fsr22_12 on pre-nightly builds) "
            "so the neural-rendering pass has a D3D12 upscaler to ride on"
        )
    else:
        report.warnings.append(
            "Could not confirm D3D12 from the executable. The add-on does not "
            "support D3D11 or Vulkan (including DXVK-rendered D3D11 titles). "
            "Consider --feeder, which does support D3D11"
        )

    # --- feeder prerequisites ---------------------------------------------
    if feeder:
        providers = find_mv_providers(target)
        if providers:
            report.info.append("Motion-vector provider found: " + ", ".join(
                p.name for p in providers))
        elif mv_provider_supplied:
            report.info.append("Motion-vector provider will be installed from "
                               "--mv-provider")
        else:
            report.warnings.append(
                f"No effect declaring {MV_TEXTURE} found in "
                f"{shaders_dir(target)}. Without a motion-vector provider the "
                "feed has no motion and the image will smear when you move. "
                "Install ReshadeMotionEstimation or qUINT_motionvectors, or "
                "pass --mv-provider"
            )
        report.warnings.append(
            "Feeder is incompatible with NVIDIA Smooth Motion and OptiScaler -- "
            "make sure neither is active. Turn the game's MSAA/SSAA off too"
        )

    existing, where = target.dlss_path, target.dlss_where
    if optiscaler:
        # OptiScaler works by intercepting an upscaler the game already runs.
        # With nothing to intercept there is no hook point at any resolution.
        shipped = game_upscalers(target)
        if shipped:
            report.info.append("Game ships upscaler(s) OptiScaler can hook: "
                               + ", ".join(shipped))
        else:
            report.fatal.append(
                "This game ships no upscaler (no DLSS/FSR/XeSS in its own files), "
                "so OptiScaler has nothing to intercept. Use --mode feeder, which "
                "synthesises the inputs from ReShade's depth buffer instead"
            )
    elif feeder:
        # No game DLSS is required; the feeder supplies its own runtime.
        report.info.append(
            "Feeder mode: nvngx_dlss.dll + nvngx_dlssnr.dll will be installed "
            "beside the executable (the game needs no DLSS of its own)"
        )
    elif existing:
        report.info.append(f"Existing DLSS runtime ({where}): {existing}")
        if where == "prefix system32":
            report.warnings.append(
                "The game ships no DLSS runtime of its own; Proton is providing it "
                "in the prefix. If the add-on reports WAITING FOR GAME DLSS, re-run "
                "with --dlss-dest both"
            )
        try:
            report.info.append("DLSS runtime will be written to: " + ", ".join(
                str(path) for path in dlss_destinations(target, dlss_dest)))
        except Fail as error:
            report.fatal.append(str(error))
    else:
        report.fatal.append(
            "No nvngx_dlss.dll found in the game tree or the prefix. The add-on "
            "only works with D3D12 games that already use NVIDIA NGX DLSS"
        )

    # --- ReShade loader ----------------------------------------------------
    for name in (OPTISCALER_PROXY_NAMES if optiscaler else LOADER_NAMES):
        candidate = target.folder / name
        if not candidate.is_file():
            continue
        if is_optiscaler_dll(candidate):
            if name == loader:
                report.info.append(
                    f"An existing OptiScaler {name} will be replaced with "
                    f"{OPTISCALER_VERSION}")
                install_reshade = False
            else:
                report.fatal.append(
                    f"OptiScaler is already installed as {name}. Remove it first "
                    f"(run remove_optiscaler.sh, or restore), or use --loader {name}")
        elif is_reshade_dll(candidate):
            if name == loader and optiscaler:
                report.warnings.append(
                    f"A ReShade {name} is here and will be REPLACED by OptiScaler. "
                    "ReShade's own config and shaders are not removed -- see below")
                install_reshade = False
            elif name == loader:
                report.info.append(f"Existing ReShade loader will be reused: {name}")
                install_reshade = False
            elif optiscaler:
                # Verified conflict: with ReShade resident on another proxy,
                # OptiScaler's first NR dispatch faults inside VKD3D's
                # d3d12core (Horizon Forbidden West, 2026-09-01). Both detour
                # DXGI/D3D12 over the same command lists. Renaming ReShade's
                # DLL away fixed it outright.
                report.warnings.append(
                    f"ReShade is on {name} and would stay loaded alongside "
                    f"OptiScaler on {loader}. This combination is known to crash "
                    "neural rendering (two DXGI/D3D12 hook chains over the same "
                    f"command lists). If NR faults, rename {name} aside and retry")
            else:
                report.fatal.append(
                    f"ReShade is already installed as {name}. Installing {loader} "
                    f"as well would load two copies -- re-run with --loader {name}"
                )
        elif name == loader:
            report.fatal.append(
                f"A non-ReShade {name} already exists in the game folder; it will "
                "not be overwritten"
            )
        else:
            report.warnings.append(f"The game ships its own {name}; leaving it alone")

    # A proxy the executable does not import will simply never be loaded --
    # the game starts fine and ReShade is silently absent.
    imported = pe_imported_dlls(target.exe)
    if imported:
        proxy_set = OPTISCALER_PROXY_NAMES if optiscaler else LOADER_NAMES
        usable = [name for name in proxy_set if name in imported]
        if loader in imported:
            report.info.append(
                f"{target.exe.name} statically imports {loader} -- the proxy is "
                "guaranteed to load")
        else:
            # Not a hard blocker: a plain LoadLibrary("d3d11.dll") searches the
            # application directory first, which is how ReShade works in Unity
            # titles that import nothing graphics-related. It only fails when the
            # caller restricts the search to system32 -- RE Requiem's nvapi64
            # does exactly that for d3d12.dll, which is why that combination
            # silently never loaded.
            report.warnings.append(
                f"{target.exe.name} does not statically import {loader}. It can "
                "still load if the game resolves it by name from its own folder, "
                "but not if the loader restricts the search to system32. "
                + (f"Guaranteed alternatives: {', '.join(usable)}"
                   if usable else "No ReShade proxy name is statically imported")
                + ". If ReShade.log is never written, this is why."
            )

    if optiscaler:
        reshade_stays = any(
            is_reshade_dll(target.folder / n) and n != loader
            for n in LOADER_NAMES)
        stale = [] if reshade_stays else leftover_reshade_artifacts(target)
        if reshade_stays:
            report.info.append(
                "ReShade's own config, add-ons and shaders are left in place")
        if stale:
            report.warnings.append(
                "ReShade/feeder leftovers in the game folder that OptiScaler does "
                "not need and this tool will not delete: "
                + ", ".join(p.name for p in stale)
                + ". Harmless (nothing loads them once ReShade's proxy is gone), "
                "but remove them by hand if you want the folder clean")
        report.info.append(
            f"OptiScaler {OPTISCALER_VERSION} will be installed as {loader} "
            "(no ReShade). It hooks the game's own DLSS pass, so leave the "
            "game's DLSS ON")
    elif install_reshade:
        report.info.append(f"ReShade {RESHADE_VERSION} (add-on build) will be installed as {loader}")

    # --- prior install -----------------------------------------------------
    if (target.folder / STATE_NAME).is_file():
        report.fatal.append(
            "This game already has installer state. Run 'restore' first"
        )

    # --- running game ------------------------------------------------------
    if process_running(target.exe.name):
        report.fatal.append(f"{target.exe.name} appears to be running. Close it first")

    # --- Steam launch options ---------------------------------------------
    if target.appid:
        options = launch_options_for(target.appid) or ""
        if "WINEDLLOVERRIDES" in options and "dxgi" in options:
            report.info.append("Launch options already override dxgi")
        else:
            report.warnings.append(
                f"Steam launch options do not override {Path(loader).stem} yet, and "
                "Proton does not set it automatically. The installer will print the "
                "exact string to paste"
            )
        if "PROTON_DLSS_UPGRADE=1" in options and dlss_dest in ("system32", "both"):
            report.warnings.append(
                "PROTON_DLSS_UPGRADE=1 is set and you are writing to the prefix "
                "system32. Proton overwrites nvngx_dlss*.dll there on every launch; "
                "remove that variable or your DLSS 5 files will be replaced"
            )

    # --- hardware ----------------------------------------------------------
    names, drivers = gpu_info()
    if not names:
        report.warnings.append("nvidia-smi not available; GPU could not be verified")
    elif not any(re.search(r"RTX\s*50\d{2}", name, re.IGNORECASE) for name in names):
        report.fatal.append("No RTX 50-series GPU detected: " + ", ".join(names))
    else:
        report.info.append("RTX 50-series GPU detected: " + ", ".join(names))

    if drivers:
        report.info.append("NVIDIA Linux driver: " + ", ".join(drivers))
        report.warnings.append(
            f"The runtime declares Windows driver {WINDOWS_MIN_DRIVER} as its "
            "minimum. Linux driver numbering differs, so this cannot be checked "
            "automatically -- if the add-on loads but never initialises, an older "
            "driver branch is the first thing to suspect"
        )

    return report


# ---------------------------------------------------------------------------
# Install / restore
# ---------------------------------------------------------------------------


def dlss_destinations(target: Target, dlss_dest: str) -> list[Path]:
    """Directories that should receive the DLSS runtime DLLs.

    'auto' also covers the directory the game's own DLSS runtime lives in --
    Unreal keeps it under Plugins/.../ThirdParty/Win64 rather than beside the
    shipping executable, and NGX looks there rather than in the exe folder.
    """
    places: list[Path] = []

    def add(path: Path | None) -> None:
        if path and path.is_dir() and path not in places:
            places.append(path)

    if dlss_dest in ("auto", "game", "both"):
        add(target.folder)
    if dlss_dest in ("auto", "both"):
        for directory in target.dlss_dirs:
            add(directory)
    if dlss_dest in ("system32", "both"):
        if not target.system32:
            raise Fail("--dlss-dest needs a Proton prefix; pass --prefix, or use "
                       "--dlss-dest game")
        add(target.system32)
    if not places:
        raise Fail(f"--dlss-dest {dlss_dest} resolved to no usable directory")
    return places


def resolve_mode(args) -> str:
    """Which engine/mode to install. --full/--feeder remain as aliases."""
    if getattr(args, "feeder", False):
        return "feeder"
    if getattr(args, "full", False):
        return "full"
    return getattr(args, "mode", "optiscaler")


def install_optiscaler(target: Target, loader: str, backup: Path,
                       records: list[dict],
                       override: str | Path | None = None) -> None:
    """Place the OptiScaler tree, renaming its loader to the chosen proxy.

    Mirrors setup_linux.sh minus the interactive parts: on an NVIDIA GPU that
    script only renames OptiScaler.dll and writes an uninstaller, leaving
    OptiScaler.ini untouched. Doing it here keeps every file in the state file
    so `restore` can undo it.
    """
    source = fetch_optiscaler(override)
    install_file(source / OPTISCALER_LOADER_SOURCE, target.folder / loader,
                 backup, records, "optiscaler")
    for relative in OPTISCALER_FILES:
        origin = source / relative
        if not origin.is_file():
            raise Fail(f"OptiScaler archive is missing {relative}")
        destination = target.folder / relative
        if relative in RUNTIME_OWNED and destination.is_file():
            # OptiScaler.ini is the user's, not ours: it holds whatever they
            # tuned in the overlay, plus LogToFile/LogLevel. Overwriting it on
            # every upgrade silently turns diagnostics back off, which is how a
            # rollback ended up producing no log at all.
            log(f"keeping existing {relative}", "INFO")
            continue
        install_file(origin, destination, backup, records, "optiscaler")


def destinations_for(name: str, target: Target, dlss_dest: str) -> list[Path]:
    """Where a given payload file should land."""
    if not name.startswith(DLSS_RUNTIME_PREFIXES):
        # The add-on and the ReShade loader must sit beside the executable.
        return [target.folder / name]
    return [directory / name for directory in dlss_destinations(target, dlss_dest)]


def install_file(source: Path, destination: Path, backup: Path,
                 records: list[dict], category: str) -> None:
    existed = destination.is_file()
    backup_path = None
    original_hash = None
    if existed:
        # The same filename can be installed to both the game folder and the
        # prefix, so key the backup on the full destination path.
        tag = hashlib.md5(str(destination).encode()).hexdigest()[:8]
        backup_path = backup / f"{destination.name}.{tag}.original"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup_path)
        original_hash = sha256_file(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    records.append({
        "name": destination.name,
        "category": category,
        "target": str(destination),
        "existed_before": existed,
        "original_hash": original_hash,
        "backup": str(backup_path) if backup_path else None,
        "installed_hash": sha256_file(destination),
    })
    log(f"installed {destination}", "OK")


def write_text_file(text: str, destination: Path, backup: Path,
                    records: list[dict], category: str) -> None:
    existed = destination.is_file()
    backup_path = None
    original_hash = None
    if existed:
        backup_path = backup / f"{destination.name}.original"
        shutil.copy2(destination, backup_path)
        original_hash = sha256_file(destination)
    destination.write_text(text, encoding="utf-8")
    records.append({
        "name": destination.name,
        "category": category,
        "target": str(destination),
        "existed_before": existed,
        "original_hash": original_hash,
        "backup": str(backup_path) if backup_path else None,
        "installed_hash": sha256_file(destination),
    })
    log(f"wrote {destination}", "OK")


def install_mv_provider(source: Path, effects: Path, backup: Path,
                        records: list[dict]) -> list[Path]:
    """Copy a user-supplied motion-vector provider into the shader folder.

    Providers ship as a handful of .fx/.fxh files (ReshadeMotionEstimation is
    MotionEstimation.fx plus three headers). Nothing is downloaded: these are
    third-party, often non-commercial-licensed, so the user supplies them.
    """
    source = source.expanduser()
    if not source.exists():
        raise Fail(f"--mv-provider path does not exist: {source}")
    if source.is_file():
        candidates = [source]
    else:
        candidates = sorted(p for p in source.rglob("*")
                            if p.suffix.lower() in (".fx", ".fxh"))
    if not candidates:
        raise Fail(f"No .fx/.fxh files found under {source}")

    installed: list[Path] = []
    for shader in candidates:
        destination = effects / shader.name
        install_file(shader, destination, backup, records, "feeder")
        installed.append(destination)

    if not any(MV_TEXTURE in p.read_text(encoding="utf-8", errors="replace")
               for p in installed if p.suffix.lower() == ".fx"):
        log(f"None of the installed effects mention {MV_TEXTURE}; this may not "
            "be a motion-vector provider", "WARN")
    return installed


def install_d3dcompiler(target: Target, backup: Path | None = None,
                        records: list[dict] | None = None) -> bool:
    """Ensure ReShade can compile .fx effects. Returns True if installed
    app-locally (which means the caller must add a d3dcompiler_47 override).

    Feeder mode makes this load-bearing rather than optional: DLSS5_Feed.fx and
    the motion-vector provider are real effects that have to compile.
    """
    system32 = target.system32
    if system32 and (system32 / "d3dcompiler_47.dll").is_file():
        if (system32 / "d3dcompiler_47.dll").stat().st_size > 2_000_000:
            log("d3dcompiler_47 already native in the prefix", "OK")
            return False

    if not target.appid or shutil.which("protontricks") is None:
        # No appid (non-Steam) or no protontricks: drop a native copy beside the
        # executable instead. The app directory is searched first, and the
        # printed WINEDLLOVERRIDES makes Wine prefer it over the built-in.
        source = find_native_d3dcompiler()
        if source is None:
            log("No native d3dcompiler_47.dll found to copy. If ReShade effects "
                "fail to compile, install it into the prefix with winetricks", "WARN")
            return False
        destination = target.folder / "d3dcompiler_47.dll"
        if backup is not None and records is not None:
            install_file(source, destination, backup, records, "reshade")
        else:
            shutil.copy2(source, destination)
            log(f"installed {destination}", "OK")
        log(f"Reused a native d3dcompiler_47 from {source.parent}", "INFO")
        return True
    return _install_d3dcompiler_protontricks(target)


def _install_d3dcompiler_protontricks(target: Target) -> bool:
    if not target.appid:
        return False
    if not target.system32:
        log("Skipping d3dcompiler_47: the prefix is empty. Run the game once, then "
            "'protontricks " + target.appid + " d3dcompiler_47'", "WARN")
        return False
    log("Installing d3dcompiler_47 into the prefix via protontricks", "STEP")
    env = dict(os.environ)
    if target.prefix:
        env["STEAM_COMPAT_DATA_PATH"] = str(target.prefix.parent)
    result = subprocess.run(
        ["protontricks", "--no-bwrap", target.appid, "-q", "d3dcompiler_47"],
        env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log("protontricks failed; falling back to an app-local copy", "WARN")
        log((result.stderr or result.stdout).strip()[-400:], "WARN")
        source = find_native_d3dcompiler()
        if source is not None:
            shutil.copy2(source, target.folder / "d3dcompiler_47.dll")
            log(f"installed {target.folder / 'd3dcompiler_47.dll'} (app-local)", "OK")
            return True
        return False
    log("d3dcompiler_47 installed", "OK")
    return False


def build_launch_options(target: Target, loader: str, dlss_dest: str = "auto",
                         extra_overrides: list[str] | None = None) -> str:
    """Merge the required loader override into the game's existing options.

    Returns the string exactly as it should be pasted into Steam's launch
    options box -- plain quotes, not the escaped form stored on disk.
    """
    override = f"{Path(loader).stem}=n,b"
    wanted = [override, *(extra_overrides or [])]
    existing = (launch_options_for(target.appid) if target.appid else None) or ""

    # Writing DLSS 5 into the prefix only to have Proton replace it on the next
    # launch is the single most confusing failure mode here, so drop the flag.
    if dlss_dest in ("system32", "both"):
        existing = re.sub(r"\s*PROTON_DLSS_UPGRADE=1\b", "", existing)

    if not existing.strip():
        return 'WINEDLLOVERRIDES="' + ";".join(wanted) + '" %command%'

    match = re.search(r'WINEDLLOVERRIDES="([^"]*)"', existing)
    if match:
        stale = {Path(name).stem for name in LOADER_NAMES
                 if name != loader and not (target.folder / name).is_file()}
        # Drop overrides for proxy names left over from an earlier attempt whose
        # DLL is no longer on disk; they are noise that muddies the next bisect.
        parts = [part for part in match.group(1).split(";")
                 if part and part.split("=")[0] not in stale]
        for want in wanted:
            key = want.split("=")[0]
            if not any(part.split("=")[0] == key for part in parts):
                parts.append(want)
        if not parts:
            return (existing[:match.start()] + existing[match.end():]).replace("  ", " ")
        replacement = 'WINEDLLOVERRIDES="' + ";".join(parts) + '"'
        return existing[:match.start()] + replacement + existing[match.end():]

    joined = 'WINEDLLOVERRIDES="' + ";".join(wanted) + '"'
    if "%command%" in existing:
        return existing.replace("%command%", f"{joined} %command%", 1)
    return f"{existing} {joined} %command%"


def do_install(args) -> int:
    mode = resolve_mode(args)
    feeder = mode == "feeder"
    optiscaler = mode == "optiscaler"

    # OptiScaler drives the model itself, so the RenoDX add-on is not needed.
    addon_path = Path(args.addon) if args.addon else default_addon()
    if not addon_path and not optiscaler:
        raise Fail(f"Could not find {ADDON_NAME}; pass --addon")
    if optiscaler:
        addon_path = None
    zip_path = Path(args.zip) if args.zip else default_zip()
    if not zip_path:
        raise Fail("Could not find a DLSS ZIP containing nvngx_dlssnr.dll; pass --zip")

    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    nr_override = getattr(args, "nr_runtime", None)
    payload = prepare_payload(zip_path, addon_path, bool(args.addon),
                              Path(nr_override) if nr_override else None)

    # The feeder and OptiScaler both keep their runtime beside the exe; there is
    # no existing game DLSS folder to also populate.
    dlss_dest = "game" if (feeder or optiscaler) else args.dlss_dest
    loader = args.loader
    if optiscaler and loader not in OPTISCALER_PROXY_NAMES:
        raise Fail(f"{loader} is not an OptiScaler proxy name. Choose one of: "
                   + ", ".join(OPTISCALER_PROXY_NAMES))
    loader_present = (target.folder / loader).is_file()
    report = scan(target, payload, mode == "full", not loader_present, dlss_dest,
                  loader, feeder, bool(getattr(args, "mv_provider", None)),
                  optiscaler)
    report.show()
    if not report.ok:
        raise Fail("Installation blocked by the compatibility scan")

    if optiscaler:
        mode_label = f"OPTISCALER ({OPTISCALER_VERSION}, no ReShade)"
    elif feeder:
        mode_label = f"FEEDER (DLSS5-Feeder {FEEDER_VERSION}, synthesised DLAA)"
    elif mode == "full":
        mode_label = "ADVANCED (full DLL set)"
    else:
        mode_label = "MINIMAL"
    print()
    log(f"About to install into {target.folder}", "STEP")
    log(f"  mode: {mode_label}   loader: {loader}   dlss-dest: {dlss_dest}", "STEP")
    log("This is an experimental, unofficial injection. It can crash, show a black "
        "screen, or do nothing. Do not use it in online or anti-cheat games.", "WARN")
    if not confirm("Continue?", args.yes):
        log("Cancelled.", "WARN")
        return 1

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.folder / BACKUP_DIR_NAME / timestamp
    backup.mkdir(parents=True, exist_ok=True)
    state_path = target.folder / STATE_NAME
    records: list[dict] = []

    state = {
        "tool": "dlss5-proton",
        "version": VERSION,
        "installed_at": dt.datetime.now().astimezone().isoformat(),
        "mode": mode,
        "optiscaler_version": (
            Path(args.optiscaler_zip).name
            if optiscaler and getattr(args, "optiscaler_zip", None)
            else OPTISCALER_VERSION if optiscaler else None),
        "feeder_version": (
            Path(args.feeder_zip).name
            if feeder and getattr(args, "feeder_zip", None)
            else FEEDER_VERSION if feeder else None),
        "loader": loader,
        "dlss_dest": dlss_dest,
        "appid": target.appid,
        "name": target.name,
        "root": str(target.root),
        "exe": str(target.exe),
        "folder": str(target.folder),
        "prefix": str(target.prefix) if target.prefix else None,
        "dlss_zip": str(payload.zip_path),
        "dlss_zip_sha256": payload.zip_hash,
        "backup": str(backup),
        "files": records,
    }

    def save_state() -> None:
        state["files"] = records
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    try:
        save_state()

        # OptiScaler *is* the loader; ReShade must not be installed at all.
        if optiscaler:
            install_optiscaler(target, loader, backup, records,
                               getattr(args, 'optiscaler_zip', None))
            save_state()
        elif not loader_present:
            reshade = fetch_reshade()
            install_file(reshade, target.folder / loader, backup, records, "reshade")
            save_state()
        else:
            log(f"Reusing existing ReShade loader {loader}", "OK")

        ini = target.folder / "ReShade.ini"
        if optiscaler:
            pass
        elif not ini.is_file():
            write_text_file(RESHADE_INI, ini, backup, records, "reshade")
            save_state()
        else:
            log("Keeping the existing ReShade.ini", "OK")

        if optiscaler:
            selected = ["nvngx_dlssnr.dll"]
        elif feeder:
            selected = FEEDER_DLSS_FILES
        elif mode == "full":
            selected = FULL_FILES
        else:
            selected = MINIMAL_FILES
        for name in selected:
            source = payload.folder / name
            # Always land the NR add-on under its canonical name. The "++"
            # suffix is a Windows-installer convention to avoid clobbering
            # older copies; the feeder (and everything else) looks for
            # renodx-dlss5.addon64 by that exact name.
            out_name = FEEDER_NR_ADDON if name == ADDON_NAME else name
            for destination in destinations_for(out_name, target, dlss_dest):
                install_file(source, destination, backup, records, "dlss5")
                save_state()

        if feeder:
            artifacts = fetch_feeder(getattr(args, 'feeder_zip', None))
            install_file(artifacts[FEEDER_ADDON], target.folder / FEEDER_ADDON,
                         backup, records, "feeder")
            save_state()

            effects = shaders_dir(target)
            effects.mkdir(parents=True, exist_ok=True)

            # Stock headers first: DLSS5_Feed.fx and every motion-vector
            # provider include ReShade.fxh, and ReShade ships no shaders when
            # installed by extracting the DLL.
            for name, source in fetch_reshade_headers().items():
                if (effects / name).is_file():
                    log(f"Keeping the existing {name}", "OK")
                else:
                    install_file(source, effects / name, backup, records, "reshade")
            save_state()

            install_file(artifacts[FEEDER_SHADER], effects / FEEDER_SHADER,
                         backup, records, "feeder")
            save_state()

            provider = getattr(args, "mv_provider", None)
            if getattr(args, "no_mv_provider", False):
                provider = None
                log("Skipping the motion-vector provider (--no-mv-provider); "
                    "motion vectors will be zero", "WARN")
            elif not provider and not find_mv_providers(target):
                fallback = default_mv_provider()
                if fallback:
                    provider = str(fallback)
                    log(f"No motion-vector provider present; using {fallback.name}",
                        "STEP")
            if provider:
                for shader in install_mv_provider(Path(provider), effects,
                                                  backup, records):
                    log(f"installed motion-vector provider file {shader.name}", "OK")
                save_state()

            cfg = target.folder / FEEDER_CFG_NAME
            if cfg.is_file():
                log(f"Keeping the existing {FEEDER_CFG_NAME}", "OK")
            else:
                write_text_file(FEEDER_CFG, cfg, backup, records, "feeder")
                save_state()

        app_local_d3dc = False
        if not args.skip_d3dcompiler:
            app_local_d3dc = install_d3dcompiler(target, backup, records)
            state["app_local_d3dcompiler"] = app_local_d3dc
            save_state()

    except Exception as error:
        log(f"Installation failed; rolling back: {error}", "ERROR")
        skipped = restore_records(records, check_hashes=False)
        if skipped:
            log("Rollback incomplete: " + "; ".join(skipped), "ERROR")
            log(f"Backups remain at {backup}", "ERROR")
        else:
            state_path.unlink(missing_ok=True)
            log("Rollback complete.", "OK")
        raise

    print()
    log(f"Installation complete. Backup: {backup}", "OK")
    print()
    print("Next steps")
    print("-" * 60)
    extra = ["d3dcompiler_47=n"] if app_local_d3dc else []
    if target.appid:
        print(f"1. Set the Steam launch options for {target.name} ({target.appid}) to:\n")
        print(f"   {build_launch_options(target, loader, dlss_dest, extra)}\n")
    else:
        overrides = ";".join([f"{Path(loader).stem}=n,b", *extra])
        print("1. This game is not in your Steam library, so set these wherever it")
        print("   launches from -- Lutris/Heroic: Environment Variables; umu or a")
        print("   shell wrapper: export them before the command:\n")
        print(f'     WINEDLLOVERRIDES="{overrides}"')
        if target.prefix:
            print(f'     WINEPREFIX="{target.prefix}"')
        print()
        print("   Simplest route on this machine: Steam > Add a Non-Steam Game, force")
        print("   a Proton version in its properties, and set its launch options to:\n")
        print(f'     WINEDLLOVERRIDES="{overrides}" %command%\n')
    if optiscaler:
        print("2. Launch the game. Leave the game's OWN DLSS ON -- OptiScaler reads")
        print("   the depth and motion vectors it already produces.")
        print("3. Press INSERT for the OptiScaler overlay (not Home -- no ReShade).")
        print("4. Enable 'Neural Rendering' under DLSS Neural Rendering (off by")
        print("   default). Page Up/Down toggles performance stats.")
        print("5. OptiScaler.log beside the exe says why if it refuses.")
    elif feeder:
        # The feeder supplies its own DLAA pass. If the game also runs DLSS there
        # are two NGX consumers fighting over the frame, so the game's own
        # upscaler must be off -- the opposite of the normal install.
        print("2. Launch the game. Turn the game's OWN upscaler/DLSS OFF (use TAA or")
        print("   none), and turn MSAA/SSAA off.")
        print("3. Press Home for the ReShade overlay.")
        print("4. Enable techniques in this order, top to bottom:")
        print("     a) your motion-vector technique  (DRME)")
        print("     b) DLSS 5 Feed                   (must sit BELOW it)")
        print("     c) neural rendering, in the DLSS 5 Neural Rendering panel")
        print(f"5. Check {FEEDER_CFG_NAME[:-4]}.log beside the exe for "
              "'feature ready ... DLAA'")
        print("   and 'frame N delivered'. If the image smears when you move, the")
        print("   motion-vector provider is not feeding it.")
    else:
        print("2. Launch the game, enable DirectX 12 and DLSS in its graphics settings.")
        print("3. Press Home to open the ReShade overlay.")
        print("4. Open Add-ons > DLSS Neural Rendering and enable it.")
    print(f"\nTo undo:  dlss5-proton restore {args.target!r}")
    return 0


def restore_records(records: list[dict], check_hashes: bool = True,
                    force: bool = False) -> list[str]:
    """Undo records in reverse order. Returns a list of skipped targets."""
    skipped: list[str] = []
    for record in reversed(records):
        target_path = Path(record["target"])
        if check_hashes and target_path.is_file():
            if sha256_file(target_path) != record.get("installed_hash") and not force:
                skipped.append(f"{target_path} (changed since install)")
                continue
        if record["existed_before"]:
            backup = Path(record["backup"]) if record.get("backup") else None
            if not backup or not backup.is_file():
                skipped.append(f"{target_path} (backup missing)")
                continue
            if sha256_file(backup) != record.get("original_hash"):
                skipped.append(f"{target_path} (backup hash mismatch)")
                continue
            shutil.copy2(backup, target_path)
            log(f"restored {target_path}", "OK")
        elif target_path.is_file():
            target_path.unlink()
            log(f"removed {target_path}", "OK")
    return skipped


def load_state(target: Target) -> tuple[Path, dict]:
    state_path = target.folder / STATE_NAME
    if not state_path.is_file():
        raise Fail(f"No installer state found in {target.folder}")
    return state_path, json.loads(state_path.read_text(encoding="utf-8"))


def do_restore(args) -> int:
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    state_path, state = load_state(target)
    records = state.get("files", [])
    if not records:
        raise Fail("Installer state contains no files")

    if process_running(target.exe.name):
        raise Fail(f"{target.exe.name} appears to be running. Close it first")

    changed = [r["target"] for r in records
               if Path(r["target"]).is_file()
               and sha256_file(Path(r["target"])) != r.get("installed_hash")]
    force = args.force
    if changed and not force:
        log("These files changed after installation:", "WARN")
        for path in changed:
            log(f"  {path}", "WARN")
        force = confirm("Restore over them anyway?", args.yes)

    if not confirm(f"Restore {len(records)} file(s) in {target.folder}?", args.yes):
        log("Cancelled.", "WARN")
        return 1

    skipped = restore_records(records, check_hashes=True, force=force)
    if skipped:
        log("Skipped: " + "; ".join(skipped), "WARN")
        log("Installer state retained so you can retry.", "WARN")
    else:
        state_path.unlink()
        log("Original files restored.", "OK")

    backup_dir = Path(state["backup"]) if state.get("backup") else None
    if backup_dir and backup_dir.is_dir() and not any(backup_dir.iterdir()):
        # Nothing was ever overwritten, so there is nothing worth keeping.
        backup_dir.rmdir()
        parent = backup_dir.parent
        if parent.is_dir() and parent.name == BACKUP_DIR_NAME and not any(parent.iterdir()):
            parent.rmdir()
        log("No files were overwritten; empty backup folder removed.", "INFO")
    else:
        log(f"Backup retained at {state.get('backup')}", "INFO")
    return 0 if not skipped else 1


def toggle_file(folder: Path, filename: str, state: str, label: str,
                hint: str = "") -> int:
    """Rename a component in or out of the way, for bisecting a crash."""
    live = folder / filename
    parked = folder / (filename + ".disabled")

    if state == "status":
        if live.is_file():
            log(f"{label} ENABLED: {live}", "OK")
        elif parked.is_file():
            log(f"{label} DISABLED: {parked}", "WARN")
        else:
            log(f"{label} not present in {folder}", "WARN")
        return 0

    if state == "off":
        if parked.is_file() and not live.is_file():
            log(f"{label} is already disabled.", "OK")
            return 0
        if not live.is_file():
            raise Fail(f"{filename} is not present in {folder}")
        live.rename(parked)
        log(f"{label} disabled (renamed to {parked.name}).", "OK")
        if hint:
            log(hint, "INFO")
        return 0

    if live.is_file():
        log(f"{label} is already enabled.", "OK")
        return 0
    if not parked.is_file():
        raise Fail(f"No {parked.name} to restore in {folder}")
    parked.rename(live)
    log(f"{label} enabled: {live}", "OK")
    return 0


# ---------------------------------------------------------------------------
# Verification -- read the logs the way a human would
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """One verification result. ok=None means 'could not tell'."""
    ok: bool | None
    label: str
    detail: str = ""

    @property
    def mark(self) -> str:
        return {True: "PASS", False: "FAIL", None: "  ? "}[self.ok]


def _tail_read(path: Path, limit: int = 8_000_000) -> str:
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read()
    except OSError:
        return ""


def verify_install(target: Target) -> list[Check]:
    """Answer 'did it actually work?' from the same evidence a human uses.

    Every failure mode hit during development was diagnosed by reading
    ReShade.log and dlss5-feed.log, in a specific order: did ReShade load at
    all, did the add-on register, did NGX initialise, is it evaluating or
    skipping. This encodes that order so the answer is not a guess.
    """
    checks: list[Check] = []
    state_path = target.folder / STATE_NAME
    state: dict = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}

    if not state:
        checks.append(Check(False, "Installed", f"no {STATE_NAME} in {target.folder}"))
        return checks

    mode = state.get("mode", "?")
    feeder = mode == "feeder"
    checks.append(Check(True, "Installed",
                        f"mode={mode}, loader={state.get('loader')}, "
                        f"{state.get('installed_at', '?')[:19]}"))

    # --- files still as installed ------------------------------------------
    bad, rewritten, total = [], [], 0
    for record in state.get("files", []):
        path = Path(record["target"])
        total += 1
        if not path.is_file():
            bad.append(f"{path.name} MISSING")
        elif sha256_file(path) != record.get("installed_hash"):
            (rewritten if path.name in RUNTIME_OWNED else bad).append(path.name)
    detail = f"{total} files match" if not bad else ", ".join(bad)
    if rewritten:
        detail += f" (config rewritten by the app: {', '.join(rewritten)})"
    checks.append(Check(not bad, "Files intact", detail))

    if mode == "optiscaler":
        return checks + verify_optiscaler(target, state)

    # Read the log up front: it is stronger evidence than anything else here,
    # and it settles the launch-option question below.
    log_path = target.folder / "ReShade.log"
    installed_at = state_path.stat().st_mtime
    log_fresh = log_path.is_file() and log_path.stat().st_mtime >= installed_at
    text = _tail_read(log_path) if log_path.is_file() else ""
    init = re.search(r"Initializing crosire's ReShade version '([^']+)'.*?loaded from "
                     r"'([^']+)'", text)
    reshade_loaded = bool(init) and log_fresh

    # --- launch options -----------------------------------------------------
    if target.appid:
        options = launch_options_for(target.appid) or ""
        stem = Path(state.get("loader", "dxgi.dll")).stem
        has = re.search(r'WINEDLLOVERRIDES="([^"]*)"', options)
        ok = bool(has and any(p.split("=")[0] == stem for p in has.group(1).split(";")))
        if ok:
            checks.append(Check(True, "Launch option", f"{stem}=n,b present"))
        elif init:
            # Steam keeps launch options in memory while running, so the on-disk
            # copy lags. A fresh ReShade.log proves the override is live.
            checks.append(Check(True, "Launch option",
                                "not in Steam's on-disk config, but ReShade has "
                                "loaded from this folder, so it is set (Steam only "
                                "flushes launch options on exit)"))
        else:
            checks.append(Check(False, "Launch option",
                                f"WINEDLLOVERRIDES is missing {stem} -- ReShade "
                                "will not load"))

    # --- ReShade.log --------------------------------------------------------
    if not log_path.is_file():
        checks.append(Check(False, "ReShade loaded",
                            "no ReShade.log -- the game has not run, or the "
                            "loader was never injected"))
        return checks

    stamp = dt.datetime.fromtimestamp(log_path.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S")
    if not log_fresh:
        checks.append(Check(None, "ReShade.log is stale",
                            f"written {stamp}, before this install -- launch the "
                            "game, then verify again"))
        return checks
    if init:
        checks.append(Check(True, "ReShade loaded",
                            f"v{init.group(1)} from {Path(init.group(2)).name} ({stamp})"))
    else:
        checks.append(Check(False, "ReShade loaded", f"no init line in {log_path.name}"))

    failed = sorted(set(re.findall(r"Failed to compile '([^']+)'", text)))
    if failed:
        checks.append(Check(False, "Effects compile",
                            ", ".join(Path(f).name for f in failed)))
    elif "Successfully compiled" in text or feeder:
        checks.append(Check(True, "Effects compile", "no compile failures"))

    addons = sorted(set(re.findall(r'Registered add-on "([^"]+)"', text)))
    if feeder and not any("Feed" in a for a in addons):
        checks.append(Check(False, "Feeder add-on", "dlss5-feed did not register"))
    if addons:
        checks.append(Check(True, "Add-ons registered", ", ".join(addons)))
    else:
        checks.append(Check(False, "Add-ons registered",
                            "none -- is this the add-on build of ReShade?"))

    # --- neural rendering ---------------------------------------------------
    runtime = re.search(r"signed DLSSNR (\S+) D3D12 runtime initialized", text)
    checks.append(Check(bool(runtime), "NR runtime",
                        f"DLSSNR {runtime.group(1)} initialised" if runtime else
                        "never initialised -- NGX did not come up"))

    resources = re.findall(r"created inline NR resources (\S+) -> (\S+) \((\w+)\) "
                           r"format=(\d+)", text)
    if resources:
        last = resources[-1]
        checks.append(Check(True, "NR resources",
                            f"{last[0]} -> {last[1]} ({last[2]}) format={last[3]}"))

    counts = [int(n) for n in re.findall(r"evaluation succeeded \(count=(\d+)", text)]
    skips = len(re.findall(r"skipped an NGX evaluation", text))
    if counts and max(counts) > 1:
        checks.append(Check(True, "NR evaluating", f"count reached {max(counts)}"))
    elif counts:
        checks.append(Check(False, "NR evaluating",
                            "only one evaluation, then it stopped"))
    else:
        checks.append(Check(False, "NR evaluating", "no successful evaluations"))

    if skips:
        checks.append(Check(False, "Dimension skips",
                            f"{skips} evaluations skipped -- guide/output mismatch; "
                            "try --full so the game's DLSS DLLs match the runtime"))
    else:
        checks.append(Check(True, "Dimension skips", "none"))

    # --- feeder -------------------------------------------------------------
    if feeder:
        feed_log = target.folder / "dlss5-feed.log"
        if not feed_log.is_file():
            checks.append(Check(False, "Feeder", "no dlss5-feed.log"))
        else:
            feed = _tail_read(feed_log)
            ready = re.search(r"feature ready[^\n]*", feed)
            checks.append(Check(bool(ready), "Feeder feature",
                                ready.group(0).strip() if ready else "never became ready"))
            frames = [int(n) for n in re.findall(r"frame (\d+) delivered", feed)]
            checks.append(Check(bool(frames), "Feeder frames",
                                f"{max(frames)} delivered" if frames else "none delivered"))
            # Read the LAST effects line rather than grepping the whole log:
            # the warning is emitted before the user ticks DRME, so a whole-file
            # search reports a stale failure forever after.
            states = re.findall(r"MV provider ([^,]+),", feed)
            if states:
                current = states[-1].strip()
                live = not current.startswith("none")
                checks.append(Check(live, "Motion vectors",
                                    f"provider: {current}" if live else
                                    "none -- motion vectors are zero, so the image "
                                    "will smear when you move. Tick DRME above "
                                    "DLSS 5 Feed"))
    return checks


def verify_optiscaler(target: Target, state: dict) -> list[Check]:
    """Read OptiScaler.log and report whether the NR pass actually ran."""
    checks: list[Check] = []
    log_path = target.folder / "OptiScaler.log"
    if not log_path.is_file():
        checks.append(Check(False, "OptiScaler ran",
                            "no OptiScaler.log -- the game has not run, or the "
                            "proxy DLL was never loaded"))
        return checks

    stem = Path(state.get("loader", "dxgi.dll")).stem
    options = launch_options_for(target.appid) if target.appid else ""
    has = re.search(r'WINEDLLOVERRIDES="([^"]*)"', options or "")
    ok = bool(has and any(x.split("=")[0] == stem for x in has.group(1).split(";")))
    checks.append(Check(ok, "Launch option",
                        f"{stem}=n,b present" if ok else
                        f"WINEDLLOVERRIDES needs {stem}=n,b -- without it Wine loads "
                        "its builtin / system32 copy and the proxy never runs"))

    stamp = dt.datetime.fromtimestamp(log_path.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S")
    fresh = log_path.stat().st_mtime >= (target.folder / STATE_NAME).stat().st_mtime
    text = _tail_read(log_path)
    if not fresh:
        checks.append(Check(None, "OptiScaler.log is stale",
                            f"written {stamp}, before this install -- launch the game"))
        return checks
    checks.append(Check(True, "OptiScaler ran", stamp))

    forwarder = re.search(r"DLSS-NR forwarder loaded from ([^\n]+)", text)
    checks.append(Check(bool(forwarder), "NR forwarder",
                        "nvngx.dll_dlssnr.dll loaded" if forwarder else
                        "not loaded -- is nvngx.dll_dlssnr.dll beside the exe?"))

    runs = re.findall(r"DLSS-NR running at (\d+x\d+), guides (\d+x\d+) "
                      r"\(preset (\d+), intensity (\d+), style (\d+)\)", text)
    if runs:
        last = runs[-1]
        sizes = sorted({r[0] for r in runs})
        checks.append(Check(True, "NR dispatching",
                            f"{len(runs)} build(s); latest {last[0]} "
                            f"preset={last[2]} style={last[4]}; sizes seen: "
                            + ", ".join(sizes)))
    else:
        checks.append(Check(False, "NR dispatching",
                            "no 'DLSS-NR running at' lines -- enable Neural "
                            "Rendering in the overlay (Insert)"))

    colour = re.search(r"DLSS-NR: the game's DLSS buffer is ([^\n]+)", text)
    if colour:
        checks.append(Check(True, "Colour path", colour.group(1).strip()))

    # OptiScaler forwards the game's Streamline log through its own, so those
    # lines are not OptiScaler failures. Under Wine the OTA updater can never
    # spawn nvngx_update.exe, and DLSS_G has no context unless frame generation
    # is on -- both are expected noise.
    errors = [line for line in text.splitlines() if "] [E] " in line]
    forwarded = [line for line in errors if "streamlineLogCallback" in line]
    benign = ("queryNvapi",)   # GPU identification only; NR is unaffected
    own = [line for line in errors
           if line not in forwarded and not any(b in line for b in benign)]
    detail = "none" if not own else f"{len(own)} error line(s)"
    ignored = len(errors) - len(own)
    if ignored:
        detail += f" ({ignored} benign/forwarded messages ignored)"
    checks.append(Check(not own, "OptiScaler errors", detail))
    return checks

def do_verify(args) -> int:
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    checks = verify_install(target)
    print(f"{target.describe()}\n{'-' * 60}")
    for check in checks:
        level = {True: "OK", False: "ERROR", None: "WARN"}[check.ok]
        log(f"[{check.mark}] {check.label}: {check.detail}", level)
    failed = [c for c in checks if c.ok is False]
    print()
    if failed:
        log(f"{len(failed)} check(s) failed.", "ERROR")
        return 2
    log("All checks passed.", "OK")
    return 0


def do_addon(args) -> int:
    """Toggle just the add-on, leaving ReShade in place."""
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    return toggle_file(
        target.folder, ADDON_NAME, args.state, "Add-on",
        "ReShade still loads. If the game survives now, the add-on is the crash; "
        "if it still dies, ReShade or the loader is.",
    )


def do_loader(args) -> int:
    """Toggle the ReShade loader itself -- the true baseline control.

    With the loader renamed away, Wine falls back to its built-in DLL no matter
    what WINEDLLOVERRIDES says, so the game runs completely unmodded without
    having to edit Steam launch options.
    """
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    loader = "dxgi.dll"
    state_path = target.folder / STATE_NAME
    if state_path.is_file():
        try:
            loader = json.loads(state_path.read_text(encoding="utf-8")).get(
                "loader", loader)
        except (OSError, json.JSONDecodeError):
            pass
    return toggle_file(
        target.folder, loader, args.state, f"ReShade loader ({loader})",
        "Wine will use its built-in DLL. The game is now effectively unmodded -- "
        "if it STILL crashes, ReShade was never the cause.",
    )


def do_status(args) -> int:
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    print(f"Game:    {target.describe()}")
    print(f"Root:    {target.root}")
    print(f"EXE:     {target.exe}")
    print(f"Folder:  {target.folder}")
    print(f"Prefix:  {target.prefix or '(not found)'}")
    existing, where = find_existing_dlss(target)
    print(f"DLSS:    {existing or '(none found)'}" + (f"  [{where}]" if where else ""))
    if target.appid:
        print(f"Launch:  {launch_options_for(target.appid) or '(none set)'}")

    state_path = target.folder / STATE_NAME
    if not state_path.is_file():
        print("\nNot installed.")
        return 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(f"\nInstalled {state.get('installed_at')} "
          f"(mode={state.get('mode')}, loader={state.get('loader')}, "
          f"dlss-dest={state.get('dlss_dest')})")
    for record in state.get("files", []):
        path = Path(record["target"])
        if not path.is_file():
            mark = "MISSING"
        elif sha256_file(path) == record.get("installed_hash"):
            mark = "ok"
        else:
            mark = "CHANGED"
        print(f"  [{mark:>7}] {path}")
    return 0


def do_launch_options(args) -> int:
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    if not getattr(args, "loader", None):
        # Use the loader the install actually used, not a guess.
        state_path = target.folder / STATE_NAME
        args.loader = "dxgi.dll"
        if state_path.is_file():
            try:
                args.loader = json.loads(state_path.read_text(encoding="utf-8")).get(
                    "loader", "dxgi.dll")
            except (OSError, json.JSONDecodeError):
                pass
    print(build_launch_options(target, args.loader, getattr(args, "dlss_dest", "auto")))
    return 0


def do_check(args) -> int:
    addon_path = Path(args.addon) if args.addon else default_addon()
    zip_path = Path(args.zip) if args.zip else default_zip()
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    payload = None
    if addon_path and zip_path:
        payload = prepare_payload(zip_path, addon_path, bool(args.addon),
                              Path(args.nr_runtime) if getattr(args, "nr_runtime", None) else None)
    else:
        log("No add-on or DLSS ZIP resolved; payload checks skipped", "WARN")
    mode = resolve_mode(args)
    feeder = mode == "feeder"
    dlss_dest = "game" if mode in ("feeder", "optiscaler") else args.dlss_dest
    loader_present = (target.folder / args.loader).is_file()
    report = scan(target, payload, mode == "full", not loader_present, dlss_dest,
                  args.loader, feeder, bool(getattr(args, "mv_provider", None)),
                  mode == "optiscaler")
    report.show()
    return 0 if report.ok else 2


def do_scan(args) -> int:
    games = installed_games()
    if not games:
        raise Fail("No installed Steam games found")

    rows = []
    for game in games.values():
        prefix = find_prefix(game.appid)
        target_dir = game.install_dir
        has_state = any(target_dir.rglob(STATE_NAME))
        dlss = "-"
        if next(target_dir.rglob("nvngx_dlss.dll"), None):
            dlss = "game"
        elif prefix and (prefix / "drive_c/windows/system32/nvngx_dlss.dll").is_file():
            dlss = "prefix"
        if args.dlss_only and dlss == "-":
            continue
        rows.append((game.appid, game.name, "yes" if prefix else "no", dlss,
                     "YES" if has_state else ""))

    rows.sort(key=lambda row: row[1].lower())
    width = max((len(row[1]) for row in rows), default=20)
    print(f"{'APPID':<10} {'NAME':<{width}}  {'PFX':<4} {'DLSS':<6} INSTALLED")
    print("-" * (10 + width + 22))
    for appid, name, prefix, dlss, installed in rows:
        print(f"{appid:<10} {name:<{width}}  {prefix:<4} {dlss:<6} {installed}")
    print(f"\n{len(rows)} game(s). DLSS column: where nvngx_dlss.dll was found.")
    return 0


# ---------------------------------------------------------------------------
# DLSS runtime DLL swapping -- deliberately independent of the mod install
# ---------------------------------------------------------------------------
#
# Games ship whatever DLSS runtime they were built against and rarely update it,
# so a title can sit years behind (Horizon Forbidden West ships 3.5.10, from
# before the transformer model). In OptiScaler mode neural rendering runs AFTER
# the game's own upscaler, so the upscaler's version sets the quality of the
# image NR is handed -- which makes this worth doing on its own.
#
# This keeps its own state file rather than joining the mod's. The two are
# reinstalled on completely different schedules: `restore` + `install` runs
# every time OptiScaler ships a build, and that must not silently revert a DLL
# swap (or, worse, restore the swapped DLL as if it were the game's original).

DLLS_STATE_NAME = "_dlss5_proton_dlls.json"

# nvngx_dlssnr.dll is deliberately NOT here: that one belongs to the neural
# rendering mod, `install` already places the newest build, and it is tracked in
# the mod's state file.
DLL_SWAP_FILES = ["nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll"]

DLL_LABELS = {
    "nvngx_dlss.dll": "Super Resolution",
    "nvngx_dlssd.dll": "Ray Reconstruction",
    "nvngx_dlssg.dll": "Frame Generation",
}

DLL_ALIASES = {"sr": "nvngx_dlss.dll", "rr": "nvngx_dlssd.dll",
               "fg": "nvngx_dlssg.dll"}

# Streamline is the game's integration layer, not a driver runtime: sl.interposer
# is loaded by the game's own code and the plugins are version-matched against
# it. Swapping it works in some titles and breaks others, so it stays opt-in.
STREAMLINE_SWAP_FILES = [
    "sl.common.dll", "sl.interposer.dll", "sl.deepdvc.dll", "sl.dlss.dll",
    "sl.dlss_d.dll", "sl.dlss_g.dll", "sl.dlss_nr.dll", "sl.nis.dll",
    "sl.pcl.dll", "sl.reflex.dll",
]


def pe_version_bytes(data: bytes) -> str | None:
    """FileVersion from a PE image, read out of the VS_VERSION_INFO resource.

    Anchoring on the resource matters. The 0xFEEF04BD signature also occurs in
    .rdata in these binaries, so matching the first hit anywhere in the file
    reports nonsense (46863.4696.3908.4208 rather than 310.8.0.0).
    """
    anchor = data.find("VS_VERSION_INFO".encode("utf-16-le"))
    if anchor < 0:
        return None
    at = data.find(b"\xBD\x04\xEF\xFE", anchor, anchor + 0x200)
    if at < 0:
        return None
    ms, ls = struct.unpack_from("<II", data, at + 8)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def pe_file_version(path: Path) -> str | None:
    try:
        return pe_version_bytes(path.read_bytes())
    except OSError:
        return None


def version_key(version: str | None) -> tuple[int, ...]:
    """Sortable form of a dotted version. Unknown sorts below everything."""
    if not version:
        return (-1,)
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (-1,)


MAX_ZIP_DEPTH = 3


@dataclass
class DllSource:
    """One candidate runtime DLL, read on demand from wherever it lives."""
    name: str
    version: str | None
    origin: str
    root: Path
    chain: list[str]

    def read(self) -> bytes:
        return read_member(self.root, self.chain)


def read_member(root: Path, chain: list[str]) -> bytes:
    """Bytes of a file, following a chain of nested ZIP members."""
    if not chain:
        return root.read_bytes()
    with zipfile.ZipFile(root) as archive:
        return _read_member_zip(archive, chain)


def _read_member_zip(archive: zipfile.ZipFile, chain: list[str]) -> bytes:
    with archive.open(chain[0]) as handle:
        blob = handle.read()
    if len(chain) == 1:
        return blob
    with zipfile.ZipFile(io.BytesIO(blob)) as inner:
        return _read_member_zip(inner, chain[1:])


def walk_dll_sources(root: Path, wanted: set[str]):
    """Yield a DllSource for every wanted DLL under root.

    Nothing is written to disk. Whole-archive extraction is not an option here:
    the 310.8.0 bundle expands to 327 MB, the 310.9.0 drop is a ZIP of three
    ZIPs, and /home does not have the room to spare for either.
    """
    if root.is_dir():
        for path in root.rglob("*.dll"):
            if path.name.lower() in wanted:
                yield DllSource(path.name, pe_file_version(path),
                                str(path), path, [])
        return
    if not root.is_file():
        raise Fail(f"DLL source does not exist: {root}")
    try:
        with zipfile.ZipFile(root) as archive:
            yield from _walk_zip(archive, wanted, root, [], root.name, 0)
    except zipfile.BadZipFile:
        raise Fail(f"Not a readable ZIP: {root}")


def _walk_zip(archive: zipfile.ZipFile, wanted: set[str], root: Path,
              chain: list[str], label: str, depth: int):
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        member = entry.filename.replace("\\", "/")
        base = member.rsplit("/", 1)[-1]
        low = base.lower()
        if low in wanted:
            with archive.open(entry) as handle:
                blob = handle.read()
            yield DllSource(base, pe_version_bytes(blob),
                            f"{label}!{member}", root, chain + [entry.filename])
        elif low.endswith(".zip") and depth < MAX_ZIP_DEPTH:
            with archive.open(entry) as handle:
                blob = handle.read()
            try:
                with zipfile.ZipFile(io.BytesIO(blob)) as inner:
                    yield from _walk_zip(inner, wanted, root,
                                         chain + [entry.filename],
                                         f"{label}!{member}", depth + 1)
            except zipfile.BadZipFile:
                continue


def dll_source_candidates() -> list[Path]:
    """Archives near the project that plausibly carry DLSS runtimes."""
    here = Path(__file__).resolve().parent
    wanted = {n.lower() for n in DLL_SWAP_FILES + STREAMLINE_SWAP_FILES}
    found: list[Path] = []
    for candidate in sorted({p for directory in (here, here.parent)
                             for p in directory.glob("*.zip")}):
        try:
            with zipfile.ZipFile(candidate) as archive:
                names = [n.replace("\\", "/").rsplit("/", 1)[-1].lower()
                         for n in archive.namelist()]
        except (zipfile.BadZipFile, OSError):
            continue
        # Either the DLLs directly, or the nested-ZIP layout 310.9.0 ships in.
        if any(n in wanted for n in names) or any(
                n.endswith(".zip") and "dlss" in n for n in names):
            found.append(candidate)
    return found


def collect_dll_sources(spec: Path | None,
                        names: list[str]) -> dict[str, DllSource]:
    """Highest-versioned copy of each wanted DLL across the source(s)."""
    if spec is not None:
        roots = [Path(spec).expanduser()]
    else:
        roots = dll_source_candidates()
        if not roots:
            raise Fail("No DLSS runtime archive found next to the tool. "
                       "Pass --from <zip-or-folder>")
    best: dict[str, DllSource] = {}
    wanted = {n.lower() for n in names}
    for root in roots:
        for source in walk_dll_sources(root, wanted):
            key = source.name.lower()
            current = best.get(key)
            if current is None or version_key(source.version) > version_key(current.version):
                best[key] = source
    return best


def mod_owned_paths(target: Target) -> set[str]:
    """Files the mod install owns, so a swap never fights `restore`."""
    state_path = target.folder / STATE_NAME
    if not state_path.is_file():
        return set()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {r["target"] for r in state.get("files", []) if "target" in r}


def game_dll_targets(target: Target, names: list[str]) -> dict[str, list[Path]]:
    """Existing copies of each DLL in the game tree.

    Only files already present are candidates. Dropping a runtime into a game
    that never shipped one achieves nothing -- the game asks NGX for the
    features it was built to use -- and the prefix's system32 copy is skipped
    on purpose because Proton rewrites it on every launch.
    """
    wanted = {n.lower() for n in names}
    owned = mod_owned_paths(target)
    found: dict[str, list[Path]] = {}
    for path in target.root.rglob("*.dll"):
        key = path.name.lower()
        if key not in wanted:
            continue
        if BACKUP_DIR_NAME in path.parts or str(path) in owned:
            continue
        found.setdefault(key, []).append(path)
    return {key: sorted(paths) for key, paths in found.items()}


def dll_swap_live(record: dict) -> bool:
    """True when the swapped file is still the one on disk.

    A game update rewrites the runtimes it ships (007 First Light's 2026-09-15
    patch put its own 310.7.128 back over our 310.9.1), and the state file
    cannot know. So the record is checked against the file every time it is
    shown or acted on, and a record whose file has moved on is reported as
    such rather than as a live swap."""
    path = Path(record.get("target", ""))
    return path.is_file() and sha256_file(path) == record.get("installed_hash")


def load_dll_state(target: Target) -> tuple[Path, dict]:
    state_path = target.folder / DLLS_STATE_NAME
    if not state_path.is_file():
        return state_path, {}
    try:
        return state_path, json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state_path, {}


def print_dll_table(present: dict[str, list[Path]], target: Target,
                    sources: dict[str, DllSource],
                    swapped: dict[str, dict]) -> None:
    if not present:
        print("  (no DLSS runtime DLLs in the game tree)")
        return
    for name in sorted(present):
        label = DLL_LABELS.get(name, name)
        for path in present[name]:
            version = pe_file_version(path) or "?"
            mark = ""
            record = swapped.get(str(path))
            if record:
                if dll_swap_live(record):
                    mark = "  <- swapped"
                else:
                    when = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
                    mark = f"  <- swap undone by a game update on {when} (the game's own again)"
            print(f"  {label:<20} {version:<12} "
                  f"{path.relative_to(target.root)}{mark}")
        offered = sources.get(name)
        if offered:
            newer = version_key(offered.version) > max(
                (version_key(pe_file_version(p)) for p in present[name]),
                default=(-1,))
            note = "available" if newer else "available (not newer)"
            print(f"  {'':<20} {offered.version or '?':<12} {note}")


def do_dlls(args) -> int:
    target = resolve_target(args.target, Path(args.prefix) if args.prefix else None)
    names = list(DLL_SWAP_FILES)
    if args.only:
        chosen = []
        for token in args.only.replace(",", " ").split():
            key = DLL_ALIASES.get(token.lower(), token.lower())
            if key not in DLL_SWAP_FILES:
                raise Fail(f"--only: unknown component {token!r} "
                           f"(use sr, rr, fg)")
            chosen.append(key)
        names = chosen
    if args.streamline:
        names += STREAMLINE_SWAP_FILES

    state_path, state = load_dll_state(target)
    records = state.get("files", [])
    swapped = {r["target"]: r for r in records}
    present = game_dll_targets(target, names)

    print(f"\n{target.describe()}")
    print("-" * 60)

    if args.action == "status":
        sources: dict[str, tuple[Path, str | None]] = {}
        try:
            sources = collect_dll_sources(
                Path(args.source) if args.source else None, names)
        except Fail as error:
            log(str(error), "WARN")
        print_dll_table(present, target, sources, swapped)
        live = [r for r in records if dll_swap_live(r)]
        stale = [r for r in records if not dll_swap_live(r)]
        if live:
            print(f"\n{len(live)} DLL(s) swapped on "
                  f"{state.get('installed_at', '?')}. Undo: dlls <target> restore")
        if stale:
            print(f"\n{len(stale)} swap(s) from {state.get('installed_at', '?')} no longer on disk: "
                  "a game update put its own runtimes back. Swap again with "
                  "'dlls <target> install' (the current files become the originals it backs up); "
                  "'restore' clears the stale records.")
        if not records:
            print("\nNo DLLs swapped by this tool.")
        return 0

    if args.action == "restore":
        if not records:
            raise Fail(f"No DLL swap state in {target.folder}")
        if process_running(target.exe.name):
            raise Fail(f"{target.exe.name} appears to be running. Close it first")
        if not confirm(f"Restore {len(records)} DLL(s) in {target.root}?", args.yes):
            log("Cancelled.", "WARN")
            return 1
        live = [r for r in records if dll_swap_live(r)]
        for r in records:
            if r not in live and not args.force:
                log(f"{Path(r['target']).name}: a game update already replaced the swapped file; "
                    "nothing to restore, record cleared", "INFO")
        skipped = restore_records(records if args.force else live,
                                  check_hashes=True, force=args.force)
        if skipped:
            log("Skipped: " + "; ".join(skipped), "WARN")
            log("Swap state retained so you can retry.", "WARN")
            return 1
        state_path.unlink()
        log("Original DLSS runtimes restored.", "OK")
        return 0

    # install
    if process_running(target.exe.name):
        raise Fail(f"{target.exe.name} appears to be running. Close it first")
    sources = collect_dll_sources(
        Path(args.source) if args.source else None, names)

    planned: list[tuple[DllSource, Path, str, str]] = []
    for name in names:
        destinations = present.get(name)
        if not destinations:
            continue
        offered = sources.get(name)
        if not offered:
            log(f"{DLL_LABELS.get(name, name)}: not in the source, leaving alone",
                "INFO")
            continue
        for destination in destinations:
            current = pe_file_version(destination)
            if version_key(offered.version) <= version_key(current) and not args.force:
                log(f"{destination.relative_to(target.root)}: {current} is already "
                    f">= {offered.version}, skipping", "INFO")
                continue
            planned.append((offered, destination, current or "?",
                            offered.version or "?"))

    if not planned:
        log("Nothing to do -- every DLL is already at or above the source "
            "version. Use --force to swap anyway.", "OK")
        return 0

    print("\nPlanned swaps:")
    for _, destination, current, new in planned:
        print(f"  {destination.relative_to(target.root)}")
        print(f"      {current}  ->  {new}")
    if args.streamline:
        log("Streamline DLLs are being swapped. sl.interposer is loaded by the "
            "game's own code and its plugins are version-matched to it, so if "
            "the game stops launching, run: dlls <target> restore", "WARN")

    if not confirm(f"\nSwap {len(planned)} DLL(s)?", args.yes):
        log("Cancelled.", "WARN")
        return 1

    backup = target.folder / BACKUP_DIR_NAME / "dlls"
    backup.mkdir(parents=True, exist_ok=True)
    new_records: list[dict] = []
    # Materialise each source once, in the system temp dir rather than beside
    # the game: these are up to 165 MB and /home is usually the tight volume.
    with tempfile.TemporaryDirectory(prefix="dlss5-dlls-") as staging:
        staged: dict[str, Path] = {}
        for source, destination, _, _ in planned:
            if source.origin not in staged:
                path = Path(staging) / source.name
                path.write_bytes(source.read())
                staged[source.origin] = path
            staged_path = staged[source.origin]
            prior = swapped.get(str(destination))
            if prior and not dll_swap_live(prior):
                # A game update replaced our swap: what is on disk now is the
                # game's own file again, and that is what a later restore must
                # bring back. Back it up fresh; the earlier backup is left.
                log(f"{destination.name}: the earlier swap was undone by a game update; "
                    "backing up the game's current file as the original", "INFO")
                prior = None
            if prior:
                # Already swapped once. Keep pointing at the game's true
                # original, otherwise a second swap would make the first swap's
                # DLL look like the file to restore.
                shutil.copy2(staged_path, destination)
                record = dict(prior)
                record["installed_hash"] = sha256_file(destination)
                new_records.append(record)
                log(f"installed {destination}", "OK")
            else:
                install_file(staged_path, destination, backup, new_records,
                             "dlss-runtime")

    kept = [r for r in records if r["target"] not in
            {rec["target"] for rec in new_records}]
    state_path.write_text(json.dumps({
        "tool": "dlss5-proton",
        "version": VERSION,
        "installed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "appid": target.appid,
        "name": target.name,
        "root": str(target.root),
        "backup": str(backup),
        "files": kept + new_records,
    }, indent=2), encoding="utf-8")

    log(f"Swapped {len(new_records)} DLL(s). State: {state_path}", "OK")
    print(f"\nTo undo:  dlss5-proton dlls {args.target!r} restore")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dlss5-proton",
        description="Install the RenoDX DLSS5 ReShade add-on into Proton games.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_target(sp, with_payload: bool = False):
        sp.add_argument("target", help="appid, game-name substring, folder, or .exe")
        sp.add_argument("--prefix", help="Wine prefix (the 'pfx' directory)")
        if with_payload:
            sp.add_argument("--zip", help="DLSS 5 ZIP containing nvngx_dlssnr.dll")
            sp.add_argument("--addon", help=f"path to {ADDON_NAME}")
            sp.add_argument("--mode", default="optiscaler",
                            choices=["optiscaler", "minimal", "full", "feeder"],
                            help="optiscaler (default): OptiScaler + DLSS NR, no "
                                 "ReShade. minimal/full/feeder use the ReShade + "
                                 "RenoDX add-on path")
            sp.add_argument("--full", action="store_true",
                            help="alias for --mode full")
            sp.add_argument("--optiscaler-zip",
                            help="pin this game to a specific OptiScaler "
                                 "archive instead of the built-in version. "
                                 "Upstream ships several times a day and a "
                                 "build that fixes one title can hang another")
            sp.add_argument("--feeder-zip",
                            help="pin this game to a specific DLSS5-Feeder "
                                 "archive instead of the built-in version. "
                                 "Upstream is beta-heavy and ships several "
                                 "times a day")
            sp.add_argument("--loader", default="dxgi.dll",
                            choices=sorted(set(LOADER_NAMES) | set(OPTISCALER_PROXY_NAMES)),
                            help="ReShade proxy DLL name (default: dxgi.dll). Use "
                                 "dinput8.dll to keep ReShade out of the DXGI "
                                 "chain in Streamline games")
            sp.add_argument("--feeder", action="store_true",
                            help="alias for --mode feeder: synthesises a DLAA "
                                 "contract from ReShade depth + motion vectors so "
                                 "games with NO DLSS (incl. D3D11/Unity) can work")
            sp.add_argument("--nr-runtime",
                            help="use a specific nvngx_dlssnr.dll instead of the "
                                 "one in the ZIP (e.g. a custom DLSSNR build)")
            sp.add_argument("--no-mv-provider", action="store_true",
                            help="do not install a motion-vector provider. On D3D11 "
                                 "games the feeder must share the MV texture across "
                                 "its private D3D12 device, which can fail; without "
                                 "a provider it runs with zero motion vectors "
                                 "(sharp when still, smears when moving)")
            sp.add_argument("--mv-provider",
                            help="path to a motion-vector provider (.fx file or a "
                                 "folder of .fx/.fxh), e.g. ReshadeMotionEstimation")
            sp.add_argument("--dlss-dest", default="auto",
                            choices=["auto", "game", "system32", "both"],
                            help="where DLSS runtime DLLs go (default: auto -- "
                                 "beside the exe plus the game's own DLSS folder)")

    scan_parser = sub.add_parser("scan", help="list installed games and their DLSS status")
    scan_parser.add_argument("--dlss-only", action="store_true",
                             help="only show games with a DLSS runtime")
    scan_parser.set_defaults(func=do_scan)

    check_parser = sub.add_parser("check", help="dry-run compatibility scan")
    add_target(check_parser, with_payload=True)
    check_parser.set_defaults(func=do_check)

    install_parser = sub.add_parser("install", help="install ReShade + the add-on + DLSS 5")
    add_target(install_parser, with_payload=True)
    install_parser.add_argument("--skip-d3dcompiler", action="store_true",
                                help="do not run protontricks d3dcompiler_47")
    install_parser.add_argument("-y", "--yes", action="store_true",
                                help="do not prompt")
    install_parser.set_defaults(func=do_install)

    restore_parser = sub.add_parser("restore", help="undo an install")
    add_target(restore_parser)
    restore_parser.add_argument("--force", action="store_true",
                                help="restore over files changed since install")
    restore_parser.add_argument("-y", "--yes", action="store_true")
    restore_parser.set_defaults(func=do_restore)

    addon_parser = sub.add_parser("addon",
                                  help="enable/disable just the add-on (bisect aid)")
    addon_parser.add_argument("target", help="appid, game-name substring, folder, or .exe")
    addon_parser.add_argument("state", choices=["on", "off", "status"])
    addon_parser.add_argument("--prefix", help="Wine prefix (the 'pfx' directory)")
    addon_parser.set_defaults(func=do_addon)

    loader_parser = sub.add_parser("loader",
                                   help="enable/disable the ReShade loader (baseline control)")
    loader_parser.add_argument("target", help="appid, game-name substring, folder, or .exe")
    loader_parser.add_argument("state", choices=["on", "off", "status"])
    loader_parser.add_argument("--prefix", help="Wine prefix (the 'pfx' directory)")
    loader_parser.set_defaults(func=do_loader)

    dlls_parser = sub.add_parser(
        "dlls",
        help="swap the game's own DLSS SR/RR/FG runtimes for newer ones")
    dlls_parser.add_argument("target",
                             help="appid, game-name substring, folder, or .exe")
    dlls_parser.add_argument("action", nargs="?", default="status",
                             choices=["status", "install", "restore"],
                             help="default: status")
    dlls_parser.add_argument("--prefix", help="Wine prefix (the 'pfx' directory)")
    dlls_parser.add_argument("--from", dest="source",
                             help="ZIP or folder of DLSS runtimes (nested ZIPs "
                                  "are unpacked). Default: the newest of each "
                                  "DLL across the archives next to the tool")
    dlls_parser.add_argument("--only",
                             help="comma list of components: sr, rr, fg")
    dlls_parser.add_argument("--streamline", action="store_true",
                             help="also swap sl.*.dll. Off by default: the "
                                  "interposer is version-matched to the game's "
                                  "own integration and swapping it breaks some "
                                  "titles")
    dlls_parser.add_argument("--force", action="store_true",
                             help="swap even when it would downgrade, and "
                                  "restore over files changed since the swap")
    dlls_parser.add_argument("-y", "--yes", action="store_true",
                             help="do not prompt")
    dlls_parser.set_defaults(func=do_dlls)

    verify_parser = sub.add_parser("verify",
                                   help="read the logs and say whether it actually worked")
    add_target(verify_parser)
    verify_parser.set_defaults(func=do_verify)

    status_parser = sub.add_parser("status", help="show what is installed")
    add_target(status_parser)
    status_parser.set_defaults(func=do_status)

    lo_parser = sub.add_parser("launch-options",
                               help="print the Steam launch options to use")
    add_target(lo_parser)
    lo_parser.add_argument("--loader", default=None,
                           choices=sorted(set(LOADER_NAMES) | set(OPTISCALER_PROXY_NAMES)),
                           help="defaults to whatever is actually installed")
    lo_parser.set_defaults(func=do_launch_options)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Fail as error:
        log(str(error), "ERROR")
        return 2
    except KeyboardInterrupt:
        log("Interrupted.", "WARN")
        return 130


if __name__ == "__main__":
    sys.exit(main())
