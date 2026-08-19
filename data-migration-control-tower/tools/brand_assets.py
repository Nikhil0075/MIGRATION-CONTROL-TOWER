#!/usr/bin/env python
"""Derive the whole brand asset family from one generated master.

Why derive rather than generate each variant separately: an image model
draws a *different* tower every call. Asking it five times for "the same
logo, square" yields five logos. The square mark, the monochrome version,
the reversed version and the favicon must be the same artwork at different
crops and colours, so exactly one image is generated and everything else is
computed from it. That is also what makes this reproducible — re-running
this script on the same master reproduces the same family byte for byte.

The one genuinely tricky part is alpha. The model cannot produce a real
transparent background: asked for one it *paints a checkerboard*, which is
worse than useless because it looks correct in a preview and is opaque grey
in production. So the master is generated on flat white and the white is
keyed out here.

Naive keying (drop pixels near white) destroys antialiasing and leaves a
jagged edge with white fringes, which is glaring against the navy command
bar this logo actually sits on. Instead the image is treated as what it
physically is — the artwork composited over white:

    C = A·F + (1 - A)·255

Coverage is recovered as `255 - min(r,g,b)` (white has min 255, any
saturated or dark ink has a low min), gained slightly so solid ink reaches
full opacity, and then the foreground colour is un-premultiplied. That
keeps the soft edge pixels *as* soft edge pixels, so the logo composites
cleanly onto any background.

Usage:
    python tools/brand_assets.py <master.png> [--out DIR] [--check]

`--check` writes composite proofs over navy and white so edge halos are
visible to a human instead of being discovered in the header later.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "frontend" / "client" / "src" / "assets" / "brand" / "v1"

#: Matches --mct-brand-navy in frontend/client/src/styles/app.css. The
#: reversed logo is checked against this exact colour because it is the
#: command bar it has to sit on.
BRAND_NAVY = (0x0B, 0x25, 0x45)

#: Below this coverage a pixel is background, not a faint edge. Generated
#: "white" is not exactly 255 (JPEG-ish ringing leaves 253-254), so a hard
#: zero threshold would leave a grey wash across the whole canvas.
BACKGROUND_FLOOR = 10

#: Solid ink computes to ~244 rather than 255 because navy's blue channel
#: is not zero. Without this gain the logo is permanently ~4% transparent.
COVERAGE_GAIN = 1.45


def key_white_to_alpha(image: Image.Image) -> Image.Image:
    """Recover an alpha channel from artwork composited over white."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    out = Image.new("RGBA", (width, height))
    src = rgb.load()
    dst = out.load()

    for y in range(height):
        for x in range(width):
            r, g, b = src[x, y]
            coverage = 255 - min(r, g, b)
            if coverage <= BACKGROUND_FLOOR:
                dst[x, y] = (0, 0, 0, 0)
                continue
            alpha = min(255, int(round(coverage * COVERAGE_GAIN)))
            # Un-premultiply: recover F from C = A·F + (1-A)·255.
            scale = alpha / 255.0
            fr = (r - 255 * (1 - scale)) / scale
            fg = (g - 255 * (1 - scale)) / scale
            fb = (b - 255 * (1 - scale)) / scale
            dst[x, y] = (
                max(0, min(255, int(round(fr)))),
                max(0, min(255, int(round(fg)))),
                max(0, min(255, int(round(fb)))),
                alpha,
            )
    return out


def trim(image: Image.Image, pad: int = 0) -> Image.Image:
    """Crop to the artwork, optionally re-padding evenly."""
    box = image.getbbox()
    if box is None:
        raise ValueError("the image is entirely transparent — keying removed everything")
    cropped = image.crop(box)
    if not pad:
        return cropped
    padded = Image.new("RGBA", (cropped.width + pad * 2, cropped.height + pad * 2), (0, 0, 0, 0))
    padded.paste(cropped, (pad, pad))
    return padded


def split_glyph(image: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Separate the tower glyph from the wordmark.

    Found structurally rather than by a hardcoded fraction: the lockup has
    exactly one wide vertical band of empty pixels between the glyph and
    the text, so the widest empty column-run IS the gap. A fixed 30% split
    would silently mis-cut the moment the artwork is regenerated.
    """
    alpha = image.getchannel("A")
    width, height = image.size
    columns = [alpha.crop((x, 0, x + 1, height)).getbbox() is not None for x in range(width)]

    best_start = best_len = run_start = run_len = 0
    for x, filled in enumerate(columns):
        if filled:
            run_len = 0
            continue
        run_start = x if run_len == 0 else run_start
        run_len += 1
        if run_len > best_len:
            best_len, best_start = run_len, run_start

    if best_len < width * 0.02:
        raise ValueError("no clear gap between glyph and wordmark — is this the right master?")

    cut = best_start + best_len // 2
    return trim(image.crop((0, 0, cut, height))), trim(image.crop((cut, 0, width, height)))


def to_monochrome(image: Image.Image, ink: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """Flatten every ink colour to one, preserving the alpha shape."""
    flat = Image.new("RGBA", image.size, (*ink, 0))
    flat.putalpha(image.getchannel("A"))
    return flat


def square(image: Image.Image, size: int, margin: float = 0.10) -> Image.Image:
    """Center the artwork on a transparent square canvas."""
    inner = int(size * (1 - margin * 2))
    art = image.copy()
    art.thumbnail((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    return canvas


def build(master_path: Path, out_dir: Path, check: bool) -> list[Path]:
    master = Image.open(master_path)
    keyed = trim(key_white_to_alpha(master))
    glyph, _wordmark = split_glyph(keyed)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def save(image: Image.Image, name: str) -> None:
        path = out_dir / name
        image.save(path, "PNG", optimize=True)
        written.append(path)

    # Horizontal lockup, capped at a sensible header resolution. The 2752px
    # master is 4MB; nothing in the UI renders it above ~320px wide, and
    # every byte here is shipped to every visitor.
    lockup = keyed.copy()
    lockup.thumbnail((1200, 1200), Image.LANCZOS)
    save(lockup, "logo-horizontal.png")

    reversed_lockup = to_monochrome(lockup, ink=(255, 255, 255))
    save(reversed_lockup, "logo-horizontal-reversed.png")
    save(to_monochrome(lockup), "logo-horizontal-mono.png")

    save(square(glyph, 512), "logo-symbol.png")
    save(square(to_monochrome(glyph, ink=(255, 255, 255)), 512), "logo-symbol-reversed.png")

    for size in (32, 48, 180):
        save(square(glyph, size, margin=0.04), f"favicon-{size}.png")

    if check:
        # Deliberately OUTSIDE out_dir. Proofs written next to the real
        # assets get swept into the build by the CopyPlugin and shipped to
        # every visitor — which happened once already.
        proof_dir = REPO_ROOT / "build" / "brand-proofs"
        proof_dir.mkdir(parents=True, exist_ok=True)
        for label, bg in (("navy", BRAND_NAVY), ("white", (255, 255, 255))):
            canvas = Image.new("RGB", lockup.size, bg)
            canvas.paste(lockup if label == "white" else reversed_lockup, (0, 0), lockup)
            path = proof_dir / f"proof-on-{label}.png"
            canvas.save(path, "PNG")
            written.append(path)

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path, help="the generated master lockup, on a white background")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="also write composite proofs")
    args = parser.parse_args()

    if not args.master.is_file():
        raise SystemExit(f"No such master image: {args.master}")

    for path in build(args.master, args.out, args.check):
        size_kb = path.stat().st_size / 1024
        print(f"  {path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path}  {size_kb:.0f} KB")


if __name__ == "__main__":
    main()
