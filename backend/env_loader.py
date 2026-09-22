"""Tiny .env loader wrapper. python-dotenv is available in this project's
environment (unlike the first project's build environment), so we use it
directly -- kept as a thin wrapper so main.py doesn't care which one is
underneath."""
from __future__ import annotations

import os


def load_dotenv(path: str | None = None) -> None:
    try:
        from dotenv import load_dotenv as _load_dotenv
    except ImportError:
        _load_dotenv = None

    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [os.path.join(here, "..", ".env"), ".env"]
        path = next((c for c in candidates if os.path.exists(c)), candidates[0])

    if _load_dotenv is not None:
        _load_dotenv(path, override=False)
        return

    # Fallback: dependency-free manual parse, same as project 1.
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
