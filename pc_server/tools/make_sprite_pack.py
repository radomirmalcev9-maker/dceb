#!/usr/bin/env python3
"""Build a Capri sprite pack for the Android client.

Two sources are supported:

1. ``--video`` — a clip from the repository's own ``capri_media`` (or any clip
   you own). Frames are sampled, the static background is keyed out, the
   subject is cropped to one stable bounding box and every frame is written as
   an RGBA PNG with transparency.

2. ``--apk`` — a donor APK whose character art is already layered
   (``assets/characters/capri/face/*.png``, ``overlay/*.png``). The PNGs are
   pulled out, resized and renumbered. Only use art you have the rights to.

Output layout (consumed by ``ui/CapriCompanion.kt``)::

    <out>/idle_00.png ... idle_NN.png     # animation frames, in order
    <out>/manifest.json                   # {frames, fps, width, height, source}

Requires Pillow + numpy; frame extraction needs ``ffmpeg`` on PATH or the
``imageio-ffmpeg`` wheel.

    python tools/make_sprite_pack.py --video ../../capri_media/clip.mp4 \
        --out ../../android_client/app/src/main/assets/capri --height 256
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


# --------------------------------------------------------------------------- #
# ffmpeg discovery
# --------------------------------------------------------------------------- #

def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:                          # noqa: BLE001
        raise SystemExit(
            "ffmpeg not found: install it or `pip install imageio-ffmpeg` "
            f"({exc})") from exc


def extract_frames(video: Path, workdir: Path, start: float, duration: float,
                   fps: int, width: int) -> list:
    """Sample frames from a clip, scaled to ``width`` (height auto)."""
    out = workdir / "f_%04d.png"
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-ss", str(start), "-t", str(duration), "-i", str(video),
           "-vf", f"fps={fps},scale={width}:-1", "-frames:v", str(int(duration * fps) + 1),
           str(out), "-y"]
    subprocess.run(cmd, check=True)
    return sorted(workdir.glob("f_*.png"))


# --------------------------------------------------------------------------- #
# keying
# --------------------------------------------------------------------------- #

def luma(arr: np.ndarray) -> np.ndarray:
    # Ellipsis so this works on both a single frame (H, W, 3) and a stack
    # (N, H, W, 3).
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def key_frames(frames: list, dark_bg_threshold: float = 60.0,
               motion_lo: float = 6.0, motion_hi: float = 22.0,
               lum_lo: float = 4.0, lum_hi: float = 38.0) -> tuple:
    """Return (alpha_stack, union_mask) for the sampled frames.

    A pixel is foreground when it differs from the temporal median (it moves)
    or when it stands out from a dark background in luminance.
    """
    stack = np.stack([np.asarray(Image.open(p).convert("RGB"), dtype=np.float32)
                      for p in frames])
    bg = np.median(stack, axis=0)
    bg_l = luma(bg)
    dark_bg = float(bg_l.mean()) < dark_bg_threshold

    diff = np.abs(stack - bg).mean(axis=3)
    motion = np.clip((diff - motion_lo) / max(1e-6, motion_hi - motion_lo), 0, 1)

    fg_l = luma(stack)
    if dark_bg:
        lum = np.clip((fg_l - (bg_l[None, :, :] + lum_lo)) / max(1e-6, lum_hi - lum_lo), 0, 1)
    else:
        lum = np.clip(((bg_l[None, :, :] - lum_lo) - fg_l) / max(1e-6, lum_hi - lum_lo), 0, 1)

    alpha = np.maximum(motion * 0.9, lum)
    alpha = np.clip(alpha, 0, 1)

    union = (alpha.max(axis=0) > 0.35)
    union = keep_largest_blob(union)
    alpha = alpha * union[None, :, :]
    return alpha, union


def keep_largest_blob(mask: np.ndarray) -> np.ndarray:
    """Keep only the largest 4-connected component (kills keying noise)."""
    from collections import deque

    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = None
    best_size = 0
    for y in range(h):
        row = mask[y]
        for x in np.flatnonzero(row):
            if seen[y, x]:
                continue
            q = deque([(y, x)])
            seen[y, x] = True
            comp = []
            while q:
                cy, cx = q.popleft()
                comp.append((cy, cx))
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if len(comp) > best_size:
                best_size = len(comp)
                best = comp
    if not best:
        return mask
    out = np.zeros_like(mask, dtype=bool)
    ys, xs = zip(*best)
    out[list(ys), list(xs)] = True
    return out


def bbox_of(mask: np.ndarray, pad: int = 4) -> tuple:
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return 0, 0, mask.shape[1], mask.shape[0]
    h, w = mask.shape
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + 1 + pad)
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + 1 + pad)
    return x0, y0, x1, y1


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #

def write_video_pack(frames: list, alpha: np.ndarray, union: np.ndarray,
                     outdir: Path, height: int, name: str, fps: int,
                     source: str) -> dict:
    x0, y0, x1, y1 = bbox_of(union)
    outdir.mkdir(parents=True, exist_ok=True)
    count = 0
    size = None
    for i, frame_path in enumerate(frames):
        rgb = Image.open(frame_path).convert("RGB").crop((x0, y0, x1, y1))
        a = Image.fromarray((alpha[i, y0:y1, x0:x1] * 255).astype(np.uint8), mode="L")
        a = a.filter(ImageFilter.GaussianBlur(0.8))
        rgba = rgb.convert("RGBA")
        rgba.putalpha(a)
        scale = height / max(1, rgba.height)
        if scale < 1:
            rgba = rgba.resize((max(1, int(rgba.width * scale)), height), Image.LANCZOS)
        size = rgba.size
        rgba.save(outdir / f"{name}_{i:02d}.png", optimize=True)
        count += 1
    manifest = {"frames": count, "fps": fps, "width": size[0], "height": size[1],
                "source": source, "loop": True}
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def write_apk_pack(apk: Path, outdir: Path, height: int, name: str,
                   prefix: str) -> dict:
    """Pull already-cut RGBA layers out of a donor APK (only if you may)."""
    outdir.mkdir(parents=True, exist_ok=True)
    written = 0
    size = None
    with zipfile.ZipFile(apk) as zf:
        members = sorted(n for n in zf.namelist()
                         if n.startswith(prefix) and n.lower().endswith(".png"))
        if not members:
            raise SystemExit(f"no PNGs under {prefix!r} in {apk.name}")
        for i, member in enumerate(members):
            with zf.open(member) as fh:
                img = Image.open(fh).convert("RGBA")
            if img.height > height:
                scale = height / img.height
                img = img.resize((max(1, int(img.width * scale)), height), Image.LANCZOS)
            size = img.size
            img.save(outdir / f"{name}_{i:02d}.png", optimize=True)
            written += 1
    manifest = {"frames": written, "fps": 8, "width": size[0], "height": size[1],
                "source": f"{apk.name}:{prefix}", "loop": True}
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", type=Path, help="clip to key (your own media)")
    src.add_argument("--apk", type=Path, help="donor APK with layered RGBA art")
    ap.add_argument("--out", type=Path, required=True, help="sprite pack directory")
    ap.add_argument("--name", default="idle", help="frame prefix (default: idle)")
    ap.add_argument("--height", type=int, default=256, help="output frame height px")
    ap.add_argument("--start", type=float, default=1.0, help="video: start seconds")
    ap.add_argument("--duration", type=float, default=2.0, help="video: seconds to sample")
    ap.add_argument("--fps", type=int, default=12, help="video: frames per second")
    ap.add_argument("--width", type=int, default=360, help="video: working width")
    ap.add_argument("--prefix", default="assets/characters/capri/",
                    help="apk: asset path prefix to harvest")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        if args.video:
            frames = extract_frames(args.video, Path(tmp), args.start,
                                    args.duration, args.fps, args.width)
            if not frames:
                raise SystemExit("no frames extracted")
            alpha, union = key_frames(frames)
            coverage = float(union.mean())
            print(f"frames={len(frames)} subject coverage={coverage:.1%}")
            if coverage < 0.01 or coverage > 0.98:
                print("WARNING: keying looks wrong (coverage out of range) — "
                      "try another segment or brighter clip", file=sys.stderr)
            manifest = write_video_pack(frames, alpha, union, args.out, args.height,
                                        args.name, args.fps, args.video.name)
        else:
            manifest = write_apk_pack(args.apk, args.out, args.height, args.name,
                                      args.prefix)

    print(f"wrote {manifest['frames']} frames "
          f"({manifest['width']}x{manifest['height']}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
