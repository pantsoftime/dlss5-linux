"""verify = upstream diagnose.analyse() + the Proton checks it cannot know about."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from core import diagnose, installer
from . import proton

BENIGN = ("queryNvapi", "XeSSFeature::LogCallback", "streamlineLogCallback",
          "NGX Updater not available",
          # The RenoDX add-on tries to hook NVSDK_NGX_D3D12_EvaluateFeature_C,
          # which no NGX core on Linux exports (prefix or driver, verified);
          # it falls back to the plain entry and NR runs. Baseline, not a fault.
          "EvaluateFeature_C", "libvorbisfile", "reshade-shaders\\Shaders")


def _tail(p: Path, n: int = 8_000_000) -> str:
    try:
        return p.read_text(encoding="utf8", errors="replace")[-n:]
    except OSError:
        return ""


def run(g) -> list[tuple[str, str, str]]:
    """[(level, title, detail)] -- upstream findings first, then ours."""
    root = g.install_dir
    out: list[tuple[str, str, str]] = []
    rep = diagnose.analyse(root)
    out.append(("INFO", "route", rep.route or "(no manifest)"))
    if not rep.route and any((root / n).is_file() for n in ("ReShade.log", "OptiScaler.log", "dlss5-feed.log")):
        out.append(("WARN", "stale logs", "no in-process payload is installed here; the add-on lines below come from logs "
                    "an earlier route left behind (restore keeps them). The vk layer section, if any, is current."))
    # Nothing of ours is in this folder and the launch line carries the VK layer:
    # the route is out of process. Upstream's analysis only knows in-folder
    # routes, so with no manifest and no log it concludes "not started since the
    # install" and suggests another proxy name -- advice for a route that is not
    # in use. Say what the route is and leave the layer's own section to report.
    from . import vklayer as _vk
    try:
        _cur = proton.launcher_overrides(g) if proton.is_xlcore(g) else proton.current_launch_options(g)
    except Exception:
        _cur = ""
    oop = not rep.route and _vk.enabled_in(_cur) and not any(
        (root / n).is_file() for n in ("ReShade.log", "OptiScaler.log", "dlss5-feed.log"))
    if oop:
        out[-1] = ("INFO", "route", "vk layer (out of process: nothing is installed in the game folder)")
    seen: set[tuple[str, str]] = set()
    for f in ([] if oop else rep.findings):    # upstream re-registers per device; dedupe
        key = (f.title, f.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append((f.level.upper(), f.title, f.detail))
    if rep.verdict and not oop:
        out.append(("INFO", "verdict", rep.verdict))

    # --- Proton -----------------------------------------------------------
    man = diagnose._manifest(root)
    proxy = man.get("proxy") or ""
    xl = proton.is_xlcore(g)
    cur = proton.launcher_overrides(g) if xl else proton.current_launch_options(g)
    if proxy and proxy != "(vulkan layer)":
        need = proton.override_entries(g, proxy, rep.route)
        missing = [e for e in need if not proton.override_present(cur, proxy, [e])]
        where = "xivlauncher-rb launcher.ini" if xl else "WINEDLLOVERRIDES"
        if not missing:
            out.append(("OK", where, ";".join(need) + " present"))
        elif missing == need:
            out.append(("BAD", where, f"missing -- Wine loads its builtin {proxy}; set: " +
                        (";".join(need) if xl else proton.launch_options(g, proxy, route=rep.route))))
        else:
            out.append(("BAD", where, f"incomplete -- add {';'.join(missing)}" +
                        (" (the D3D11 bridge needs vkd3d-proton's d3d12; without it: D3D12CreateDevice 80004002)"
                         if any(m.startswith("d3d12") for m in missing) else "")))
    pfx = proton.prefix_for(g)
    out.append(("OK" if pfx else "WARN", "Proton prefix", str(pfx) if pfx else "not found"))
    if pfx:
        out.append(("OK" if proton.ngx_bridge_present(g) else "BAD", "NGX bridge",
                    "_nvngx.dll in prefix system32" if proton.ngx_bridge_present(g) else
                    "no _nvngx.dll in system32 -- game never run under Proton, or driver NGX missing"))
    if proton.running(g):
        out.append(("WARN", "process", f"{g.exe.name} is running now"))

    # --- route-specific log readers ---------------------------------------
    since = diagnose._installed_at(root)
    if rep.route == "optiscaler":
        log = root / "OptiScaler.log"
        text = _tail(log)
        if text:
            if since and log.stat().st_mtime < since:
                out.append(("WARN", "OptiScaler.log", "older than the install -- launch once"))
            m = re.search(r"Upscaler support - fsr4: (\w+), dlss: (\w+)", text)
            if m:
                lvl = "OK" if m.group(2) == "true" else "WARN"
                out.append((lvl, "dlss capability", f"dlss: {m.group(2)}" + (
                    "" if m.group(2) == "true" else
                    " -- NVAPI init ran before nvapi64.dll loaded; OptiScaler will substitute XeSS for DLSS/DLAA (jaggies). Consider the native route.")))
            dl = len(re.findall(r"VK_ERROR_DEVICE_LOST", text))
            if dl:
                out.append(("BAD", "device lost", f"VK_ERROR_DEVICE_LOST x{dl} -- GPU fault; see FINDINGS.md (the v0.2.x deadlock)"))
            runs = re.findall(r"DLSS-NR running at (\d+x\d+)", text)
            out.append(("OK" if runs else "WARN", "NR dispatching",
                        f"{len(runs)} build(s), latest {runs[-1]}" if runs else "no 'DLSS-NR running at' -- enable NR (Insert)"))
            fwd = re.search(r"DLSS-NR forwarder loaded", text)
            out.append(("OK" if fwd else "WARN", "NR forwarder", "loaded" if fwd else "not loaded"))
            ver = re.findall(r"ReadVersion DLSS v([\d.]+) loaded", text)
            if ver:
                out.append(("INFO", "DLSS runtime", f"v{ver[-1]} (the game's own, as OptiScaler loaded it)"))
            # y4my4m writes "DLSS-NR cost: 5.30 ms total = ..."; the wilsjo2 line (0.7.7+) and
            # the upstream NR pull request write "DLSS-NR elapsed: 5.30 ms total, ...".
            costs = [float(c) for c in re.findall(r"DLSS-NR (?:cost|elapsed): ([\d.]+) ms total", text)]
            if costs:
                costs.sort()
                out.append(("INFO", "NR cost", f"{costs[0]:.1f} / {costs[len(costs)//2]:.1f} / {costs[-1]:.1f} ms "
                            f"(min / median / max over {len(costs)} samples)"))
            # D3D11 games: the dx11on12 bridge must land on vkd3d-proton's d3d12.
            if re.search(r"D3D12 interop device created", text):
                out.append(("OK", "D3D12 bridge", "interop device created on the D3D11 adapter"))
            elif re.search(r"GetD3D12DeviceFromD3D11 D3D12CreateDevice failed: 80004002", text):
                out.append(("BAD", "D3D12 bridge", "D3D12CreateDevice 80004002 -- Wine's builtin d3d12 got the DXVK "
                            "adapter; add d3d12=n,b;d3d12core=n,b to the overrides (umu launchers skip Proton's own)"))
            # Multipass on a game that keeps several DLSS features alive alternates
            # per dispatch (the pass gate is per command list) -- FFXIV shows it as flashing.
            handles = set(re.findall(r"HandleId: (\d+)", text))
            passes = re.search(r"(?m)^Passes\s*=\s*([2-9])", _tail(root / "OptiScaler.ini", 200_000))
            if passes and len(handles) > 1:
                out.append(("WARN", "multipass", f"Passes={passes.group(1)} with {len(handles)} DLSS features alive -- "
                            "the passes alternate between features (flashing); use Passes=1 here"))
            errs = [l for l in text.splitlines() if "] [E] " in l and not any(b in l for b in BENIGN)]
            out.append(("OK" if not errs else "WARN", "OptiScaler errors",
                        "none" if not errs else f"{len(errs)}: {errs[0][:110]}"))
    elif (man.get("components") or {}).get("nrlinux"):
        from . import nrlinux
        out.extend(nrlinux.verify(g))
    else:
        log = root / "ReShade.log"
        text = _tail(log)
        if text:
            if since and log.stat().st_mtime < since:
                out.append(("WARN", "ReShade.log", "older than the install -- launch once"))
            feats = re.findall(r"feature \d+ created .*?for NR input (\d+x\d+)", text)
            out.append(("OK" if feats else "WARN", "NR feature",
                        f"{len(feats)} creation(s), latest {feats[-1]}" if feats else "no NR feature created yet -- enable it in the add-on tab"))
            if "Streamline interposer not found" in text:
                out.append(("WARN", "Streamline", "interposer not seen as a loaded module (ShortFuse build limitation under Proton)"))
            if re.search(r"NR upscaling is not applicable", text):
                out.append(("INFO", "DLAA", "game already renders at output resolution; NR runs after native DLAA"))
            rt = re.search(r"signed DLSSNR (\S+) D3D12 runtime initialized", text)
            if rt:
                out.append(("OK", "NR runtime", f"DLSSNR {rt.group(1)} initialised"))
            errs = [l for l in text.splitlines() if "| ERROR" in l and not any(b in l for b in BENIGN)]
            out.append(("OK" if not errs else "WARN", "ReShade errors",
                        "none (EvaluateFeature_C hook miss is baseline on Linux)" if not errs else f"{len(errs)}: {errs[0][:110]}"))
        if rep.route == "feeder":
            feed = _tail(root / "dlss5-feed.log")
            if feed:
                # Every probe in the session, not just the last one: a probe reads 0% on a
                # still screen (menu, loading, a paused scene), so judging the provider by
                # the final sample calls a healthy run broken whenever the player stopped
                # moving before quitting. The provider works if it ever fed.
                mv = [int(x) for x in re.findall(r"MV probe .*?(\d+)% non-zero", feed)]
                if mv:
                    live = [x for x in mv if x >= 50]
                    if live:
                        tail = ("" if mv[-1] >= 50 else
                                f"; the last read {mv[-1]}%, which is what a still screen looks like")
                        out.append(("OK", "motion vectors",
                                    f"{len(live)} of {len(mv)} probes fed, peak {max(mv)}%{tail}"))
                    else:
                        out.append(("WARN", "motion vectors",
                                    f"{len(mv)} probe(s), none above 50% -- the provider is not feeding "
                                    "(ReshadeMotionEstimation does not compile on D3D12 here; use VORT)"))
                if re.search(r"CreateFeature raised", feed) and (g.api or "").upper() == "DX12":
                    out.append(("BAD", "feeder on D3D12", "the same-device create faults under Proton in most launches "
                                "(vkd3d-proton, via the DLSS cubin path); no config fixes it -- see the findings; "
                                "the DLSS5VKLayer route works on this class of game (verify --vklayer)"))
                probes = re.findall(r"Depth probe .*", feed)
                if probes and "FLAT" in probes[-1]:
                    out.append(("WARN", "depth", "the last depth probe was flat while the scene moved -- ReShade's Generic "
                                "Depth is on the wrong buffer (Add-ons tab > Generic Depth); DLSS and NR get no depth"))

    # --- the out-of-process route, when its token is in the launch options ---
    from . import vklayer
    if vklayer.enabled_in(cur) or (rep.route in (None, "", "(no manifest)") and vklayer.installed()):
        out.extend(vklayer.verify(g))
    return out
