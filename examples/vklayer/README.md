# The out-of-process route: DLSS5VKLayer under Proton

[bmitch87/DLSS5VKLayer](https://github.com/bmitch87/DLSS5VKLayer) (AGPL-3.0)
is a Linux Vulkan implicit layer plus a Windows helper. The layer captures each
presented frame; the helper, running under a Proton runner of its own, runs the
neural-rendering model on it and hands it back. Nothing goes into the game
folder, and the game keeps presenting its own frames if the helper is absent.

Use it for what the in-process routes cannot reach:

* **D3D12 games without DLSS** (FF7 Remake here): the feeder's same-device
  create faults under vkd3d-proton; this route never touches the game's device.
* **native Linux Vulkan games**: no other route enters those.
* anything else that presents through Vulkan (DXVK D3D9/10/11, vkd3d-proton
  D3D12) when you want nothing written beside the game.

A game with its own DLSS is still better on OptiScaler: real motion vectors,
real depth, before the UI, integrated with the upscaler. This route is
post-present (the HUD gets the model too), uses optical-flow vectors and zero
depth, and does not upscale.

Measured 2026-09-10/14: RTX 5090, NVIDIA 615.71, CachyOS, proton-cachyos-slr
as the helper's runner, DLSS5VKLayer 0.2.6-2 through 0.3.0-3, FF7 Remake at
5120x1440. Upstream ships most days. Run 0.3.0-2 or newer: 0.2.6-3 stopped a
1x1 probe swapchain from building a model and hanging the GPU with Xid 109 on
the first submit, and 0.3.0-2 fixed the hotkey stall noted under Controls.

## Install (user mode, no root)

```bash
# 1. the release tarball (works on Arch/CachyOS since 0.2.5-2)
tar -xzf dlssnr-*-linux-x86_64.tar.gz && cd dlssnr-*-linux-x86_64
./install.sh --user
#    -> ~/.local/lib/dlssnr, ~/.local/bin/dlssnr-{helper,gui,shmctl},
#       ~/.local/share/vulkan/implicit_layer.d/VK_LAYER_NV_dlssnr.{x86_64,i686}.json

# 2. the installer also writes ~/.config/environment.d/dlssnr.conf, a session-wide
#    VK_INSTANCE_LAYERS pin that exists only to order the layer against NVIDIA
#    Smooth Motion. Without Smooth Motion it lists the layer for every Vulkan app
#    at your next login; remove it. The layer stays gated on its launch token.
rm ~/.config/environment.d/dlssnr.conf

# 3. init picks a runner from compatibilitytools.d (user dirs first, then
#    /usr/share/steam/compatibilitytools.d), then import the model runtime
dlssnr-helper init
dlssnr-helper runners
dlssnr-helper import-binaries /path/to/dir-with-nvngx_dlssnr.dll   # nvngx.dll optional; nvapi64.dll not needed under Proton
dlssnr-helper doctor

# 4. start the helper BEFORE the game (first start builds its own Proton prefix)
dlssnr-helper start
dlssnr-helper status
```

Pick the runtime by hash, not by name: `sha256sum nvngx_dlssnr.dll`.
`e16bcf15...` is NVIDIA's signed 310.8 for RTX 50; `e67dee20...` is the
ShortFuse cross-generation build for RTX 20/30/40; `8270b350...` is a widely
copied patched build that works but is not the one to give a helper.
`python3 dlss5_linux.py vklayer status` names the one the helper has.

## Enable it for a game

The layer never starts its helper, the helper never stops on its own (it is a
daemon whose wait loop costs ~18% of a core while idle), and the controls are
a desktop window. `vklayer-run`, beside the port, handles all three around one
game. It runs on the host before the game's command: starts the helper if it
is not up and waits until it reports ready, opens `dlssnr-gui` so alt-tab
reaches the settings while you play (`DLSSNR_GUI=0` to skip; the GUI gets the
desktop session's environment, not Steam's, so it is a normal window), exports
the enable token, runs the game, then closes the GUI it opened and stops the
helper it started (`DLSSNR_KEEP=1` keeps both; anything already running is
left alone). The wrapper strips Steam's overlay preload from everything it
starts and hands it back to the game only: inherited, it made the helper take
35 s to come up. Minimise the GUI while playing, do not close it: its close
button stops the helper. Steam launch options:

```text
/path/to/dlss5-linux/vklayer-run %command%
```

`launch-options <game> --vklayer` prints exactly that line, with the real
path, and `--apply` writes it while Steam is closed. The bare form still works
if you would rather manage the helper yourself:

```text
VKLayer_DLSS5=1 %command%
```

The port writes it while Steam is closed (`launch-options <game> --vklayer
--apply`) or prints it while Steam is open. If the game previously had an
in-process route, remove that payload first (`dlss5_proton.py restore
<appid>` or `uninstall`); a leftover `WINEDLLOVERRIDES` in the options is
harmless once the files are gone. `PROTON_LOG=1 PROTON_DEBUG_DIR=$HOME` keeps
the layer's own lines in `~/steam-<appid>.log`, which `verify --vklayer` reads.

For a native Linux game: `vklayer-run ./game` (or `VKLayer_DLSS5=1 ./game` with the helper started by hand).

## Controls

There is no in-game overlay, because nothing runs inside the game. Settings
take effect on the next frame:

* `dlssnr-gui`: every setting as a row (style, intensity, local tone, local
  structure, skin structure, sharpness, detail and colour amounts, highlight
  guard, transfer mode, passes with per-pass overrides, working scale and
  downscaler, motion quality, HDR mode, white point), profiles, a split-screen
  compare, a frame hold that freezes the model's input, and debug views.
  Closing the GUI stops the helper.
* `dlssnr-shmctl /tmp/dlssnr-$UID/shm.bin settings | set <key> <value> | toggle enabled`
* a toggle hotkey: `DLSSNR_TOGGLE_KEY=F10` in the launch options (evdev: your
  user in the `input` group), or the GUI's "Toggle key". **Needs 0.3.0-2 or
  newer.** Before it, a bound key re-opened every `/dev/input` node once a
  second on the present thread, 122 ms of stall per second with 16 nodes here
  (upstream #12). 0.3.0-2 remembers which nodes are not keyboards and only
  stats them: 0.01 ms per pass here. The setting persists in
  `~/.config/dlssnr/config.ini` as `set_toggle_key`.

## Check a run

```bash
python3 dlss5_linux.py verify "<game>" --vklayer
```

reads the helper log (`~/.local/state/dlssnr/helper.log`), the live counters
in shared memory, and the layer's lines in the Proton log: feature create
result and size, frames through the model, transport, HDR state, and the
per-frame rebuild described below.

## Caveats (0.3.0-3, measured here)

1. **HDR10 swapchains rebuild the whole composition every frame.** The layer's
   `Prepare()` compares the raw PQ transfer flag against a value it stores
   normalised to zero whenever the proxy is 8-bit, so on a PQ10 swapchain the
   comparison never matches. `verify --vklayer` counts it. Fixed by a local
   patch -- see `patches/` -- which brought builds per session down from one
   per frame to four. Unpatched, play in SDR.
2. **The float16 HDR proxy does not engage on this runtime**: the helper only
   tries HDR when `GetFeatureRequirements` reports it, and that query returns
   `0xbad00005` (feature not supported), so the model is created SDR and sees
   PQ code values as an 8-bit picture. NVAPI is reachable in the helper's
   prefix (checked with `DXVK_NVAPI_LOG_LEVEL=info`), so this is the gate, not
   the setup. A local patch in `patches/` opens the gate and confirms the
   mechanism -- the model accepts the HDR create and the layer switches to the
   float16 proxy -- but upstream's composition on linear light then produces a
   hazy, desaturated picture, which is a separate defect. So HDR runs on the
   8-bit proxy (`set hdrmode 1`) with the rebuild fix, and looks correct.
3. **No zero-copy under a Wine runner.** dma-buf needs the fd-based
   external-memory extensions on the Wine-side device and winevulkan does not
   expose them; frames cross shared memory. `ptrace_scope` does not matter on
   that path.
4. **The present thread waits while the model runs.** The layer blocks on the
   helper's answer, so the game has nothing queued meanwhile and GPU
   utilisation drops. Measured here on FF7 Remake at 5120x1440 with the default
   full working scale: layer total 15.35 ms per frame, of which 4.83 ms
   encoding the proxy and 10.53 ms waiting, and the helper's own evaluate is
   10.06 ms of that wait. The wait is inference, not transport: upstream #13
   first blamed the shared-memory handshake, and its reporter withdrew that
   after measuring the helper side too. The lever is `workingscale`, which
   defaults to 1.0; cost scales with area, so 0.5 puts the model near 2.5 ms.
5. The "core" NGX init answering `0xbad00002` and the `[param-miss]
   DLSSNR.*Subrect*` lines in the helper log are expected.
6. **Idle repaint (0.3.0-3+) works under vkd3d-proton, seen on screen.** Pause
   the game and change a setting; the picture updates: the layer re-composes the held frame through the helper
   (visible in `helper.log` as a retune + rebuild, and as helper frames beyond
   the layer's presents). `DLSSNR_IDLE_REPAINT=0` turns it off. The layer's
   request for `VK_EXT_swapchain_maintenance1` is refused by vkd3d-proton's
   device and retried without; harmless.
7. **The helper is a daemon and it is not free while idle.** It must be running
   before the game starts, it outlives the game, and its wait loop spins on
   the shared-memory counter (20,000 yields, then a 1 ms sleep, repeat): about
   18% of one core, continuously, with no game running, plus a Vulkan device
   and ~35 MiB of VRAM (measured on 0.3.0-3). Stop it when you are done:
   `dlssnr-helper stop`, `dlss5_linux.py vklayer stop`, or close `dlssnr-gui`,
   which stops it too. Nothing in the layer or the helper stops it for you;
   `vklayer-run` in the launch options does.
