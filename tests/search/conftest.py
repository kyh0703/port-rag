from __future__ import annotations

import sys
from pathlib import Path


CONTRACTS_GEN = Path(__file__).resolve().parents[5] / "contracts" / "gen" / "python"

if str(CONTRACTS_GEN) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_GEN))
