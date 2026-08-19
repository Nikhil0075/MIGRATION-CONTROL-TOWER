"""The evidence frames, and the labelling discipline that makes them worth anything.

A screenshot is an assertion with no provenance. The whole point of these
frames is the Live / Local / Simulated label, so the tests here are almost
entirely about that label being present, accurate, and impossible to
inflate by accident.

The specific failure being guarded against: a frame captured from fixture
data getting relabelled LIVE — deliberately or by a copy-paste — and a
reader taking a mocked-up screen for proof of a real migration.
"""

from __future__ import annotations

import pytest

from tools import evidence_frames as ef


def test_every_frame_declares_one_of_the_three_labels():
    for item in ef.EVIDENCE:
        assert item.label in ef.LABEL_STYLE, item.key


def test_an_invalid_label_is_rejected_at_construction():
    # Not at render time: a frame with a made-up label should be
    # impossible to define, not merely impossible to draw.
    with pytest.raises(ValueError, match="not one of"):
        ef.Evidence(
            key="x", source="overview-1440-win32.png", title="t", proves="p",
            label="VERIFIED", run_id="r", estate="e", agent="a",
        )


def test_nothing_captured_from_fixtures_may_claim_to_be_live():
    """The invariant that stops the label from being inflated.

    The Playwright baselines drive the REAL console, which is why they are
    honest LOCAL evidence — but they drive it against fixture data, so
    nothing sourced from them can ever be LIVE. A live frame has to come
    from a live capture, not from a renamed baseline.
    """
    for item in ef.EVIDENCE:
        if item.label == "LIVE":
            assert not (ef.SNAPSHOTS / item.source).exists(), (
                f"{item.key} claims LIVE but is sourced from the fixture-driven "
                f"Playwright baselines"
            )


def test_every_frame_says_what_it_proves():
    for item in ef.EVIDENCE:
        assert item.proves and len(item.proves) > 40, item.key
        # A claim that just restates the heading proves nothing; the reader
        # can already see the heading.
        assert item.proves.strip().lower() != item.title.strip().lower(), item.key


def test_every_frame_carries_its_provenance():
    for item in ef.EVIDENCE:
        for field in ("run_id", "estate", "agent"):
            assert getattr(item, field), f"{item.key} is missing {field}"


def test_frame_keys_are_unique_and_ordered():
    keys = [item.key for item in ef.EVIDENCE]
    assert len(keys) == len(set(keys))
    # Numbered so the set has a defined reading order in a submission.
    assert keys == sorted(keys)


def test_every_source_capture_exists():
    missing = [item.key for item in ef.EVIDENCE if not (ef.SNAPSHOTS / item.source).exists()]
    assert not missing, f"frames pointing at captures that do not exist: {missing}"


def test_simulated_is_styled_as_a_warning_not_a_neutral_tag():
    """The weakest claim must be the loudest label.

    LIVE and LOCAL are both legitimate evidence; SIMULATED means the
    software did not produce the image. If it rendered in the same calm
    blue as LOCAL, the one label a reader must not miss would be the
    easiest to skim past.
    """
    simulated_bg = ef.LABEL_STYLE["SIMULATED"][0]
    assert simulated_bg != ef.LABEL_STYLE["LOCAL"][0]
    assert simulated_bg != ef.LABEL_STYLE["LIVE"][0]
    # Amber-ish: red channel clearly dominant.
    red, green, blue = simulated_bg
    assert red > green > blue


def test_the_set_covers_the_claims_the_project_makes():
    """Each of these is a distinct architectural claim; a set that dropped
    one would leave the strongest part of the story unevidenced."""
    keys = " ".join(item.key for item in ef.EVIDENCE)
    for expected in (
        "agent-registry",       # capability resolution
        "run-lifecycle",        # the state machine
        "policy-denial",        # deterministic authorization
        "reconciliation",       # deterministic validation
        "memory",               # cross-run learning
        "approval",             # the human gate and plan binding
        "dead-letters",         # operational recovery
        "system-health",        # runtime provenance
    ):
        assert expected in keys, f"no evidence frame covers {expected!r}"
