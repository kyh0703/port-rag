from __future__ import annotations

import sys
from pathlib import Path


CONTRACTS_GEN = next(
    candidate
    for parent in Path(__file__).resolve().parents
    for candidate in [parent / "contracts" / "gen" / "python"]
    if (candidate / "port" / "reg" / "v1" / "reg_pb2.py").exists()
)

if str(CONTRACTS_GEN) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_GEN))
