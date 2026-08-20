"""Module entry point for ``python -m plastic_promise.deployment``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
