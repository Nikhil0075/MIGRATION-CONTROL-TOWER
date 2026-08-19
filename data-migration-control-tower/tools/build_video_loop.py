#!/usr/bin/env python
"""Assemble the submission clips into one silent looping MP4.

The clips come off Wan 2.7 with an AAC track nobody asked for — the brief
called for a silent loop — so the audio is not merely ignored here, it is
dropped from the output entirely (`-an`). A muted track still ships bytes
and still un-mutes the moment someone drags the file into a player that
remembers volume.

ffmpeg comes from imageio-ffmpeg, which bundles a static binary inside
site-packages. That keeps a media dependency out of the system PATH, which
matters on a machine where `bq` already breaks because of a PATH-resolved
interpreter (see infrastructure/gcp_setup.sh).

Re-encoded rather than stream-copied. Stream copy is faster and would work
here — every clip is already 1280x720 h264 yuv420p at 30fps — but it fails
silently and confusingly the first time a clip is regenerated at different
settings, producing a file that plays for one shot and then stalls. A
single encode pass makes the output independent of what the inputs happen
to be.

Cuts are hard, matching docs/SUBMISSION_VIDEO.md: shot 6 ends calm and
shot 1 opens calm, so the loop point needs no crossfade to hide a jolt.

Usage:
    python tools/build_video_loop.py [--out docs/video/migration-control-tower-loop.mp4]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIP_DIR = REPO_ROOT / "docs" / "video"

#: Cut order. Kept explicit rather than globbed so the sequence is stated
#: in one place and a renamed file fails loudly instead of reordering the
#: story silently.
SHOTS = [
    "shot1-legacy-estate.mp4",
    "shot2-discovery.mp4",
    "shot3-fleet-activates.mp4",
    "shot4-row-loss.mp4",
    "shot5-memory-recall.mp4",
    "shot6-cutover-complete.mp4",
]


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
    except ImportError:  # pragma: no cover - environment guidance
        raise SystemExit(
            "imageio-ffmpeg is not installed. Run: pip install imageio-ffmpeg"
        )
    return imageio_ffmpeg.get_ffmpeg_exe()


def build(out_path: Path, clip_dir: Path = CLIP_DIR) -> Path:
    missing = [name for name in SHOTS if not (clip_dir / name).is_file()]
    if missing:
        raise SystemExit(f"Missing clips: {missing}")

    listing = clip_dir / "_concat.txt"
    listing.write_text(
        "".join(f"file '{(clip_dir / name).as_posix()}'\n" for name in SHOTS),
        encoding="utf-8",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-an",                       # no audio track at all, not a muted one
        "-c:v", "libx264", "-preset", "slow", "-crf", "20",
        "-pix_fmt", "yuv420p",       # the profile every player and browser accepts
        "-r", "30",
        "-movflags", "+faststart",   # metadata first, so it streams rather than buffering
        str(out_path),
    ]
    subprocess.run(command, check=True)
    listing.unlink(missing_ok=True)
    return out_path


def verify(path: Path) -> dict:
    """Assert the output really is silent, rather than assuming -an worked."""
    probe = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True,
    ).stderr
    return {
        "has_audio": "Audio:" in probe,
        "duration": next(
            (line.split("Duration:")[1].split(",")[0].strip()
             for line in probe.splitlines() if "Duration:" in line),
            "unknown",
        ),
        "video": next(
            (line.strip() for line in probe.splitlines() if "Video:" in line), ""
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path,
        default=CLIP_DIR / "migration-control-tower-loop.mp4",
    )
    args = parser.parse_args()

    out = build(args.out)
    facts = verify(out)
    size_mb = out.stat().st_size / (1024 * 1024)

    print(f"  {out.relative_to(REPO_ROOT)}  {size_mb:.1f} MB  {facts['duration']}")
    print(f"  {facts['video']}")
    if facts["has_audio"]:
        print("  WARNING: the output still carries an audio track", file=sys.stderr)
        raise SystemExit(1)
    print("  no audio track — silent as intended")


if __name__ == "__main__":
    main()
