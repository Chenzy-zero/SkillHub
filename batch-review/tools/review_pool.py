#!/usr/bin/env python3
"""Compatibility entry point for the low-overhead reviewer-pool control plane."""

from __future__ import annotations

from review_pool_core import main


if __name__ == "__main__":
    raise SystemExit(main())
