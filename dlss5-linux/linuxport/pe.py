"""Widen core.pe's DX12 promotion to engine plugin folders.

Upstream only checks the exe's own folder for an Agility SDK or DLSS-G/RR.
Unreal keeps nvngx_dlssg.dll / nvngx_dlssd.dll under
Plugins/Runtime/Nvidia/**, so Stellar Blade came back "DX11". Under Proton
every DXGI game is vkd3d/dxvk anyway; what matters is the route list, and
native/upstream/optiscaler need the DX12 label.
"""
from __future__ import annotations

from pathlib import Path

from core import dlss, pe as _pe

_orig = _pe._has_d3d12_agility_sdk


# Directory names that say "still inside one game's tree". The walk-up stops
# the moment neither the current name nor the parent's children look like
# that, so a non-Steam layout such as Games/Publisher/Game/game/ never climbs
# into the publisher folder and finds ANOTHER game's DLSS-G (which is exactly
# how Final Fantasy XIV came back "DX12" on 2026-09-07).
_TREE = {"binaries", "engine", "content", "game", "boot", "bin", "win64", "x64", "retail"}


def _inside_game_tree(d: Path) -> bool:
    if d.name.lower() in _TREE:
        return True
    try:
        return any(c.is_dir() and c.name.lower() in _TREE for c in d.iterdir())
    except OSError:
        return False


def _has_d3d12_agility_sdk(folder: Path, bits: int | None = None) -> bool:
    try:
        if _orig(folder) if _orig.__code__.co_argcount == 1 else _orig(folder, bits):
            return True
    except TypeError:
        if _orig(folder):
            return True
    # Walk up to the game root (bounded) and look where engines keep DLSS.
    root = folder
    for _ in range(4):
        if (root / "steamapps").is_dir() or root.parent == root:
            break
        found = dlss.find_dlss_files(root, names=("nvngx_dlssg.dll", "nvngx_dlssd.dll"))
        if found:
            return True
        if not _inside_game_tree(root.parent) or (root.parent / "steamapps").is_dir():
            break
        root = root.parent
    return False


def install() -> None:
    _pe._has_d3d12_agility_sdk = _has_d3d12_agility_sdk
    install_api()


# --- engines that load their renderer dynamically -----------------------------
# Unity imports only opengl32.dll statically and picks D3D11 at run time via
# UnityPlayer.dll; RE Engine never stores the literal "d3d12.dll". Both came
# out of our own tool (dlss5_proton.looks_like_d3d11 / looks_like_d3d12).
_orig_detect_api = _pe.detect_api
_D3D12_NEEDLES = (b"d3d12core.dll", b"d3d12createdevice", b"d3d12sdkversion", b"agilitysdk")


def _contains(path: Path, needle: bytes) -> bool:
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                if needle in chunk.lower():
                    return True
    except OSError:
        pass
    return False


# Executables whose static import table says one thing and whose renderer does
# another. Each entry is a measured fact, not a guess: the feeder logged a
# "same-device D3D12 session" for Remake, and vkd3d-proton created its swapchain.
_KNOWN_API = {
    "ff7remake_.exe": ("DX12", "UE4 title that imports d3d11.dll statically but renders D3D12 (feeder and vkd3d-proton logs)"),
}


def detect_api(path: Path) -> tuple[str, str]:
    known = _KNOWN_API.get(path.name.lower())
    if known:
        return known
    # Unity first: its player embeds D3D12 symbols too, so the needle scan
    # below would mislabel every Unity title as DX12.
    if (path.parent / "UnityPlayer.dll").is_file() or (path.parent / (path.stem + "_Data")).is_dir():
        return "DX11", "Unity (UnityPlayer.dll / *_Data beside the exe): D3D11 at run time"
    api, why = _orig_detect_api(path)
    if api in ("OpenGL", "DX9", "Unknown", "?"):
        for n in _D3D12_NEEDLES:
            if _contains(path, n):
                return "DX12", f"{n.decode()!r} in the executable (renderer loaded dynamically)"
    return api, why


def install_api() -> None:
    _pe.detect_api = detect_api
