"""Version selection for OptiScaler -- a DEFAULT, not a clamp.

Upstream's optiscaler.resolve() always returns GitHub's latest Dagherbou
release. Our default is instead the build proven under Proton on this machine:
the y4my4my4m fork nightly of 2026-09-06 (7b7220bb), kept as a local zip in
DLSS5_work/ because upstream's "nightly" tag rolls daily. Dagherbou v0.1.2 is
the fallback if that file is missing. Every install can pick another:

    set_optiscaler("default")      the local nightly zip (DEFAULT_ZIP), else FALLBACK_TAG
    set_optiscaler("fallback")     Dagherbou v0.1.2 (FALLBACK_TAG)
    set_optiscaler("latest")       whatever Dagherbou publishes now
    set_optiscaler("v0.2.0-patch1")  any Dagherbou release tag
    set_optiscaler("/path/to.zip") a local archive (staged into the cache)

A tag is resolved through sources._json (cached, rate-limit tolerant), so
testing a fresh upstream build is one flag, not a code change.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from core import net, optiscaler as _opti, sources

WORK = Path(__file__).resolve().parents[2]            # DLSS5_work/
DEFAULT_TAG = "y4my4m-nightly-20260906"               # label recorded in the manifest
DEFAULT_ZIP = WORK / "OptiScaler_v10.0.0-pre1_20260906_y4my4m-nightly.zip"
FALLBACK_TAG = "v0.1.2-dIssnr"         # Dagherbou v0.1.2; capital-I typo is upstream's
RELEASES = "https://api.github.com/repos/Dagherbou/OptiScaler_DLSSNR/releases"

_choice = "default"
_orig_resolve = _opti.resolve


def set_optiscaler(choice: str) -> None:
    global _choice
    _choice = choice or "default"


def _by_tag(tag: str) -> tuple[str, str]:
    rel = sources._json(f"{RELEASES}/tags/{tag}")
    for a in rel.get("assets", []):
        if a["name"].lower().endswith(".zip"):
            return rel.get("tag_name", tag), a["browser_download_url"]
    raise RuntimeError(f"OptiScaler release {tag} has no .zip asset.")


def _local(p: Path, tag: str) -> tuple[str, str]:
    # Stage the file in the cache under the name the installer will ask
    # for, preserving the extension (.7z nightlies, .zip releases) so
    # net.download()'s magic check validates the right format.
    ext = p.suffix.lower() if p.suffix.lower() in (".7z", ".zip") else ".zip"
    net.cache_dir().mkdir(parents=True, exist_ok=True)
    dest = net.cache_dir() / f"OptiScaler-DLSSNR-{tag}{ext}"
    if not dest.is_file() or dest.stat().st_size != p.stat().st_size:
        shutil.copy2(p, dest)
    return tag, f"file://{dest}"


def resolve(build: str = "") -> tuple[str, str]:
    if build:
        return _orig_resolve(build)
    c = _choice
    if c == "latest":
        return _orig_resolve()
    if c == "fallback":
        return _by_tag(FALLBACK_TAG)
    if c == "default":
        if DEFAULT_ZIP.is_file():
            return _local(DEFAULT_ZIP, DEFAULT_TAG)
        print(f"  !! default OptiScaler archive missing ({DEFAULT_ZIP.name}); "
              f"falling back to Dagherbou {FALLBACK_TAG}")
        return _by_tag(FALLBACK_TAG)
    p = Path(c).expanduser()
    if p.is_file():
        return _local(p, f"local-{net.sha256(p)[:8]}")
    return _by_tag(c)


def install() -> None:
    _opti.resolve = resolve
