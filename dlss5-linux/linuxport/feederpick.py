"""The feeder's "stable" pick, read from the tag rather than from /latest.

DLSS5-Feeder publishes its 1.16 betas without GitHub's pre-release flag, so
GitHub's /latest -- which core.sources.resolve_feeder() trusts for the default,
no-tag case -- answers a beta (v1.16.0-beta.6 on 2026-09-21). DLSS5-Autopilot
2.0.4 fixed the same thing upstream (#325, #348) by reading the tag. This does
the same from the release list: the newest non-draft release whose tag does not
name a beta, rc or alpha, sorted by feeder_key(). A pinned tag and the explicit
pre-release choice are untouched.
"""
from __future__ import annotations

import re

from core import sources as _src

_orig = _src.resolve_feeder
_PRE = re.compile(r"(alpha|beta|rc|preview|pre)[.\-]?\d*", re.I)


def resolve_feeder(prerelease: bool = False, tag: str = ""):
    if tag or prerelease:
        return _orig(prerelease=prerelease, tag=tag)
    rels = _src._json(_src.FEEDER_LIST_API)
    rels = [r for r in rels if not r.get("draft") and not _PRE.search(r.get("tag_name") or "")] \
        if isinstance(rels, list) else []
    if not rels:
        return _orig(prerelease=False, tag="")
    rel = max(rels, key=lambda r: _src.feeder_key(r.get("tag_name") or ""))
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    return rel.get("tag_name", "?"), assets


def install() -> None:
    _src.resolve_feeder = resolve_feeder
