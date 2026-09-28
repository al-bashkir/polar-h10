#!/usr/bin/env python3
"""See README.md. Usage: python scripts/compare_sessions.py SESSION [SESSION ...] [--output DIR]"""

import sys
from pathlib import Path

try:
    from h10_analysis.cli import compare_main
except ImportError:  # running from a checkout without installing the package
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from h10_analysis.cli import compare_main

if __name__ == "__main__":
    sys.exit(compare_main())
