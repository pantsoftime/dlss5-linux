# Local patches against DLSS5VKLayer 0.3.0-3

Two patches against upstream commit `4dc63ab9adf4c34831b3db7d58acdbcbf866c9f8`,
addressing caveats 1 and 2 in the route's README. They are offered as a diff,
not as binaries: DLSS5VKLayer is AGPL-3.0 and its own build instructions are the
right way to produce the two `.so` files and the Wine-side helper `.exe`.

Neither patch has been sent upstream yet.

## What they do

**`composition.cpp` -- stop the per-frame rebuild on an HDR10 swapchain.**
`Prepare()` compares the PQ transfer flag it was handed against the value it
stores, but it stores that value normalised to zero whenever the proxy is
8-bit. On a PQ10 swapchain with an 8-bit proxy the comparison therefore never
matches, and every image in the composition is destroyed and rebuilt on every
present. Normalising the incoming flag the same way before the comparison fixes
it. Measured effect here: builds per session fell from one per frame to four.

**`ngx_snippet.cpp` -- do not let a refused capability query decide HDR.**
The helper only attempts an HDR feature when `GetFeatureRequirements` reports
the capability, and on the 310.8 runtime that query answers `0xbad00005`
(feature not supported) on the Vulkan path even where NVAPI is reachable in the
helper's prefix and the feature creates perfectly well. A refused query says
nothing either way, so this treats it as unknown and lets the create decide;
a create that refuses the `IsHDR` flag already falls back to the 8-bit proxy.

## What we learned from running them

The rebuild fix works and is worth having on its own.

The HDR path is a different story. With the gate opened, the model accepts the
HDR create and the layer switches to the float16 proxy, so the mechanism is
confirmed -- but upstream's composition on linear light then produces a hazy,
desaturated picture with no extra brightness. That is a separate defect in the
composition, not something these patches address. **HDR therefore runs on the
8-bit proxy** (`dlssnr-shmctl <shm> set hdrmode 1`, persisted as
`set_hdr_mode=1` in `~/.config/dlssnr/config.ini`), which looks correct and now
carries the rebuild fix. `set hdrmode 0` puts the float16 path back for anyone
who wants to work on it.

## Building and installing

Build upstream normally at the base commit with the two patches applied, then:

```bash
./install-patched.sh install   # swaps in layer64, layer32 and the helper,
                               # keeping the shipped files as *.orig-<version>
./install-patched.sh restore   # puts the shipped ones back
```

The script refuses to run while the helper, the GUI or a game on the layer is
running, and verifies the build's `SHA256SUMS` before touching anything. It
expects the build directory to hold `libVkLayer_NV_dlssnr.x86_64.so`,
`libVkLayer_NV_dlssnr.i686.so`, `dlssnr_helper.exe`, `SHA256SUMS` and
`BASE_COMMIT`. Everything else in the install stays upstream's.

`dlss5_linux.py vklayer` reports when a patched build is in place, by spotting
the `*.orig-*` backups the script leaves behind.
