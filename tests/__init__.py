"""Test bootstrap: preserve the SDK's `mcp` namespace, expose bridge modules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
