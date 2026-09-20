#!/usr/bin/env bash
# install-patched.sh [install|restore] [BUILD_DIR]
#
# Swap the patched DLSS5VKLayer binaries (layer64, layer32, helper) into the
# user install at ~/.local/lib/dlssnr, keeping the shipped ones beside them as
# *.orig-<version>; `restore` puts the shipped ones back. Everything else
# (dlssnr-helper script, GUI, shmctl, manifests) stays the upstream release.
#
# Refuses while the helper is running (Wine holds the exe) or a game is on
# the layer (the .so is mapped; replacing the inode is safe, but the running
# game keeps the old code and the next verify would be confusing).
set -euo pipefail

mode="${1:-install}"
build="${2:-$(dirname "$(readlink -f "$0")")/0.3.0-3+hdr}"
lib="$HOME/.local/lib/dlssnr"
# The shipped version, for naming the backups: the manifest carries it.
manifest="$HOME/.local/share/vulkan/implicit_layer.d/VK_LAYER_NV_dlssnr.x86_64.json"
ver="$( { grep -o '"implementation_version": *"[^"]*"' "$manifest" 2>/dev/null || true; } | grep -o '[0-9][0-9.-]*' | head -1 || true)"
ver="${ver:-0.3.0-3}"

if dlssnr-helper status 2>/dev/null | grep -q '^helper running'; then
    echo "helper is running: dlssnr-helper stop first" >&2; exit 1
fi
if pgrep -fa 'VKLayer_DLSS5=1' >/dev/null 2>&1 || pgrep -x dlssnr-gui >/dev/null 2>&1; then
    echo "a game on the layer or the GUI is running: close it first" >&2; exit 1
fi

pairs=(
    "$build/libVkLayer_NV_dlssnr.x86_64.so:$lib/layer/libVkLayer_NV_dlssnr.so"
    "$build/libVkLayer_NV_dlssnr.i686.so:$lib/layer32/libVkLayer_NV_dlssnr.so"
    "$build/dlssnr_helper.exe:$lib/helper/dlssnr_helper.exe"
)

case "$mode" in
install)
    (cd "$build" && sha256sum --quiet -c SHA256SUMS) || { echo "checksums in $build do not match" >&2; exit 1; }
    for p in "${pairs[@]}"; do
        src="${p%%:*}"; dst="${p##*:}"
        [ -f "$dst.orig-$ver" ] || cp -a "$dst" "$dst.orig-$ver"
        install -m 755 "$src" "$dst.new" && mv -f "$dst.new" "$dst"
        echo "installed $(basename "$src") -> $dst  (shipped kept as $(basename "$dst").orig-$ver)"
    done
    echo "patched build from $(cat "$build/BASE_COMMIT" | cut -c1-8) installed; patches: $build/patches.diff"
    ;;
restore)
    for p in "${pairs[@]}"; do
        dst="${p##*:}"
        if [ -f "$dst.orig-$ver" ]; then
            mv -f "$dst.orig-$ver" "$dst"; echo "restored $dst"
        else
            echo "no backup for $dst (nothing to restore)"
        fi
    done
    ;;
*)  echo "usage: $0 [install|restore] [BUILD_DIR]" >&2; exit 2 ;;
esac
