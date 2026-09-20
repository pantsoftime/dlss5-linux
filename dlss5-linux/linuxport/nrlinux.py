"""The 'nrlinux' route: NapXDD/addon-dlssnr-linux as the neural add-on.

A ReShade add-on built on Linux (clang, MSVC ABI) that hooks the game's own
NGX D3D12 CreateFeature/EvaluateFeature, runs feature 18 once per frame off
the DLSS-SR output through a forwarder DLL (`nvngx.dll_nrfwd.dll`, the
snippet's caller gate) and composes the answer back before the game's
tone-mapping. It bypasses the driver's OTA "OutOfDate" gate that breaks the
closed renodx add-on under Proton, so it is the Linux-native alternative to
OptiScaler on a D3D12 game that ships DLSS.

Mechanically it is upstream's `native` route with a different add-on file:
ReShade as the proxy, nvngx_dlssnr.dll beside the exe, no OptiScaler. So the
install runs the core installer's native route with the staged add-on as the
"local renodx build", then renames the file to its own name and drops the
forwarder next to it, and the manifest is amended so uninstall removes both.

Staging: DLSS5_work/addon-dlssnr-linux/<version>/{dlssnr-linux.addon64,
nvngx.dll_nrfwd.dll} (a GitHub release, sha256 recorded in the scan notes).

Known upstream caveats (v0.2.2, 2026-09-20):
  * D3D12 + in-game DLSS Super Resolution only (the pass runs off DLSS-SR).
  * The hooks attach on a later Present: a game that creates DLSS before
    that never starts the pass until DLSS is re-created (Control, #7) --
    change the DLSS quality once in the graphics menu.
  * Only the e16bcf15 model build is tested; a mismatched model can report
    success and crash minutes in (README). RTX 40 needs the RTX40 re-target.
  * 007 First Light: Xid 31 MMU fault after the 2nd evaluate (#6, open).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import installer, dlss

ROUTE = "nrlinux"
ADDON = "dlssnr-linux.addon64"
FWD = "nvngx.dll_nrfwd.dll"
UPSTREAM = "https://github.com/NapXDD/addon-dlssnr-linux"
STAGE = Path(__file__).resolve().parents[2] / "addon-dlssnr-linux"
KNOWN_MODEL = "e16bcf15e16e13f527491cdf7845b2fe6521a738d8f7c9c721866a8496e1fc8e"
OVERLAY = 'DLSSNR Linux'


def _vkey(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name))


def staged(version: str | None = None) -> tuple[str, Path] | None:
    """(version, dir) of the newest staged release with both files, or None."""
    if not STAGE.is_dir():
        return None
    dirs = [d for d in STAGE.iterdir() if d.is_dir() and (d / ADDON).is_file() and (d / FWD).is_file()]
    if version:
        dirs = [d for d in dirs if d.name.lstrip("v") == version.lstrip("v")]
    if not dirs:
        return None
    d = max(dirs, key=lambda p: _vkey(p.name))
    return d.name, d


def fit(g, native_dlss: bool) -> tuple[bool, str]:
    if (g.api or "").upper() != "DX12":
        return False, "the add-on hooks NGX on a D3D12 device; this game is " + (g.api or "?")
    if g.bitness != 64:
        return False, "64-bit only"
    if not native_dlss:
        return False, "the pass runs off the game's own DLSS Super Resolution; this game ships none"
    return True, ""


def installed(root: Path) -> str | None:
    try:
        data = json.loads((root / installer.MANIFEST).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
    return (data.get("components") or {}).get("nrlinux")


def options(args, s) -> installer.Options:
    st = staged(getattr(args, "nrlinux_version", None))
    if not st:
        raise SystemExit(f"no staged addon-dlssnr-linux release under {STAGE} "
                         f"(download {ADDON} and {FWD} from {UPSTREAM}/releases into {STAGE}/<version>/)")
    _, d = st
    return installer.Options(
        path=dlss.NATIVE, native_dlss=s.native_dlss, upscaler=s.upscaler,
        renodx_local=d / ADDON,
        dlssnr=args.dlssnr, dlss=args.dlss,
        keep_game_dlss=not args.no_keep_game_dlss,
        reshade_proxy=args.proxy or "", opti_proxy=args.proxy or "",
    )


def finish(g, rep, log=print) -> str:
    """After the core native install: give the add-on its own name, add the
    forwarder, and record both so uninstall takes them out again."""
    ver, d = staged()
    root = g.install_dir
    written = list(rep.written)
    rel_addon = next((w for w in written if Path(w).name.lower() == installer.RENODX.lower()), None)
    if rel_addon is None:
        raise installer.InstallError("the native install did not place an add-on file")
    src = root / rel_addon
    dst = src.with_name(ADDON)
    if dst.exists():
        dst.unlink()
    src.rename(dst)
    fwd = dst.with_name(FWD)
    fwd.write_bytes((d / FWD).read_bytes())
    log(f"      {rel_addon} -> {dst.relative_to(root)}  (addon-dlssnr-linux {ver})")
    log(f"      {fwd.relative_to(root)}  (the forwarder the snippet's caller gate needs)")
    # ReShade.ini beside the add-on: the collapsed-overlay key names the old file.
    ini = dst.with_name("ReShade.ini")
    if ini.is_file():
        t = ini.read_text(encoding="utf8", errors="replace")
        t2 = t.replace(f"DLSS 5 Neural Rendering@{installer.RENODX}", f"{OVERLAY}@{ADDON}")
        if t2 != t:
            ini.write_text(t2, encoding="utf8")
    man = root / installer.MANIFEST
    try:
        data = json.loads(man.read_text(encoding="utf8"))
    except (OSError, ValueError):
        data = {}
    files = [str(dst.relative_to(root)) if w == rel_addon else w for w in data.get("files", written)]
    files.append(str(fwd.relative_to(root)))
    data["files"] = files
    data.setdefault("components", {})["nrlinux"] = ver
    data["components"].pop("renodx", None)
    data["variant"] = ROUTE
    data.setdefault("notes", []).append(f"addon-dlssnr-linux {ver} in place of renodx-dlss5 ({UPSTREAM})")
    man.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    return ver


_RX = {
    "registered": re.compile(r'Registered add-on "DLSSNR Linux"'),
    "loaded": re.compile(r"DLSSNR Linux loaded \(([^)]*)\)"),
    "swapchain": re.compile(r"swapchain created: (\d+x\d+) format=(\d+) color_space=(\d+) \[([^\]]+)\]"),
    "hooked": re.compile(r"ngx-probe: hooked (\d+) NGX exports on (\S+)"),
    "probe_eval": re.compile(r"ngx-probe: D3D12_EvaluateFeature #(\d+)"),
    "model": re.compile(r"nr-fwd: model nvngx_dlssnr\.dll -- version ([^,]+), sha256=([0-9a-fA-F]+)"),
    "init": re.compile(r"nr-fwd: snippet init => (0x[0-9a-fA-F]+)"),
    "create": re.compile(r"nr-fwd: CreateFeature\(18\) => (0x[0-9a-fA-F]+)(?: \(([^)]*)\))?"),
    "eval": re.compile(r"nr-fwd: EvaluateFeature #(\d+) => (0x[0-9a-fA-F]+)"),
    "giveup": re.compile(r"nr-fwd: giving up[^\n]*"),
    "pass": re.compile(r"nr-fwd: pass (ENABLED|DISABLED)"),
}


def verify(g) -> list[tuple[str, str, str]]:
    root = g.install_dir
    out: list[tuple[str, str, str]] = []
    ver = installed(root)
    out.append(("INFO", "nrlinux", f"addon-dlssnr-linux {ver or '?'} ({UPSTREAM})"))
    exe_dir = g.exe.parent if g.exe else root
    addon = exe_dir / ADDON
    fwd = exe_dir / FWD
    out.append(("OK" if addon.is_file() and fwd.is_file() else "BAD", "files",
                f"{ADDON} + {FWD} beside {g.exe.name if g.exe else 'the exe'}" if addon.is_file() and fwd.is_file()
                else f"missing: {', '.join(n for n, p in ((ADDON, addon), (FWD, fwd)) if not p.is_file())}"))
    log = exe_dir / "ReShade.log"
    if not log.is_file():
        log = root / "ReShade.log"
    if not log.is_file():
        out.append(("WARN", "ReShade.log", "not written yet -- launch once"))
        return out
    text = log.read_text(encoding="utf8", errors="replace")[-8_000_000:]
    if not _RX["registered"].search(text):
        out.append(("BAD", "add-on", "ReShade did not register 'DLSSNR Linux' -- dxgi override missing, or the add-on is not beside the exe"))
        return out
    m = _RX["loaded"].search(text)
    out.append(("OK", "add-on", "registered" + (f" ({m.group(1)})" if m else "")))
    sw = _RX["swapchain"].findall(text)
    if sw:
        out.append(("INFO", "swapchain", f"{sw[-1][0]} {sw[-1][3]}"))
    hk = _RX["hooked"].findall(text)
    out.append(("OK" if hk else "WARN", "NGX hooks",
                f"{hk[-1][0]} exports on {hk[-1][1]}" if hk else "not attached yet (they land on a later Present)"))
    mo = _RX["model"].findall(text)
    if mo:
        v, h = mo[-1]
        known = h.lower() == KNOWN_MODEL
        out.append(("OK" if known else "WARN", "model",
                    f"nvngx_dlssnr {v} sha256 {h[:16]}..." + ("" if known else " -- not the build the add-on is tested with (e16bcf15); a mismatch can pass and crash minutes in")))
    ini = _RX["init"].findall(text)
    if ini:
        out.append(("OK" if ini[-1].lower() == "0x1" else "BAD", "snippet init",
                    ini[-1] + ("" if ini[-1].lower() == "0x1" else " -- NVAPI/NGX not reachable or the model DLL is missing")))
    cr = _RX["create"].findall(text)
    if cr:
        code, why = cr[-1]
        ok = code.lower() == "0x1"
        out.append(("OK" if ok else "BAD", "CreateFeature(18)", f"{len(cr)} create(s), last {code}" + (f" ({why})" if why else "")))
    ev = _RX["eval"].findall(text)
    good = sum(1 for _, c in ev if c.lower() == "0x1")
    if ev:
        out.append(("OK" if good else "BAD", "evaluates", f"{good} of {len(ev)} succeeded (last #{ev[-1][0]} -> {ev[-1][1]})"))
    elif _RX["probe_eval"].search(text) and not cr:
        out.append(("WARN", "NR pass", "DLSS-SR evaluates seen but no NR feature: the game created DLSS before the hooks "
                    "attached (upstream #7) -- change the DLSS quality once in the graphics menu to re-create it"))
    elif not cr:
        out.append(("WARN", "NR pass", "no create yet -- is DLSS Super Resolution on in the game?"))
    gu = _RX["giveup"].search(text)
    if gu:
        out.append(("BAD", "nr-fwd", gu.group(0)[:140]))
    ps = _RX["pass"].findall(text)
    if ps:
        out.append(("INFO", "pass", ps[-1].lower() + " (F10 toggles)"))
    return out


def suggestion(g, native_dlss: bool) -> str | None:
    ok, _ = fit(g, native_dlss)
    st = staged()
    if not ok or not st:
        return None
    if installed(g.install_dir):
        return f"installed ({st[0]}): verify reads its nr-fwd lines"
    return (f"addon-dlssnr-linux {st[0]} staged -- the Linux-native ReShade add-on route: "
            f"install --route nrlinux (experimental; uninstall the current route first)")
