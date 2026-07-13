#!/usr/bin/env python3
"""CI lint: the money-critical core must never touch a float.

Scans the pure-core modules for float literals and the ``float(`` builtin. A
float in a dollar path is a rounding-error vulnerability, so this is a hard gate,
not a style preference. Runs in milliseconds with no dependencies.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE = [
    "money.py",
    "domain.py",
    "policy.py",
    "ledger.py",
    "pg_ledger.py",
    "dead_letter.py",
    "idempotency.py",
    "execution_guard.py",
    "reconcile.py",
]
ROOT = Path(__file__).resolve().parent.parent / "src" / "farthing"


def offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            out.append(f"{path.name}:{node.lineno}: float literal {node.value!r}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
            out.append(f"{path.name}:{node.lineno}: float() call")
    return out


def main() -> int:
    found: list[str] = []
    for name in CORE:
        p = ROOT / name
        if p.exists():
            found.extend(offenders(p))
    if found:
        print("FLOAT IN MONEY CORE (forbidden):")
        for f in found:
            print("  " + f)
        return 1
    print(f"OK: no floats in {len(CORE)} money-core modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
