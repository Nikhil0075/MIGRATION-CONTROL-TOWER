#!/usr/bin/env python
"""Wrap console captures in a branded, self-describing evidence frame.

A screenshot on its own is an assertion with no provenance. Someone
looking at one cannot tell whether it came from a live migration, a
local run against the fixtures, or a mocked-up UI — and in a submission
those three carry very different weight. Every frame here therefore
carries the label as a required field, plus the run, estate, agent
version and timestamp behind it, and one sentence saying what the frame
actually proves.

The label is the load-bearing part:

    LIVE       captured from a real migration against real infrastructure
    LOCAL      captured from the real console driven by local fixtures
    SIMULATED  a mock-up; the software did not produce this

`SIMULATED` is styled as a warning rather than a neutral tag on purpose.
The failure this whole file exists to prevent is a mocked screenshot being
read as a live result, so the frame that carries the weakest claim is the
one that says so loudest.

Every frame in the shipped set is LOCAL. They come from the Playwright
baselines, which drive the real console against fixture data — that is
what the label says, and it is deliberately not upgraded to LIVE because
the capture path cannot authenticate against a real estate.

Usage:
    python tools/evidence_frames.py [--out docs/evidence]
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = REPO_ROOT / "frontend" / "client" / "e2e" / "control-tower.spec.ts-snapshots"
BRAND = REPO_ROOT / "frontend" / "client" / "src" / "assets" / "brand" / "v1"
DEFAULT_OUT = REPO_ROOT / "docs" / "evidence"

NAVY = (0x0B, 0x25, 0x45)
INK = (0x16, 0x15, 0x13)
MUTED = (0x62, 0x5F, 0x5A)
BORDER = (0xDE, 0xDB, 0xD5)
SURFACE = (0xFF, 0xFF, 0xFF)

#: Label -> (background, foreground). SIMULATED is amber because it is the
#: claim a reader most needs to notice.
LABEL_STYLE = {
    "LIVE": ((0x15, 0x80, 0x3D), (255, 255, 255)),
    "LOCAL": ((0x1D, 0x6F, 0xD0), (255, 255, 255)),
    "SIMULATED": ((0xB4, 0x53, 0x09), (255, 255, 255)),
}

WIDTH = 1440
HEADER_H = 68
FOOTER_H = 104
PAD = 22

FONTS = {
    "regular": "C:/Windows/Fonts/segoeui.ttf",
    "bold": "C:/Windows/Fonts/segoeuib.ttf",
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONTS[kind], size)
    except OSError:  # pragma: no cover - font availability varies
        return ImageFont.load_default()


class Evidence:
    """One frame's declaration. Every field is required except the caption."""

    def __init__(self, *, key, source, title, proves, label, run_id, estate, agent):
        if label not in LABEL_STYLE:
            raise ValueError(f"{label!r} is not one of {sorted(LABEL_STYLE)}")
        self.key = key
        self.source = source
        self.title = title
        self.proves = proves
        self.label = label
        self.run_id = run_id
        self.estate = estate
        self.agent = agent


#: The evidence set. `proves` says what the frame demonstrates — not what
#: the page is called, which a reader can already see.
EVIDENCE = [
    Evidence(
        key="01-agent-registry", source="agents-1440-win32.png",
        title="Agent registry: seven specialists, resolved by capability",
        proves="Agents are resolved from APPROVED registry cards by capability, never imported directly, and each version is pinned and attributable.",
        label="LOCAL", run_id="run-live", estate="wwi-demo-estate", agent="discovery-agent 1.1.0",
    ),
    Evidence(
        key="02-run-lifecycle", source="overview-1440-win32.png",
        title="Run lifecycle: which agent is working, from the run's own history",
        proves="Stage status is derived from the run's state_history, so a completed stage is a recorded transition rather than a rendered guess.",
        label="LOCAL", run_id="run-live", estate="wwi-demo-estate", agent="orchestrator",
    ),
    Evidence(
        key="03-policy-denial", source="incidents-1440-win32.png",
        title="Policy denial: a refusal a prompt cannot influence",
        proves="policy_engine.py takes no free-text estate content as input, so a hostile table comment has no channel to reach an authorization decision.",
        label="LOCAL", run_id="run-live", estate="wwi-demo-estate", agent="risk-agent 1.0.0",
    ),
    Evidence(
        key="04-failed-reconciliation", source="reconciliation-1440-win32.png",
        title="Failed reconciliation: the defect is caught deterministically",
        proves="Row counts, aggregates and hashes are compared in ordinary Python; the failure is measured, not judged by a model.",
        label="LOCAL", run_id="run-live", estate="wwi-demo-estate", agent="validation-agent 1.0.0",
    ),
    Evidence(
        key="05-memory-assisted-recovery", source="memory-1440-win32.png",
        title="Memory-assisted recovery: a fact confirmed once, reused later",
        proves="A prior confirmed remediation is recalled as evidence and cited by later runs; it never replaces the re-validation that follows.",
        label="LOCAL", run_id="run-live", estate="wwi-demo-estate", agent="orchestrator",
    ),
    Evidence(
        key="06-approval-gate", source="approvals-1440-win32.png",
        title="Approval gate: the token is bound to the plan it approved",
        proves="An approval carries the plan hash it was issued against, so a plan changed after approval is refused at cutover — visible here before that happens.",
        label="LOCAL", run_id="run-pending", estate="wwi-demo-estate", agent="cutover-agent 1.0.0",
    ),
    Evidence(
        key="07-dead-letters", source="dead-letters-1440-win32.png",
        title="Dead letters: the fleet says what it gave up on",
        proves="A message that defeated a consumer is readable, attributable to the consumer that stopped trying, and replayable onto its original topic.",
        label="LOCAL", run_id="run-stuck", estate="wwi-demo-estate", agent="plan-created-sub consumer",
    ),
    Evidence(
        key="08-system-health", source="system-health-1440-win32.png",
        title="Runtime health: services, consumers and build provenance",
        proves="Each in-process consumer reports its own state and lease holder. The run's Cloud Trace id is recorded on the run document and resolves in Cloud Trace, which is outside this console.",
        label="LOCAL", run_id="run-live", estate="wwi-demo-estate", agent="worker-supervisor",
    ),
]


def _wrap(draw, text, font, max_width) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render(item: Evidence, out_dir: Path, captured_at: str) -> Path:
    shot = Image.open(SNAPSHOTS / item.source).convert("RGB")
    scale = (WIDTH - PAD * 2) / shot.width
    shot = shot.resize((WIDTH - PAD * 2, round(shot.height * scale)), Image.LANCZOS)

    body_font = _font("regular", 15)
    small_font = _font("regular", 13)
    meta_font = _font("bold", 13)
    title_font = _font("bold", 21)
    label_font = _font("bold", 13)

    canvas = Image.new("RGB", (WIDTH, HEADER_H + shot.height + FOOTER_H + PAD), SURFACE)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=NAVY)

    # Brand mark, then the evidence title.
    x = PAD
    logo_path = BRAND / "logo-symbol-reversed.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((36, 36), Image.LANCZOS)
        canvas.paste(logo, (x, (HEADER_H - logo.height) // 2), logo)
        x += logo.width + 12
    draw.text((x, HEADER_H // 2 - 13), item.title, font=title_font, fill=(255, 255, 255))

    # The label chip, right-aligned, sized to its text.
    bg, fg = LABEL_STYLE[item.label]
    chip_w = int(draw.textlength(item.label, font=label_font)) + 22
    chip = [WIDTH - PAD - chip_w, HEADER_H // 2 - 12, WIDTH - PAD, HEADER_H // 2 + 12]
    draw.rounded_rectangle(chip, radius=4, fill=bg)
    draw.text((chip[0] + 11, chip[1] + 5), item.label, font=label_font, fill=fg)

    canvas.paste(shot, (PAD, HEADER_H))
    draw.rectangle([PAD, HEADER_H, PAD + shot.width - 1, HEADER_H + shot.height - 1], outline=BORDER)

    # Footer: what it proves, then the provenance behind the claim.
    y = HEADER_H + shot.height + 14
    for line in _wrap(draw, item.proves, body_font, WIDTH - PAD * 2)[:2]:
        draw.text((PAD, y), line, font=body_font, fill=INK)
        y += 21

    y += 6
    fields = [
        ("Run", item.run_id),
        ("Estate", item.estate),
        ("Agent", item.agent),
        ("Captured", captured_at),
    ]
    x = PAD
    for name, value in fields:
        draw.text((x, y), f"{name} ", font=small_font, fill=MUTED)
        offset = draw.textlength(f"{name} ", font=small_font)
        draw.text((x + offset, y), value, font=meta_font, fill=INK)
        x += offset + draw.textlength(value, font=meta_font) + 26

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{item.key}.png"
    canvas.save(path, "PNG", optimize=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for item in EVIDENCE:
        path = render(item, args.out, captured_at)
        size_kb = path.stat().st_size / 1024
        print(f"  [{item.label:9}] {path.relative_to(REPO_ROOT)}  {size_kb:.0f} KB")


if __name__ == "__main__":
    main()
