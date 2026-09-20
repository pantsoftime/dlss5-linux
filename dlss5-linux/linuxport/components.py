"""Component identity, recorded and compared across installs.

Why this exists. On 2026-09-20 a reinstall of Dreamfall Chapters silently
replaced the proven neural add-on -- renodx-dlss5++ 0.2026.827.2036, 391,168
bytes, seeded from a local bundle -- with the catalog's "4.55" download, which
is 1,694,720 bytes and carries the v4.6 engine. The game then crashed on its
first neural evaluate. Nothing in the tool said a component had changed,
because:

  * the manifest records one label per component, and a label is a weak
    identity: the rhi-repo download named "4.55" carries v4.6 engine markers
    (DLSS5-Feeder #90), so two different builds share one label;
  * when a local add-on file is used, installer.py records it as a free-text
    note and writes no component label at all, so the manifest could not say
    which add-on was installed;
  * nothing ever compared one install against the last.

So identity is taken here from the file itself -- sha256 and size, read after
the install has written it -- stored in the manifest as `component_files`, and
compared with what the previous install recorded. Differences are reported,
never blocked: a deliberate upgrade and an accidental swap look identical from
here, and only the user knows which one they meant.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core import installer as _inst

# What counts as a component: the things that decide behaviour. Configuration
# (ReShade.ini, OptiScaler.ini, dlss5-feed.cfg) is deliberately excluded --
# it changes on every launch and the user edits it on purpose.
PROXY_NAMES = {"dxgi.dll", "winmm.dll", "version.dll", "d3d11.dll", "d3d12.dll",
               "dinput8.dll", "d3d9.dll", "opengl32.dll"}


def is_payload(rel: str) -> bool:
    name = Path(rel).name.lower()
    if name.endswith((".addon64", ".addon32")):
        return True
    if name.startswith("nvngx") and name.endswith(".dll"):
        return True
    return name in PROXY_NAMES


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(root: Path, written: list[str]) -> dict:
    """{relative path: {sha256, size}} for every payload file just written."""
    out: dict[str, dict] = {}
    for rel in written:
        if not is_payload(rel):
            continue
        p = root / rel
        try:
            if p.is_file():
                out[str(rel).replace("\\", "/")] = {"sha256": sha256(p), "size": p.stat().st_size}
        except OSError:
            continue
    return out


def _manifest(root: Path) -> dict:
    try:
        return json.loads((root / _inst.MANIFEST).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def previous(root: Path) -> tuple[dict, dict]:
    d = _manifest(root)
    return dict(d.get("components") or {}), dict(d.get("component_files") or {})


def _short(entry: dict) -> str:
    return f"{entry.get('size', '?')} B  sha256 {str(entry.get('sha256', ''))[:12]}"


def diff(before_c: dict, before_f: dict, after_c: dict, after_f: dict) -> list[str]:
    """Human lines describing what changed. Empty on a first install."""
    if not before_c and not before_f:
        return []                      # nothing to compare against
    lines: list[str] = []
    for rel in sorted(set(before_f) | set(after_f)):
        b, a = before_f.get(rel), after_f.get(rel)
        if b and a and b.get("sha256") != a.get("sha256"):
            lines.append(f"{rel} is a DIFFERENT BUILD than the last install: "
                         f"was {_short(b)}, now {_short(a)}")
        elif b and not a:
            lines.append(f"{rel} was installed last time and is not here now")
        elif a and not b and before_f:
            lines.append(f"{rel} is new since the last install ({_short(a)})")
    for key in sorted(set(before_c) | set(after_c)):
        b, a = before_c.get(key), after_c.get(key)
        if b and a and b != a:
            lines.append(f"{key}: {b} -> {a}")
    return lines


def record(root: Path, rep, opt=None) -> dict:
    """Write component_files into the manifest, and give a local add-on a label.

    installer.install() has already written the manifest by the time this runs,
    so the file is read back and amended rather than composed here.
    """
    files = fingerprint(root, rep.written)
    local = getattr(opt, "renodx_local", None) if opt is not None else None
    if local:
        # A local build had no component label at all, which is how a swap went
        # unnoticed. Name it after the file it came from.
        rep.components.setdefault("renodx", f"local:{Path(local).name}")
    man = root / _inst.MANIFEST
    data = _manifest(root)
    if data:
        data["component_files"] = files
        if rep.components:
            data.setdefault("components", {}).update(rep.components)
        try:
            man.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
        except OSError:
            pass
    return files


def advisory(root: Path) -> list[str]:
    """What is recorded as installed here now, for the screen before a reinstall."""
    comps, files = previous(root)
    if not comps and not files:
        return []
    out = ["installed here now:"]
    for key in sorted(comps):
        out.append(f"    {key}: {comps[key]}")
    for rel in sorted(files):
        out.append(f"    {rel}  {_short(files[rel])}")
    if not files:
        out.append("    (no file fingerprints recorded -- this install predates the check, "
                   "so a build swap cannot be detected until after this run)")
    return out


def backfill(root: Path) -> int:
    """Fingerprint an existing install that predates this check, so the NEXT
    install can be compared against it. Reads the manifest's own file list."""
    data = _manifest(root)
    if not data or data.get("component_files"):
        return 0
    files = {}
    for rel in data.get("files", []):
        if not is_payload(rel):
            continue
        p = root / rel
        try:
            if p.is_file():
                files[str(rel).replace("\\", "/")] = {"sha256": sha256(p), "size": p.stat().st_size}
        except OSError:
            continue
    if not files:
        return 0
    data["component_files"] = files
    man = root / _inst.MANIFEST
    try:
        # Keep the manifest's timestamp. verify() treats it as the install time
        # and warns that the logs predate the install when it moves, so simply
        # recording what is already there must not look like a reinstall.
        st = man.stat()
        man.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
        import os
        os.utime(man, (st.st_atime, st.st_mtime))
    except OSError:
        return 0
    return len(files)


_orig_install = _inst.install


def _install(g, opt, *args, **kwargs):
    root = g.install_dir
    before_c, before_f = previous(root)
    rep = _orig_install(g, opt, *args, **kwargs)
    after_f = record(root, rep, opt)
    for line in diff(before_c, before_f, rep.components, after_f):
        rep.warnings.append(line)
    return rep


def install() -> None:
    _inst.install = _install
