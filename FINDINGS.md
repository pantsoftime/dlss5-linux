# Findings: DLSS 5 neural rendering under Proton

Everything below was measured on one machine (RTX 5090, NVIDIA 610.57,
proton-cachyos 11 / GE-Proton 11, CachyOS) between late August and early
September 2026. Dates matter: the projects involved ship several times a day.

## The build that matters: OptiScaler, y4my4my4m nightly

* **Dagherbou/OptiScaler_DLSSNR v0.2.0 and v0.2.0-patch1 deadlock under
  Proton** on some titles (007 First Light, Stellar Blade): black screen with
  the overlay drawn, every thread parked, no GPU fault; with an HDR10 swapchain
  they also lose the device. v0.1.2 did not deadlock but lost a race: its GPU
  capability check ran before the game loaded `nvapi64.dll`, `dlss: false`
  stuck for the session, and XeSS silently replaced the game's DLSS/DLAA.
* **y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG nightly 2026-09-06 (7b7220bb)**
  fixed both: the deadlocking title runs, and `Upscaler support ... dlss: true`
  on every game tried. Its changelog names the mechanism: the overlay is drawn
  on the game's queue under vkd3d-proton, and Vulkan device creation no longer
  enumerates adapters through DXVK's DXGI (which re-entered its own hook).
  This is the default the tools pin.
* The `nightly` tag rolls daily. A newer nightly is a candidate to test on one
  game, not an upgrade.

## D3D11 games with DLSS (Final Fantasy XIV)

* The ReShade-based routes need a D3D12 device; OptiScaler bridges the game to
  D3D12 (`Dx11Upscaler=dlss_12`, a token only the nightly line carries; older
  builds only offer `fsr22_12`/`xess_12` on the bridge) and runs the model there.
* **The bridge needs vkd3d-proton's `d3d12`/`d3d12core`.** Proton only injects
  those overrides on Steam's `run` verb. Launchers that use umu's
  `runinprefix` (XIVLauncher-RB, Lutris) hand the bridge Wine's builtin d3d12,
  which cannot take DXVK's adapter: `WithDx12::GetD3D12DeviceFromD3D11
  D3D12CreateDevice failed: 80004002`, every w/Dx12 upscaler falls back to
  FSR 2.2. Fix: `d3d12=n,b;d3d12core=n,b` in the launcher's DLL overrides.
* FFXIV specifics (from the RenoDX Discord community, reproduced under Proton):
  OptiScaler as `winmm.dll` (the game crashes with other proxies),
  `DrsMinOverrideEnabled=true`, `DrsMaxOverrideEnabled=true`, every
  `QualityRatio*` set to one value, `UseDelayedInit=true`; in-game DLSS on,
  Borderless, Frame Rate Threshold "Always enabled". Any in-game resolution or
  display-mode change with the bridge active crashes.
* **Multipass flashes in FFXIV.** The game keeps two DLSS features alive and
  both get an NR dispatch every frame; the fork decides whether an extra pass
  has been submitted by command-list pointer when the swapchain is not
  wrapped, so the feature whose list built pass 2 never passes the check and
  runs one pass while the other runs two. Output alternates per dispatch.
  Single-feature games are unaffected. Needs an upstream fix (present count on
  the bridge path); no config works around it.

## Games without DLSS: the feeder route

* **D3D11 (Dreamfall Chapters): works.** The feeder builds its DLAA contract
  on a private D3D12 device.
* **D3D12 (FF7 Remake Intergrade): does not work.** The feeder creates DLSS on
  the game's own device through ReShade's wrapper, and that create faults
  inside vkd3d-proton on most launches (11 of 15) with a garbage object
  pointer reached through the DLSS snippet's NVAPI cubin calls
  (`NvAPI_D3D12_GetCuda*: Invalid pointer`, then an access violation in
  `d3d12core.dll`). The four successes were all in the game's HDR mode; no
  configuration variable predicted it (add-on build, snippet version, DLSS
  override mode, swapchain re-initialisation were all bisected and ruled out).
  Hiding the cubin entrypoints via `DXVK_NVAPI_DISABLE_ENTRYPOINTS` makes NGX
  report DLSS unavailable, so that path cannot be avoided. When the create did
  succeed, no add-on build drew a visible NR frame on the R10G10B10A2 PQ
  backbuffer, and feeder 0.14.0-beta.4's own feed came out black there where
  0.12.0's was visible. **The out-of-process route below runs this game**:
  the model never touches the game's device, so the fault is not on the path.
* ReshadeMotionEstimation does not compile on ReShade's D3D12 backend
  (`error X3020: cannot sample from texture that is also used as render target`);
  VORT (`DLSS5_MV_PROVIDER=2`) does and feeds real vectors.
* **ReShade's Generic Depth can silently select nothing, and then DLAA runs
  with no depth at all.** On Dreamfall every candidate depth buffer was
  3840x1440 while the backbuffer was 5120x1440, and the add-on's default
  "Similar aspect ratio" heuristic rejected all of them as the wrong shape, so
  it picked none. Nothing said so: the game rendered, the feed reported
  "Working.", and only the feeder's own depth probe showed it --
  `min 0, max 0, variance 0` on every sample, including ones taken while the
  scene was moving. Ticking the right buffer by hand in the overlay
  (Add-ons -> Generic Depth; take the full-height one with the most draw
  calls) fixed it immediately. `verify` surfaces the probe.
* **That mismatch was the game rendering below the display's resolution.**
  Dreamfall's stored Unity resolution was 3840x1440 on a 5120x1440 display, and
  Proton presented the smaller render into a full-size swapchain -- so the feed
  saw 5120x1440 and DLAA was anti-aliasing an image that had already been
  upscaled. The game's own resolution menu never offered the native mode; Unity
  takes it on the command line regardless:
  `%command% -screen-width 5120 -screen-height 1440`. Worth checking before
  concluding anything about image quality on an ultrawide.
  Fixing it also fixed depth detection permanently: the depth buffers are now
  the same shape as the backbuffer, so the heuristic accepts them and no manual
  pick is needed after a restart (ReShade persists the heuristic settings, but
  never the chosen buffer -- handles change every run).
  Cost of going native, same scene: feed GPU 6.85 -> 6.95 ms/frame, frame
  interval 10.4 -> 10.9 ms.
* DLSS5-Feeder **1.16.0-beta.5** runs this route. Against 0.14.0-beta.5 the
  feed's own cost fell sharply (CPU 17.93 -> 0.30 ms/frame, GPU 21.42 -> 6.85
  ms/frame), though the two sessions were different scenes, so treat the
  direction as the finding and not the exact numbers.
* `PROTON_DLSS_UPGRADE=1` and the feeder: the game-local `nvngx_dlss.dll`
  must stay (with no NVAPI use in the game, the driver-store copy is not found
  on its own), and the classic RenoDX add-on tolerates the second copy the
  override loads.

## RenoDX add-on builds

| build | native route | feeder route |
|---|---|---|
| classic `++` 0.2026.827.2036 (391 KB) | works | creates/evaluates; black on a PQ backbuffer |
| v4.x 0.2026.828.517 (1.7 MB, = rhi-repo 4.55) | hangs/crashes in CreateFeature | not viable |
| perf-fix 4.1.5 0.2026.828.2110 (575 KB) | works, HDR-aware (transfer strength ~0.5 fixes tints) | double-detours core + snippet; create faults |
| rhi-repo 4.70 (1.7 MB) | untested | has a PQ BT.2020 colour bridge; black under the feeder |
| ShortFuse 2.6 MB (renodx-dlss SF 0.5x) | never hooks under Proton | - |

`NVSDK_NGX_D3D12_EvaluateFeature_C` is not exported by any NGX core on Linux;
the add-ons' hook miss on it is baseline, not a fault.

**The build matters more than the label, and a reinstall can change it under
you.** On 2026-09-20 a reinstall of a working Dreamfall setup replaced the
classic `++` build with the catalog's "4.55" download -- the 1.7 MB v4.x
engine in the table above -- and the game died on the first neural evaluate:
`0xC0000005` inside `nvngx_dlssnr.dll`, reached through the add-on, followed by
a `std::system_error` ("resource deadlock would occur") inside the add-on
itself. The feeder caught the first fault cleanly and stopped; the second one
killed the process. Two things made this easy to miss: the two builds can carry
the same version label (DLSS5-Feeder #90 notes the rhi-repo download named
"4.55" carries v4.6 engine markers), and the install record stored no component
label at all when a local add-on file was used. The tool now fingerprints the
payload files it installs (sha256, in `component_files` in the manifest) and
reports any that differ from the previous install, so a swap is visible instead
of silent. It reports rather than blocks: a deliberate upgrade and an accidental
swap look identical from there.

## Proton and DLSS knobs

* `PROTON_DLSS_INDICATOR=1` is the only way to get the on-screen DLSS readout:
  dxvk-nvapi rewrites `NGXCore\ShowDlssIndicator` at every launch, so a manual
  registry value is clobbered.
* `PROTON_DLSS_UPGRADE=1` writes DLSS 310.9 SR/RR/FG into
  `system32/umu/` and redirects the game's loads there; it coexists with
  OptiScaler and the add-ons. Swapping the game's own files (`dlls install`)
  does the same without the variable.
* `DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION=RENDER_PRESET_K`
  selects the DLSS preset.
* ReShade and OptiScaler must not both be loaded in the same process.

## Cyberpunk 2077 (GOG, RED4ext + CET + redscript)

OptiScaler livelocks the game thread on the mod stack; no DLSS feature is
created. Not a Proton problem as such; parked. Untested on the out-of-process
route, which does not enter the process at all.

## The out-of-process route: DLSS5VKLayer (2026-09-10/13)

* **What it is.** A Linux Vulkan implicit layer (`VK_LAYER_NV_dlssnr`, 64 and
  32-bit) captures the presented swapchain image and hands it over shared
  memory to a separate Windows helper (`dlssnr_helper.exe`) running under a
  Proton runner picked from `compatibilitytools.d`. The helper drives
  `nvngx_dlssnr.dll` through its Vulkan NGX exports, synthesises motion
  vectors with `VK_NV_optical_flow`, passes zero depth, composes with a
  RenoDX-derived shader and returns the frame. One launch token,
  `VKLayer_DLSS5=1`, and nothing in the game folder.
* **It works on FF7 Remake**, the D3D12-without-DLSS title every in-process
  route failed on: 4,531 frames through the model in one session, feature 18
  created on the snippet path in 78 ms, a visible effect, no fault, clean
  exit. It also runs under a 615.71 driver. Anything that presents through
  Vulkan is reachable, DXVK and vkd3d-proton alike, and it is the only route
  that reaches native Linux games.
* **What it costs.** Post-present: the HUD gets the model too. Synthetic
  vectors and no depth, so a game with its own DLSS is still better served by
  OptiScaler (real vectors, real depth, before the UI, integrated with the
  upscaler). No upscaling. One copy each way per frame at display resolution.
* **Versions move fast.** 0.3.0-2 (2026-09-13) is the floor worth running.
  0.2.6-3 refuses frames below 64x64 after a 1x1 probe swapchain was found to
  build a model and hang the GPU (Xid 109) on the first submit, killing the
  game with it. 0.3.0-1 adds SDR quality controls, a raw-composition bypass,
  and raises the multipass ceiling to 30 with a fast-pass mode on by default.
  0.3.0-2 fixes the hotkey stall below.
* **Three findings from our own runs, none of them ours to fix, all still
  present in 0.3.0-2.**
  1. On an HDR10 (PQ10) swapchain the layer rebuilt its whole composition
     every frame: the early-return compare in `Prepare()` tests the raw PQ
     transfer flag against a value it stores normalised to zero whenever the
     proxy is 8-bit, so it never matches. One line. Play in SDR until it is
     fixed; `verify --vklayer` counts the rebuilds.
  2. The float16 HDR proxy never engages on this runtime: the helper gates it
     on `NVSDK_NGX_VULKAN_GetFeatureRequirements` reporting HDR capability,
     and that query returns `0xbad00005` (feature not supported) here, so the
     model was created SDR and saw PQ code values as an 8-bit picture. It is
     not an NVAPI-reachability problem: with dxvk-nvapi logging on, the helper's
     prefix initialises NVAPI and identifies the card, and the query still
     fails. The snippet does ask dxvk-nvapi for one function it does not
     implement (id `0xad298d3f`, "Unknown function ID"), which may be what the
     query needs.
  3. Zero-copy dma-buf cannot engage under a Wine runner: the helper needs
     `VK_EXT_external_memory_dma_buf` + `VK_KHR_external_memory_fd` on the
     Wine-side device, and winevulkan does not expose the fd-based
     external-memory extensions. The shared-memory transport is what runs;
     `ptrace_scope` is irrelevant on that path.
* **Expected, not faults.** The "core" NGX init and its parameter allocator
  answer `0xbad00002`; the snippet route is the one that works. The
  `[param-miss] DLSSNR.*Subrect*` lines are the DLL probing optional inputs.
* **Controls.** There is no overlay because nothing runs inside the game.
  `dlssnr-gui` (live, next frame; profiles; split-screen compare; frame
  hold; debug views), `dlssnr-shmctl <shm> set|toggle`, and a toggle hotkey.
  Closing the GUI stops the helper, and the helper must be up before the game.
* **What it costs, measured.** FF7 Remake at 5120x1440, one pass, full working
  scale, `DLSSNR_TIME=1` on both ends, medians over 246 samples:

  | stage | ms per frame |
  |---|---|
  | layer: encode the proxy | 4.83 |
  | layer: wait for the helper | 10.53 |
  | layer: resolve | 0.01 |
  | layer total, on the present thread | 15.35 |
  | helper: evaluate (optical flow inside it: 2.44) | 10.06 |
  | helper total, with readback | 10.75 |

  The helper's own total fills the layer's wait: the present thread is waiting
  for inference, not for the transport. Upstream issue #13 first attributed
  the wait to the shared-memory handshake by comparing it with the optical-flow
  time alone; its reporter measured the helper side, found the handshake at
  0.32 ms and inference scaling with model size, and closed it. GPU
  utilisation still drops, because while the model runs the game has nothing
  queued. The lever is `workingscale`, 1.0 by default: cost scales with area,
  so 0.75 puts the evaluate near 5.7 ms and 0.5 near 2.5 ms. For comparison,
  OptiScaler in-process on FF7 Rebirth at 3840x1440 and 75% scale costs
  5.3-5.8 ms total.
* **Binding the toggle hotkey stalled the present thread** before 0.3.0-2:
  the evdev backend re-opened every `/dev/input` node inline on that thread
  once a second, and closing an evdev node costs 4-16 ms in the kernel. It ran
  whether or not neural rendering was enabled (upstream #12). Reproduced here,
  16 nodes, 122 ms per rescan. 0.3.0-2 remembers which nodes are not keyboards
  and only stats them afterwards: 0.01 ms per pass here.
* **The helper is a daemon, and idling is not free.** `dlssnr-helper start`
  leaves it up after the game exits; only `dlssnr-helper stop` or closing the
  GUI ends it. Its wait loop spins on the shared-memory counter (20,000 yields,
  then a 1 ms sleep, repeat), which costs about 18% of one core continuously
  with no game running, plus a Vulkan device and ~35 MiB of VRAM (measured on
  0.3.0-3). If a background process is hogging a core after you finish
  playing, this is it. The layer does not start the helper either: without
  one running, every frame passes through. The port's `vklayer-run` wrapper,
  put in front of `%command%`, starts the helper before the game, waits for
  it to report ready, opens `dlssnr-gui` beside the game so alt-tab reaches
  the settings (with the desktop session's environment, not Steam's, which
  carries the overlay in `LD_PRELOAD` and its runtime's library paths), and
  closes the GUI and stops the helper when the game exits; `launch-options
  --vklayer` emits that form. Three things learned building it. Steam hands
  the wrapper its overlay in `LD_PRELOAD`, and a helper started with that
  inherited took 35 s to become ready instead of 2 (the overlay hooks every
  Proton and Wine process of the helper and waits on Steam), while Steam
  counted those processes as SteamVR activity; the wrapper now strips the
  preload from everything it runs and gives it back to the game alone.
  `dlssnr-helper stop` can run for a minute or more after the helper is
  already dead (it polls every `/proc/*/cmdline` through a subprocess, in
  loops), so the wrapper runs it detached and Steam sees the game exit at
  once. And the GUI's close button stops the helper, so minimise it while
  playing. A backoff to a longer sleep after a moment of
  idleness would fix the cost upstream.
* **0.3.0-3 (2026-09-15) adds an idle repaint.** When the game stops
  presenting (paused, occluded, alt-tabbed) a layer thread acquires a
  swapchain image itself and re-composes the held frame, so settings changes
  show while the picture is still; `DLSSNR_IDLE_REPAINT=0` turns it off.
  Works on FF7 Remake under vkd3d-proton, seen on screen: with the game paused,
  intensity changes from `dlssnr-shmctl` updated the picture; they reached the helper (it logs the retune and the
  rebuild) and the helper evaluated ~700 frames more than the layer presented,
  which are the repaints. The layer's request for `VK_EXT_swapchain_maintenance1`
  at device creation is refused by vkd3d-proton's device (feature not present)
  and it retries with the game's own list; the repaint still worked without it.
* **SDR run, the intended path.** Same game, same resolution, SDR: the
  composition was built twice in 10,411 frames (once per swapchain) instead of
  once per frame, which confirms the rebuild bug is HDR10-only. Cost medians
  over 370 samples: encode 4.28, wait 10.59, total 15.02 ms; helper evaluate
  9.31 ms. SDR saves about half a millisecond of encode; the model costs the
  same, so `workingscale` remains the lever.
* **Native Linux Vulkan, smoke-tested.** `VKLayer_DLSS5=1 vkcube` put 1,830
  frames through the model at 500x500: the layer, transport and helper work
  for a native Vulkan process, not only for Proton. No real native game yet.
* **Where the frame rate stops, measured (0.3.1-1, 2026-09-20).** FF7 Remake at
  3840x2160, HDR on the 8-bit proxy. The game ran at 60 fps with the GPU at
  75-80%, and nothing outside the layer was limiting it: the busiest game
  thread sat near 20% of a core, no frame cap was set, and the card was in its
  top power state well under its limit. The layer's own cost per frame is the
  ceiling, because the present thread waits for the helper's answer before the
  game can queue the next frame. Changing `workingscale` live:

  | workingscale | model raster | layer total | ceiling | model evaluate |
  |---|---|---|---|---|
  | 1.00 | 3840x2160 | 16.4 ms | 61 fps | 10.6 ms |
  | 0.75 | 2880x1620 | 11.4 ms | 88 fps | 6.6 ms |
  | 0.50 | 1920x1080 | 8.6 ms | 116 fps | 4.1 ms |

  At 0.75 the game measured 85.7 fps against that 87-89 fps ceiling. Encoding
  barely moves with the scale (4.4 -> 3.6 ms), so about 3.6 ms plus optical flow
  is the floor. Motion quality Fast trims flow from 1.7 to 1.1 ms (ceiling 89
  -> 95 fps); a cheaper downscale filter did not help. The GPU stays near 80%
  at the ceiling for a structural reason: the work is serial inside the
  present call, and the frame crosses ordinary shared memory both ways because
  zero-copy is unavailable under a Wine runner, so about 2 ms of every frame
  has no GPU work queued by either process. Nothing configurable removes that.
  What would: overlapping the model with the next frame's rendering, or a
  helper that is not a separate Windows process. The maintainer has said they
  are considering [OpenDLSS-NR](https://github.com/maanHimself/OpenDLSS-NR) --
  an MIT Vulkan reimplementation of the 310.8 network that ships without
  weights -- to move away from the split design (upstream #26). No code yet.
* **Upgrading resets live settings when the protocol moves.** 0.3.0 -> 0.3.1
  bumped the shared-memory protocol from 19 to 20, so the header came back
  with defaults: HDR mode returned to auto, which on an HDR10 swapchain is the
  float16 path that composes wrongly. `dlssnr-gui` pushes the saved
  `config.ini` values within seconds of opening, so a normal launch recovers.
  `vklayer-run` now does the same for the HDR mode itself when started with
  `DLSSNR_GUI=0`. All four parts -- layer, helper, GUI and `dlssnr-shmctl` --
  must be the same release.
* **Not done.** A native Linux game, Cyberpunk with its mod stack.

## Neural-rendering runtime builds, by hash

`nvngx_dlssnr.dll` is not in any SDK. Three builds circulate; know which you
have (`sha256sum`), because a helper or add-on does not tell you:

| sha256 (prefix) | what it is |
|---|---|
| `e16bcf15e16e13f5` | NVIDIA-signed 310.8 for RTX 50; the content digest matches its Authenticode signature |
| `e67dee209320cdaf` | ShortFuse cross-generation 310.8 for RTX 20/30/40 (FP16 path on 20/30, Ada path on 40, RTX 50 unchanged) |
| `8270b350cd82de5c` | a patched 310.8.0: NVIDIA's signature over different content; works, measured at the same cost, but not the file to hand a helper |

## Driver 615.71.09 (2026-09-10)

The Linux driver moved from 610.57 to 615.71 under us. Windows reports on
the 616.64+ branch say the driver routes NR through its own runtime there
and every renodx-dlss5 add-on build faults on evaluate. On Linux 615.71:
OptiScaler on FF7 Rebirth unchanged (dlss: true, NR 5.8 ms median), the
feeder + classic add-on on Dreamfall unchanged, the VK layer runs. Neither
driver ships `nvngx_dlssnr.dll`; the runtime always came from the community
archive. A `GetDriverStore ... -3FFFFFFE` warning in OptiScaler.log is a
Windows registry query that cannot succeed under Wine; harmless.

## DLSS5-Feeder 1.16.0-beta.7 on Dreamfall (2026-09-23)

The feeder route runs 1.16.0-beta.7 here, with the classic `++` RenoDX
consumer, VORT motion and the game at native 5120x1440. It fixes a crash in
beta.4 to beta.6 that any D3D11 user could trip: dragging the add-on's
work-resolution slider below 100 killed the game, because a warning drawn
under the slider was passed to the UI as a printf format string. A session on
beta.7 was clean: five neural feature creations at full resolution, motion
fed in 19 of 21 probes, depth detected automatically, no GPU faults, and the
feed's own GPU cost at 6.64-6.69 ms per frame during play, against
6.85-6.95 ms on beta.5. The feeder's fixes now land on the 1.17 line, which
adds consumers and an experimental output stabiliser but nothing this route
needs yet.

**The feeder's "stable" release is not always stable.** DLSS5-Feeder publishes
its betas without GitHub's pre-release flag, so GitHub's "latest release"
answered `v1.16.0-beta.6` on 2026-09-21. The port's resolver trusted that for
an install that named no version; it now reads the tag instead, and takes the
newest release whose tag carries no beta, rc or preview marker. A pinned tag
and the explicit pre-release choice are unchanged. DLSS5-Autopilot 2.0.4
fixed the same thing upstream.

## DLSS5-Feeder 0.15.x and 1.16 (2026-09-09/10)

* 0.15.1 adds `hdr_bridge`: on a PQ BT.2020 swapchain the frame is decoded
  to linear FP16 on the way in and re-encoded on the way out, and the add-on
  asks ReShade for the colour space instead of guessing from the format. This
  is the mechanism behind "NR black or wrecked highlights on a 10-bit PQ
  backbuffer". D3D11 64-bit only, so it does not reach the D3D12 titles
  where we saw it.
* 0.15.0 accepts the OptiScaler DLSS-NR fork as a third feed consumer
  (installed as `winmm.dll`; the SuperSampling probe flips to min-arch 0x0
  when OptiScaler answers), fixes feature-level-10 shader creation and sRGB
  `work_resolution`, and makes RenoDX the default consumer.
* 1.16.0-beta.1: `DLSS5_FEED_D3D12_DEBUG=1` names D3D12 resource-state
  transitions (useful for a DRED capture of the Remake fault); a 1500 ms
  grace on the first NGX call for OptiScaler; `OutOfDate` text names 616.56
  as the minimum driver in Windows numbering.

## Other OptiScaler lines (standing as of 2026-09-11)

* **wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass** (NR before super resolution,
  1-3 passes, RTX 50 NVFP4 hybrid, RTX 20/30 MFG) branches from Dagherbou's
  09-03 tip, not from y4my4m, so none of the vkd3d-proton overlay/device
  fixes that ended the v0.2.x deadlock are in it. Its "submission epoch" gate
  never advances under vkd3d-proton on Cyberpunk 2077 (open issue #24: the
  DXGI swapchain wrapper is created on the 09-03 build and not on v0.7.5+,
  a regression between those builds). Not a pin candidate until that closes.
* **SirenBrink/..._FFXIV** v1.x/v2.0 is the wilsjo2 line plus FFXIV patches
  (forced quality, live quality change, FG resize, split-jitter fix). The
  command-list pass gate we root-caused for FFXIV's multipass flashing no
  longer exists in that code; its own status doc says the flicker cause was
  never established. An FFXIV-only experiment.
* **y4my4my4m** has not moved since 7b7220bb; the `nightly` tag rebuilds
  daily with "No changes" and keeps its dated assets, so the pinned
  20260906 archive stays downloadable.

## DLSS5-Autopilot 1.7.2 to 1.8.1

Upstream shipped four releases in four days after the core vendored here
(1.7.1): a library cache, Windows crash-record reading, an aim-for-fps
autotuner, NVIDIA-sourced SR/RR/FG runtimes, the wilsjo2 build as a third
OptiScaler choice, and the driver-fault verdict for 616.64+. The shim targets
still exist; `gpu.driver_at_least` gained a `have` parameter that the Linux
replacement must accept before a re-vendor. Not synced yet.
