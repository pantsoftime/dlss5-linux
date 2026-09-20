#!/usr/bin/env python3
"""DLSS 5 on Proton -- Linux port of DLSS5-Autopilot's engine.

    recommend | install | uninstall | verify | launch-options | dlls   <game>

    ./dlss5_linux.py "Stellar Blade"        name substring, Steam appid, folder or .exe
    ./dlss5_linux.py --scan                 every Steam game with its recommendation
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linuxport  # noqa: E402
linuxport.activate()
from core import dlss, games, gpu, installer, pe  # noqa: E402


def fill(g: games.Game) -> games.Game:
    if g.exe is None:
        exes = pe.find_game_exes(g.folder)
        g.exe = exes[0] if exes else None
        g.candidates = exes
    if g.exe:
        g.bitness = pe.exe_bitness(g.exe)
        g.api, g.api_why = pe.detect_api(g.exe)
    return g


def find(spec: str, prefix: str | None = None) -> games.Game:
    """Path, Steam appid, exact name, or an unambiguous name substring."""
    g = _find(spec)
    if prefix:
        from linuxport import proton
        proton.set_prefix(g, Path(prefix))
    return g


_GENERIC = {"game", "bin", "binaries", "win64", "x64", "retail", "end", "content"}


def _find(spec: str) -> games.Game:
    p = Path(spec).expanduser()
    if p.exists():
        folder = p if p.is_dir() else p.parent
        from linuxport import games as lgames
        lgames.remember_folder(folder)
        # "game", "Win64", "Binaries"... say nothing; name the game after the folder above them.
        name = folder.name
        probe = folder
        while name.lower() in _GENERIC and probe.parent != probe:
            probe = probe.parent; name = probe.name
        return fill(games.Game(name=name, folder=folder, exe=p if p.is_file() else None, source="Manual"))
    if spec.isdigit():
        from linuxport.proton import pt
        g = pt.installed_games().get(spec)
        if not g:
            sys.exit(f"no Steam game with appid {spec}")
        return fill(games.Game(name=g.name, folder=g.install_dir, source="Steam"))
    low = spec.lower()
    hits = [g for g in games.scan_steam() if low in g.name.lower()]
    if not hits:
        sys.exit(f"no Steam game matches {spec!r}")
    exact = [g for g in hits if g.name.lower().rstrip("\u2122 ") == low]
    if exact:
        return fill(exact[0])
    if len(hits) > 1:
        names = "\n  ".join(f"{g.name}  ({proton_appid(g) or '-'})" for g in sorted(hits, key=lambda g: g.name))
        sys.exit(f"{spec!r} matches {len(hits)} games -- use the appid or a longer name:\n  {names}")
    return fill(hits[0])


def proton_appid(g) -> str | None:
    try:
        from linuxport import proton
        return proton.appid_for(g)
    except Exception:
        return None


def report(g: games.Game, sm: int | None) -> None:
    print(f"\n{g.name}\n{'-' * 60}")
    print(f"  exe       {g.exe.relative_to(g.folder) if g.exe else '?'}   x{g.bitness}  {g.api}  ({g.api_why})")
    ok, why = installer.check_supported(g)
    if not ok:
        print(f"  BLOCKED   {why}"); return
    s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness, sm=sm)
    print(f"  dlss      {'yes' if s.native_dlss else 'no'}   {', '.join(s.evidence[:3])}")
    print(f"  recommend {s.recommended}")
    print(f"  because   {s.reason[:300]}")
    print("  routes:")
    for r in s.options:
        lvl, _ = installer.reliability(g, r, "" if s.native_dlss else s.upscaler)
        usable, note = dlss.fit(r, g.api, s.native_dlss, sm, s.upscaler)
        mark = "*" if r == s.recommended else " "
        print(f"   {mark} {r:<11} {lvl:<12} {note[:70]}")
    opt = installer.Options(path=s.recommended, native_dlss=s.native_dlss, upscaler=s.upscaler)
    print(f"  plan      {' -> '.join(installer.plan(g, opt))}")
    from linuxport import vklayer, nrlinux
    hint = vklayer.suggestion(g, s.native_dlss)
    if hint:
        print(f"  vk layer  {hint}")
    hint = nrlinux.suggestion(g, s.native_dlss)
    if hint:
        print(f"  nrlinux   {hint}")


def options_from(args, s) -> installer.Options:
    from linuxport import pins
    pins.set_optiscaler(args.optiscaler)
    opt = installer.Options(
        path=args.route or s.recommended,
        native_dlss=s.native_dlss,
        upscaler=s.upscaler,
        renodx_local=Path(args.addon).expanduser() if args.addon else None,
        dlssnr=args.dlssnr, dlss=args.dlss,
        keep_game_dlss=not args.no_keep_game_dlss,
        reshade_proxy=args.proxy or "", opti_proxy=args.proxy or "",
        feeder_tag=args.feeder_tag or "", feeder_prerelease=bool(args.feeder_tag),
    )
    if getattr(args, "provider", None) is not None:
        opt.provider = args.provider
    return opt


def cmd_install_nrlinux(g, s, args) -> int:
    """addon-dlssnr-linux as the add-on: the core native route, then the file swap."""
    from linuxport import nrlinux, seed
    ok, why = nrlinux.fit(g, s.native_dlss)
    if not ok and not args.force_route:
        sys.exit(f"route nrlinux does not fit this game: {why}. Use --force-route to override.")
    if installer.MANIFEST and (g.install_dir / installer.MANIFEST).is_file() and not nrlinux.installed(g.install_dir):
        from core import diagnose
        cur = (diagnose._manifest(g.install_dir) or {}).get("path") or "?"
        sys.exit(f"this folder already carries the {cur!r} route -- run `uninstall` first so its files come out cleanly")
    opt = nrlinux.options(args, s)
    ver, _ = nrlinux.staged(getattr(args, "nrlinux_version", None))
    print(f"\n{g.name}  ->  route=nrlinux (core native route + addon-dlssnr-linux {ver})  proxy={opt.reshade_proxy or 'auto'}")
    pv = installer.preview(g, opt)
    for line in installer.preview_lines(pv):
        print("  " + line)
    from linuxport import components as _comp
    for line in _comp.advisory(g.install_dir):
        print("  " + line)
    if args.dry_run:
        return 0
    if pv.blockers:
        return 1
    if not args.yes and input("  proceed? [y/N] ").strip().lower() != "y":
        return 1
    seed.seed()
    rep = installer.install(
        g, opt,
        on_step=lambda i, n, name: print(f"  [{i+1}/{n}] {name}"),
        on_prog=lambda pct, msg: None,
        on_log=lambda m: print("   " + m))
    print("  [+] addon-dlssnr-linux")
    nrlinux.finish(g, rep, log=print)
    print(f"\n  done. Launch options: {proton_launch_hint(g)}")
    print("  In game: DLSS Super Resolution ON, then ReShade overlay (Home) > Add-ons > DLSSNR Linux; F10 A/Bs the pass.")
    return 0


def proton_launch_hint(g) -> str:
    from linuxport import proton
    try:
        return proton.launch_options(g, "dxgi.dll", route="native")
    except Exception:
        return 'WINEDLLOVERRIDES="dxgi=n,b" %command%'


def cmd_install(args) -> int:
    from linuxport import seed
    g = find(args.target, getattr(args, 'prefix', None))
    _, sm = gpu.detect()
    ok, why = installer.check_supported(g)
    if not ok:
        sys.exit(f"cannot install: {why}")
    from linuxport import proton
    if proton.running(g) and not args.dry_run:
        sys.exit(f"{g.exe.name} is running -- close it first")
    s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness, sm=sm)
    if args.route == "nrlinux":
        return cmd_install_nrlinux(g, s, args)
    if args.route and args.route not in s.options and not args.force_route:
        sys.exit(f"route {args.route!r} is not offered for this game ({g.api} x{g.bitness}); "
                 f"offered: {', '.join(s.options)}. Use --force-route to override.")
    opt = options_from(args, s)
    print(f"\n{g.name}  ->  route={opt.path}  proxy={opt.reshade_proxy or 'auto'}  "
          f"optiscaler={args.optiscaler}  addon={args.addon or 'catalog/auto'}")
    pv = installer.preview(g, opt)
    for line in installer.preview_lines(pv):
        print("  " + line)
    from linuxport import components
    for line in components.advisory(g.install_dir):
        print("  " + line)
    if args.dry_run:
        return 0
    if pv.blockers:
        return 1
    if not args.yes and input("  proceed? [y/N] ").strip().lower() != "y":
        return 1
    seed.seed()
    rep = installer.install(
        g, opt,
        on_step=lambda i, n, name: print(f"  [{i+1}/{n}] {name}"),
        on_prog=lambda pct, msg: None,
        on_log=lambda msg: print("   " + msg.rstrip()),
    )
    print(f"\n  wrote {len(rep.written)} file(s); {len(rep.warnings)} warning(s)")
    for w in rep.warnings:
        print("  !! " + w[:200])
    for n in rep.notes:
        print("  -- " + n[:200])
    proxy = installer._proxy_name(g.api, opt.reshade_proxy) if opt.path != "optiscaler" else (opt.opti_proxy or proton.installed_proxy(g) or "dxgi.dll")
    missing = proton.missing_overrides(g, proxy, opt.path)
    print(f"\n  launch configuration ({'already set' if not missing else 'SET THIS -- required: ' + ';'.join(missing)}):")
    ind = True if args.indicator else (False if args.no_indicator else None)
    for line in proton.launch_help(g, proxy, ind, opt.path):
        print("    " + line)
    from linuxport import tuning
    notes = tuning.game_notes(g.exe.name if g.exe else None)
    if notes:
        print("\n  notes for this game:")
        for n in notes:
            print("    - " + n)
    return 0


def cmd_verify(args) -> int:
    from linuxport import verify
    g = find(args.target, getattr(args, 'prefix', None))
    print(f"\n{g.name}\n{'-' * 60}")
    bad = 0
    rows = verify.run(g)
    if getattr(args, "vklayer", False) and not any(t == "vk layer" for _, t, _ in rows):
        from linuxport import vklayer
        rows += vklayer.verify(g)
    for lvl, title, detail in rows:
        mark = {"OK": "ok  ", "BAD": "FAIL", "WARN": "warn", "INFO": "    "}.get(lvl, lvl[:4])
        bad += lvl == "BAD"
        print(f"  [{mark}] {title:<18} {detail[:150]}")
    return 1 if bad else 0


def cmd_launch_options(args) -> int:
    from linuxport import proton
    from core import diagnose
    g = find(args.target, getattr(args, 'prefix', None))
    ind = True if args.indicator else (False if args.no_indicator else None)
    if getattr(args, "vklayer", False) or getattr(args, "no_vklayer", False):
        # The out-of-process route: one token, nothing else touched.
        from linuxport import vklayer
        on = bool(args.vklayer)
        cur = proton.current_launch_options(g)
        line = vklayer.launch_line(g, on, ind)
        print(f"\n{g.name}  appid={proton.appid_for(g) or '-'}  (vk layer {'on' if on else 'off'})")
        print(f"  current : {cur or '(none)'}")
        print(f"  steam > properties > launch options:\n    {line}")
        stale = vklayer.stale_overrides(g)
        if stale and on:
            print(f'  note    : WINEDLLOVERRIDES="{stale}" is still there with no in-process payload; harmless')
        if on:
            if vklayer.WRAPPER.is_file():
                print("  helper  : started by vklayer-run when the game launches, stopped when it exits "
                      "(DLSSNR_KEEP=1 to keep it; it idles at ~18% of a core otherwise)")
            else:
                print(f"  helper  : {'running' if vklayer.helper_running() else 'not running -- dlssnr-helper start before the game'}")
            if not vklayer.installed():
                print(f"  !! layer not installed: {vklayer.UPSTREAM}")
            for c in vklayer.CONTROLS[:1]:
                print(f"  note    : {c}")
        if args.apply:
            try:
                backup = proton.set_launch_options(g, line)
            except RuntimeError as e:
                sys.exit(f"  not applied: {e}")
            print(f"  applied. backup: {backup}")
        return 0
    proxy = args.proxy or proton.installed_proxy(g) or "dxgi.dll"
    route = (diagnose._manifest(g.install_dir) or {}).get("path")
    xl = proton.is_xlcore(g)
    cur = proton.launcher_overrides(g) if xl else proton.current_launch_options(g)
    print(f"\n{g.name}  appid={proton.appid_for(g) or '-'}  prefix={proton.prefix_for(g) or '-'}"
          + ("  (xivlauncher-rb)" if xl else ""))
    print(f"  current : {cur or '(none)'}")
    ind = True if args.indicator else (False if args.no_indicator else None)
    for line in proton.launch_help(g, proxy, ind, route):
        print("  " + line)
    missing = proton.missing_overrides(g, proxy, route)
    print(f"  overrides: {'complete' if not missing else 'missing ' + ';'.join(missing)}"
          + ("" if xl else f"   indicator: {'on' if proton.indicator_present(cur) else 'off'}"))
    if args.apply:
        try:
            if xl:
                backup = proton.set_launcher_overrides(g, proton.override_entries(g, proxy, route))
            else:
                backup = proton.set_launch_options(g, proton.launch_options(g, proxy, ind, route))
        except RuntimeError as e:
            sys.exit(f"  not applied: {e}")
        print(f"  applied. backup: {backup}")
    return 0


def cmd_dlls(args) -> int:
    """Swap the game's own DLSS SR/RR/FG runtimes with the newest archive next to the tools.

    Delegates to dlss5_proton.py's `dlls` subcommand (same state file, same undo)."""
    import subprocess
    from linuxport import proton
    g = find(args.target, getattr(args, 'prefix', None))
    tool = Path(__file__).resolve().parents[1] / "proton-tool" / "dlss5_proton.py"
    target = str(g.exe or g.folder)
    cmd = [sys.executable, str(tool), "dlls", target]
    pfx = proton.prefix_for(g)
    if pfx and not proton.appid_for(g):
        cmd += ["--prefix", str(pfx)]
    cmd.append(args.action)
    if args.action != "status":
        cmd.append("-y")
    if getattr(args, "streamline", False):
        cmd.append("--streamline")
    return subprocess.run(cmd).returncode


def cmd_vklayer(args) -> int:
    """The out-of-process route's own state: install, helper, runtime, live counters, controls."""
    from linuxport import vklayer
    if args.action in ("start", "stop"):
        import os, subprocess
        exe = vklayer.helper_exe()
        if not exe:
            sys.exit("dlssnr-helper is not on PATH -- install DLSS5VKLayer first (examples/vklayer)")
        env = dict(os.environ)
        if args.action == "start":
            env.setdefault("DLSSNR_TIME", "1")       # per-frame timing in helper.log; verify reads it
        rc = subprocess.run([exe, args.action], env=env).returncode
        if args.action == "stop":
            print("helper stopped. Start it again before the next game: dlss5_linux.py vklayer start")
        return rc
    print(f"\nDLSS5VKLayer\n{'-' * 60}")
    m = vklayer.manifest()
    print(f"  layer     {'installed: ' + str(m) if m else 'not installed -- ' + vklayer.UPSTREAM + ' (examples/vklayer)'}")
    if vklayer.patched_install():
        print(f"            {vklayer.patched_install()}")
    print(f"  helper    {vklayer.helper_exe() or 'dlssnr-helper not on PATH'}   {'running' if vklayer.helper_running() else 'stopped'}")
    if vklayer.helper_running():
        print("            it stays up after the game exits and spins ~18% of one core while idle: "
              "`vklayer stop` when you are done (closing dlssnr-gui also stops it)")
    digest, label = vklayer.runtime()
    print(f"  runtime   {label}" + (f"   sha256 {digest[:16]}..." if digest else ""))
    st = vklayer.shm_status()
    if st:
        print(f"  live      frames={st.get('layer_frames', '0')} answered={st.get('helper_frames', '0')} "
              f"model_up={st.get('model_up', '0')} hdr_detected={st.get('hdr_detected', '0')} proxy_format={st.get('proxy_format', '?')}")
    s = vklayer.shm_settings()
    if s:
        keys = ("enabled", "passes", "workingscale", "transfer", "mvec", "mvecquality", "hdrmode", "togglekey")
        print("  settings  " + " ".join(f"{k}={s.get(k)}" for k in keys))
    if args.action == "controls" or not st:
        print("  controls:")
        for c in vklayer.CONTROLS:
            print(f"   - {c}")
    return 0


def cmd_uninstall(args) -> int:
    g = find(args.target, getattr(args, 'prefix', None))
    for line in installer.uninstall(g, on_log=lambda m: print("   " + m)):
        print("  removed " + line)
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("recommend", "install", "uninstall", "launch-options", "verify", "dlls"):
        sp = sub.add_parser(name)
        sp.add_argument("target", nargs="?" if name == "recommend" else None, default="Stellar Blade")
        sp.add_argument("--prefix", help="wine prefix for a non-Steam game (remembered for this folder)")
        if name == "dlls":
            sp.add_argument("action", nargs="?", default="status", choices=["status", "install", "restore"],
                            help="status (default) | install = swap the game's DLSS SR/RR/FG to the newest archive | restore")
            sp.add_argument("--streamline", action="store_true", help="also swap sl.*.dll (off: breaks some titles)")
        if name == "launch-options":
            sp.add_argument("--proxy", help="default: the proxy recorded for this game")
            sp.add_argument("--apply", action="store_true", help="write it: Steam's localconfig.vdf (Steam closed) or xivlauncher-rb's launcher.ini (launcher closed)")
            sp.add_argument("--no-vklayer", action="store_true", help="remove the DLSS5VKLayer token")
        if name in ("launch-options", "verify"):
            sp.add_argument("--vklayer", action="store_true",
                            help="the out-of-process DLSS5VKLayer route: add VKLayer_DLSS5=1 / read its helper and layer logs")
        if name in ("launch-options", "install"):
            sp.add_argument("--indicator", action="store_true", help="add PROTON_DLSS_INDICATOR=1 (on-screen DLSS/FG readout)")
            sp.add_argument("--no-indicator", action="store_true")
        if name == "install":
            sp.add_argument("--route", choices=["native", "optiscaler", "upstream", "bridge", "feeder", "standalone", "renodx", "remix", "nrlinux"])
            sp.add_argument("--addon", help="local renodx add-on file (-> Options.renodx_local)")
            sp.add_argument("--dlssnr", help="catalog label, e.g. 310.8.0 (default: best for this GPU)")
            sp.add_argument("--dlss", help="catalog label for nvngx_dlss (default: newest)")
            sp.add_argument("--optiscaler", default="default",
                            help="default (y4my4m nightly 2026-09-06) | fallback (Dagherbou v0.1.2) | latest | <release tag> | /path/to.zip")
            sp.add_argument("--feeder-tag", help="feeder release tag (implies pre-release)")
            sp.add_argument("--provider", type=int, choices=[0, 1, 2, 3, 4],
                            help="feeder motion vectors: 0 texMotionVectors (DRME/qUINT), 1 Launchpad, 2 VORT (works on D3D12 here), 3 LumeniteFX Kernel (upstream default), 4 QuantMotion")
            sp.add_argument("--proxy", help="proxy DLL name (dxgi.dll, winmm.dll, ...)")
            sp.add_argument("--no-keep-game-dlss", action="store_true")
            sp.add_argument("--force-route", action="store_true", help="install a route the game does not list")
            sp.add_argument("--dry-run", action="store_true")
            sp.add_argument("-y", "--yes", action="store_true")
    vk = sub.add_parser("vklayer", help="the out-of-process DLSS5VKLayer route: install state, helper, runtime, live counters")
    vk.add_argument("action", nargs="?", default="status", choices=["status", "controls", "start", "stop"],
                    help="status | controls | start (the helper, before a game) | stop (after: an idle helper spins ~18%% of a core)")
    ap.add_argument("--scan", action="store_true")
    args = ap.parse_args()
    if args.cmd == "vklayer":
        return cmd_vklayer(args)
    if args.cmd == "install":
        return cmd_install(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "launch-options":
        return cmd_launch_options(args)
    if args.cmd == "uninstall":
        return cmd_uninstall(args)
    if args.cmd == "dlls":
        return cmd_dlls(args)
    name, sm = gpu.detect()
    print(f"GPU: {name}  sm={sm} ({gpu.label(sm)})  driver {gpu.driver_version()}")
    if args.scan:
        from linuxport import games as lgames
        for g in sorted(lgames.scan_all(), key=lambda g: g.name.lower()):
            fill(g)
            if not g.exe:
                continue
            s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness, sm=sm)
            state, sdetail = lgames.install_state(g)
            tag = {"installed": f"[installed: {sdetail}]", "foreign": f"[found: {sdetail}]"}.get(state, "")
            print(f"  {g.name[:38]:<38} {g.source.lower()[:6]:<6} {g.api:<6} x{g.bitness or '?'}  dlss={'y' if s.native_dlss else 'n'}  -> {s.recommended:<10} {tag}")
        return 0
    report(find(getattr(args, "target", None) or "Stellar Blade", getattr(args, "prefix", None)), sm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
