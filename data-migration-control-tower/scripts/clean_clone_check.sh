#!/usr/bin/env bash
# Clean-clone release gate (Day 10 hardening, Phase 6 — the audit's
# "clean-clone release gate" finding). This repo isn't under git version
# control in this environment, so a literal `git clone` isn't available
# — instead this copies the working tree (excluding .venv, __pycache__,
# .pytest_cache, and .env, the same set a .gitignore'd clone would
# exclude) into a fresh temp directory and proves the codebase is
# structurally sound in total isolation from wherever it's normally run:
#   1. requirements.txt installs cleanly into a brand-new venv
#   2. pytest can COLLECT every test (imports resolve, no syntax errors)
#      without needing any live service — the fast, no-infra half of
#      "does this repo actually work from scratch"
#
# Usage: bash scripts/clean_clone_check.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> Copying working tree to $WORKDIR (excluding .venv, caches, .env)..."
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.env' --exclude='*.egg-info' --exclude='.git' \
    "$REPO_ROOT/" "$WORKDIR/"
else
  # rsync isn't available on a stock Windows Git Bash install — copy
  # CONTENTS (the /. suffix matters: without it, cp -r would nest the
  # source dir one level deeper inside $WORKDIR instead of populating it).
  cp -r "$REPO_ROOT"/. "$WORKDIR"/
  rm -rf "$WORKDIR/.venv" "$WORKDIR/.pytest_cache" "$WORKDIR/.env" "$WORKDIR/.git"
  find "$WORKDIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  find "$WORKDIR" -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
fi

cd "$WORKDIR"

if [ ! -f ".env.example" ]; then
  echo "FAIL: .env.example missing — a fresh clone has no documented way to configure credentials." >&2
  exit 1
fi
if [ -f ".env" ]; then
  echo "FAIL: .env present in a 'clean clone' — real credentials should never ship in the tree." >&2
  exit 1
fi
echo "    .env.example present, .env absent — OK."

echo "==> Creating a fresh venv..."
python -m venv .venv
if [ -f ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"  # Windows
else
  PY=".venv/bin/python"          # Unix
fi

echo "==> Installing requirements.txt into the fresh venv (this takes a few minutes)..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt

echo "==> Collecting every test (import/syntax check, no live services)..."
"$PY" -m pytest tests/ --collect-only -q

echo ""
echo "==> Clean-clone check PASSED: fresh install + full test collection succeeded"
echo "    in an isolated copy of the repo, with no .env and no prior venv."
