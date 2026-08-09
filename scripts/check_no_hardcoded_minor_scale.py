#!/usr/bin/env python3
"""CI lint: no code may render a minor-unit integer by hardcoding a /100 scale.

A minor unit only becomes a human number through its currency's ISO-4217
exponent. JPY, KRW and VND are zero-decimal; BHD and KWD are three-decimal.
Dividing their minor units by 100 does not produce a smaller-but-close number,
it produces a different quantity with no unit at all -- 25,000,000 VND (about
950 US dollars) renders as "250,000.00", and summing such values across
currencies produces a figure that cannot be right in any currency.

``money.format_money`` is the only sanctioned renderer. This gate exists because
the mistake it catches already reached a published headline number: the
adversarial golden-set reporter accumulated twelve currencies into one integer
and printed it as dollars.

Scans money-rendering modules and the test reporters that produce published
figures. Runs in milliseconds with no dependencies.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Every module that turns a minor-unit integer into something a human reads.
SCANNED = [
    ROOT / "src" / "farthing",
    ROOT / "tests" / "test_golden_set.py",
    ROOT / "tests" / "test_reply_guard.py",
]
# format_money is the sanctioned renderer and owns the only legitimate scaling.
EXEMPT_FILES = {"money.py"}


def _is_scale_literal(node: ast.AST) -> bool:
    """A literal 100 or 1000 used as a divisor."""
    return isinstance(node, ast.Constant) and node.value in (100, 1000)


def offenders(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        # x / 100  or  x // 100
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, (ast.Div, ast.FloorDiv))
            and _is_scale_literal(node.right)
        ):
            out.append(
                f"{path.relative_to(ROOT)}:{node.lineno}: divides by "
                f"{node.right.value} -- use money.format_money(minor, currency)"
            )
    return out


def main() -> int:
    found: list[str] = []
    for target in SCANNED:
        if target.is_dir():
            paths = sorted(target.rglob("*.py"))
        elif target.exists():
            paths = [target]
        else:
            print(f"SCAN TARGET MISSING: {target} -- refusing to report clean")
            return 1
        for p in paths:
            if p.name in EXEMPT_FILES:
                continue
            found.extend(offenders(p))
    if found:
        print("HARDCODED MINOR-UNIT SCALE (forbidden):")
        for f in found:
            print("  " + f)
        return 1
    print("OK: no hardcoded minor-unit scaling outside money.format_money")
    return 0


if __name__ == "__main__":
    sys.exit(main())
