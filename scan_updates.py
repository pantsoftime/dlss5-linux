#!/usr/bin/env python3
"""Daily upstream check for the DLSS5/Proton work.

These projects ship several times a day, and a build that fixes one game can
hang another -- see FINDINGS.md. This only reports; it changes
nothing. Try a new build on ONE game first:

    dlss5_proton.py install <appid> --optiscaler-zip <archive>

Usage:  ./scan_updates.py [--notes] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/{}"
TOOL = Path(__file__).resolve().parent / "proton-tool" / "dlss5_proton.py"

# repo, label, why we watch it
REPOS = [
    ("Dagherbou/OptiScaler_DLSSNR", "OptiScaler_DLSSNR",
     "PRIMARY -- the injector + NR runner"),
    ("jlrouzies-fr/DLSS5-Feeder", "DLSS5-Feeder",
     "feeder mode (Dreamfall)"),
    ("RankFTW/RHI", "RHI",
     "ReShade HDR Installer -- proxy-conflict + per-game override logic"),
    ("clshortfuse/renodx", "RenoDX",
     "nightly shaders/fixes; NOT a source of the NR add-on"),
    ("optiscaler/OptiScaler", "OptiScaler (parent)",
     "tells us which changes are fork-specific"),
    ("NVIDIA/DLSS", "NVIDIA DLSS SDK",
     "public baseline vs our 310.9.0 archive"),
    ("NVIDIA-RTX/Streamline", "NVIDIA Streamline SDK",
     "public baseline; watch for sl.dlss_nr"),
    # --- found via DLSS5-Autopilot's own source list (2026-09-05) ---
    ("Kizzuwatnaa/DLSS5-Autopilot", "DLSS5-Autopilot",
     "Windows meta-installer; its README is the best route/maturity map"),
    ("NIGos/dlss5-bridge", "dlss5-bridge",
     "'bridge' route -- Vulkan games with DLSS, private D3D12 session"),
    ("matiasLombo/neural-upstream", "neural-upstream",
     "'neural-upstream' route -- model runs BEFORE the game's upscale"),
    ("kibblerz/DLSS5-Reshade-AIO", "DLSS5-Reshade-AIO",
     "'standalone-dlssnr' route -- own feed + DLAA/SR + FRAME GEN"),
    ("umar-afzaal/LumeniteFX", "LumeniteFX",
     "motion-vector shader the feeder route pairs with (Kernel 2.0)"),
    ("lunks/dxvk-remix-plus-dlssnr", "dxvk-remix-plus-dlssnr",
     "'remix' route -- DLSS 5 inside an RTX Remix runtime"),
    # --- found via the RenoDX Discord FFXIV thread (2026-09-06) ---
    ("y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG", "OptiScaler y4my4m fork",
     "Dagherbou fork + NR multipass, MFG x6 on RTX40, LINUX/PROTON FIXES "
     "(author runs GE-Proton 11-6); builds are on OptiScaler master (v10-dev)"),
    ("SirenBrink/OptiScaler_DLSSNR_Multipass_MFG_FFXIV", "OptiScaler FFXIV fork",
     "y4my4m fork + ForcePerfQuality for FFXIV's locked DRS; branch force-perf-quality"),
    ("wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass", "OptiScaler PreSR fork",
     "NR before the upscaler (pre-SR) + 1-3 passes; releases have zips"),
    # --- Proton-side pieces the FF7 Remake feeder fault lives in (2026-09-06) ---
    # --- assessed 2026-09-11: the out-of-process Vulkan-layer route (Remake works on it) ---
    ("bmitch87/DLSS5VKLayer", "DLSS5VKLayer",
     "Linux Vulkan layer + Wine helper; the only route for native games and D3D12-without-DLSS"),
    # --- surfaced 2026-09-20 (feeder #121): Linux-built ReShade add-on driving feature 18 directly ---
    ("NapXDD/addon-dlssnr-linux", "addon-dlssnr-linux",
     "GPL ReShade add-on for D3D12+DLSS games under Proton; forwarder bypasses the OTA 'OutOfDate' gate; fallback where OptiScaler faults"),
    # --- surfaced 2026-09-24 (VKLayer #26): native Vulkan reimplementation of the NR network ---
    ("maanHimself/OpenDLSS-NR", "OpenDLSS-NR",
     "MIT Vulkan reimplementation of DLSS-NR 310.8 (bit-exact claim, user-supplied weights); DLSS5VKLayer's maintainer is considering it to drop the Wine helper"),
    ("jp7677/dxvk-nvapi", "dxvk-nvapi",
     "NVAPI D3D12 cubin path faults on the feeder's same-device create (FINDINGS.md)"),
    ("HansKristian-Work/vkd3d-proton", "vkd3d-proton",
     "where that fault lands (this=0xF in d3d12core); also the D3D11->D3D12 bridge OptiScaler uses for FFXIV"),
]

ASSET_HINTS = ("optiscaler", "feeder", "dlssnr")

BOLD, DIM, YELLOW, RESET = "\033[1m", "\033[2m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = YELLOW = RESET = ""


def fetch(url: str):
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "dlss5-scan"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
        return {"_error": str(error)}


def pinned(name: str) -> str:
    """Read a pinned constant out of the tool so drift is visible."""
    try:
        text = TOOL.read_text(encoding="utf-8")
    except OSError:
        return "?"
    match = re.search(rf'^{name} = "([^"]+)"', text, re.M)
    return match.group(1) if match else "?"


def releases(repo: str, limit: int = 4) -> list[dict]:
    data = fetch(API.format(repo) + f"/releases?per_page={limit}")
    if isinstance(data, dict):
        return []
    return data


def report(show_notes: bool) -> dict:
    summary: dict[str, list[dict]] = {}
    print(f"\n{BOLD}Upstream check{RESET}")
    print(f"  pinned: OptiScaler {pinned('OPTISCALER_VERSION')}   "
          f"Feeder {pinned('FEEDER_VERSION')}   "
          f"ReShade {pinned('RESHADE_VERSION')}")
    print(f"  {YELLOW}OptiScaler default = y4my4m fork nightly 2026-09-06 (7b7220bb), proven on "
          f"FF7R/007/Horizon; Dagherbou v0.1.2 is the fallback. The 'nightly' tag rolls "
          f"daily -- a newer one is a candidate, not an upgrade, until tested on one game{RESET}")

    for repo, label, why in REPOS:
        print(f"\n{BOLD}{label}{RESET}  {DIM}{repo}{RESET}")
        print(f"  {DIM}{why}{RESET}")
        rs = releases(repo)
        summary[repo] = [{"tag": r["tag_name"], "date": r["published_at"][:10],
                          "prerelease": r["prerelease"]} for r in rs]
        if not rs:
            print("    (no releases -- ReShade and some others publish off-GitHub)")
            continue
        for r in rs:
            # The feeder marks some -beta tags prerelease=False, so this flag
            # alone never decides what is stable.
            flag = " [pre]" if r["prerelease"] else ""
            name = (r.get("name") or "")[:44]
            print(f"    {r['tag_name']:<26} {r['published_at'][:10]}{flag}  {name}")
            for asset in r.get("assets", [])[:8]:
                lower = asset["name"].lower()
                if lower.endswith(".zip") and any(h in lower for h in ASSET_HINTS):
                    print(f"        asset: {asset['name']}  ({asset['size']:,} b)")
        if show_notes:
            body = (rs[0].get("body") or "").strip()
            if body:
                print("    --- newest notes:")
                for line in body[:900].splitlines()[:16]:
                    print(f"      {line[:100]}")

    print(f"\n{BOLD}Open issues on the primary repo{RESET}")
    issues = fetch(API.format("Dagherbou/OptiScaler_DLSSNR")
                   + "/issues?state=open&per_page=12")
    if isinstance(issues, dict):
        print(f"    {issues.get('_error') or issues.get('message', '?')}")
    else:
        for issue in issues:
            if "pull_request" in issue:
                continue
            print(f"  #{issue['number']:<4} {issue['created_at'][:10]}  "
                  f"{issue['title'][:76]}")
    print()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", action="store_true",
                        help="print the newest release body for each repo")
    parser.add_argument("--json", action="store_true",
                        help="also dump a machine-readable summary")
    args = parser.parse_args()
    summary = report(args.notes)
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
