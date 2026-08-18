#!/usr/bin/env python
"""Launcher so .claude/launch.json can start the Control Tower UI without
depending on a POSIX shell being on PATH (WSL/bash aren't available in
this environment's preview runner)."""
import os
import sys

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data-migration-control-tower"))
sys.path.insert(0, os.getcwd())

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("frontend.app:app", host="127.0.0.1", port=8080)
