"""Proton-aware route policy on top of core.dlss.detect().

What we measured on this platform (RTX 50, proton-cachyos 11, Sept 2026):

* OptiScaler, y4my4m nightly line: `dlss: true` on every D3D12 game with
  DLSS (FF7 Rebirth, 007, Horizon), neural rendering on the game's own DLSS,
  HDR fine, no deadlock. The older Dagherbou builds either deadlocked
  (v0.2.x) or lost the NVAPI race and fell back to XeSS (v0.1.2). So the
  nightly makes upstream's own default -- OptiScaler on an RTX card -- the
  right one here too.
* D3D11 games WITH DLSS (Final Fantasy XIV): only OptiScaler works, through
  its dx11on12 bridge with `Dx11Upscaler=dlss_12`; the ReShade routes need a
  D3D12 device. The bridge needs vkd3d-proton's d3d12/d3d12core, which Proton
  only wires up on the Steam `run` path -- launchers that use umu
  `runinprefix` (XIVLauncher-RB) need `d3d12=n,b;d3d12core=n,b` added.
* D3D11 games WITHOUT DLSS: the feeder on a private D3D12 device (Dreamfall)
  works.
* D3D12 games WITHOUT DLSS: the feeder creates DLSS on the game's own device
  through ReShade's wrapper and that create faults non-deterministically
  inside vkd3d-proton (FF7 Remake, 11 of 15 launches). No proven route.
"""
from __future__ import annotations

from core import dlss as _dlss

_orig_detect = _dlss.detect

REASON_DLSS_D3D12 = (
    "OptiScaler (the y4my4m nightly this tool pins) hooks the game's own DLSS, "
    "reports 'dlss: true' under Proton, and runs neural rendering on it with a "
    "model-resolution dial and multipass. The native route is listed too: it "
    "leaves the game's DLSS untouched (frame generation and ray reconstruction "
    "stay the game's own) and is the one to pick if OptiScaler's overlay "
    "misbehaves in a title. A third option on this class of game is nrlinux: "
    "NapXDD's addon-dlssnr-linux, a Linux-built ReShade add-on that drives "
    "feature 18 through a forwarder and skips the driver's OTA gate "
    "(install --route nrlinux; experimental, one tested title upstream).")
REASON_DLSS_D3D11 = (
    "A D3D11 game with DLSS: the add-on routes need a D3D12 device, so only "
    "OptiScaler works here -- it bridges the game to D3D12 (Dx11Upscaler=dlss_12) "
    "and runs neural rendering on that side. Install it as winmm.dll, and if the "
    "game launches through umu (XIVLauncher-RB, Lutris) add d3d12=n,b;d3d12core=n,b "
    "to the overrides or the bridge gets Wine's own d3d12 and fails with 80004002.")
REASON_FEEDER_D3D11 = (
    "No DLSS in this game. The feeder builds a DLAA contract from ReShade's "
    "depth and a motion-vector shader and runs it on its own D3D12 device -- "
    "proven under Proton (Dreamfall Chapters). Use VORT for motion vectors; "
    "ReshadeMotionEstimation does not compile on the D3D12 backend here.")
REASON_FEEDER_D3D12 = (
    "No DLSS in this D3D12 game. The feeder has to create DLSS on the game's "
    "own device through ReShade's wrapper, and under Proton that create faults "
    "inside vkd3d-proton on most launches (FF7 Remake: 11 of 15); expect it not "
    "to work until the feeder or dxvk-nvapi changes. What does work here is the "
    "out-of-process DLSS5VKLayer route: a Vulkan layer hands each presented frame "
    "to a helper that runs the model on its own device, nothing in the game "
    "folder (FF7 Remake runs on it). `launch-options --vklayer --apply` adds its "
    "token once the layer is installed (examples/vklayer). Play in SDR on it for "
    "now: on an HDR10 swapchain the layer has an upstream rebuild bug.")


def detect(install_dir, folder, api, bitness, sm=None):
    s = _orig_detect(install_dir, folder, api, bitness, sm)
    d3d11 = api == "DX11"
    if s.native_dlss:
        if _dlss.OPTI in s.options:
            s.recommended = _dlss.OPTI
            s.reason = REASON_DLSS_D3D11 if d3d11 else REASON_DLSS_D3D12
    elif s.recommended == _dlss.FEEDER:
        s.reason = REASON_FEEDER_D3D11 if d3d11 else REASON_FEEDER_D3D12
    return s


def install() -> None:
    _dlss.detect = detect
