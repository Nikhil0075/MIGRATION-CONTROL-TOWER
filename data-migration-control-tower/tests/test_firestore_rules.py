"""Tests for firestore.rules (Deploy & Harden Phase 5) — this project's
whole data-access model is server-mediated-only (the Python backend's
Admin SDK, which always bypasses Security Rules, is the only thing that
ever reads/writes Firestore — confirmed directly: the frontend client
bundle has zero references to the Firebase Firestore client SDK). These
rules exist purely to deny DIRECT client-SDK access, which the app never
needs. No Firebase emulator is available in this environment to run the
real rules-testing SDK against, so these are static content assertions
on the committed file — real, but not a substitute for
`firebase emulators:exec` coverage in an environment that has the
emulator installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

RULES_PATH = REPO_ROOT / "firestore.rules"
FIREBASE_JSON_PATH = REPO_ROOT / "firebase.json"


def test_firestore_rules_file_exists():
    assert RULES_PATH.is_file(), "firestore.rules must exist at the repo root for `firebase deploy` to find it"


def test_firestore_rules_declares_a_rules_version():
    content = RULES_PATH.read_text(encoding="utf-8")
    assert "rules_version = '2';" in content


def test_firestore_rules_denies_all_document_access_by_default():
    """The core security property: every path, read and write, denied —
    this app never uses the client SDK, so there is no narrower allowlist
    to maintain instead."""
    content = RULES_PATH.read_text(encoding="utf-8")
    assert "match /{document=**}" in content
    assert "allow read, write: if false;" in content


def test_firestore_rules_contains_no_unconditional_allow():
    """Regression guard: a future edit accidentally adding `allow ...: if
    true` (or omitting a condition, which Firestore treats as always-deny,
    but is worth catching explicitly) anywhere in this file would defeat
    the whole point of it."""
    content = RULES_PATH.read_text(encoding="utf-8")
    assert "if true" not in content


def test_firebase_json_points_at_the_committed_rules_file():
    config = json.loads(FIREBASE_JSON_PATH.read_text(encoding="utf-8"))
    assert config["firestore"]["rules"] == "firestore.rules"
