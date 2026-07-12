"""Deterministic idempotency keys.

A key is derived from the workflow identity plus the step name plus a canonical
hash of the payload. It is computed **inside the workflow**, never inside the
activity that calls Stripe — an activity retry re-uses the same key rather than
minting a fresh one, which is what makes execution exactly-once even across
worker crashes and redeliveries.

Same inputs → same key. Different payloads → different keys. There is no clock,
no randomness, and no counter, so the key is reproducible from the audit ledger.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Stable serialization: sorted keys, no insignificant whitespace, and any
    Decimal rendered as its string form so byte-identical inputs hash identically."""

    def default(o: Any) -> Any:
        from decimal import Decimal

        if isinstance(o, Decimal):
            # Canonicalize so value-equal Decimals hash identically:
            # Decimal("0.250") and Decimal("0.25") must serialize the same, and
            # never in exponent form. normalize() collapses trailing zeros;
            # format 'f' avoids scientific notation (e.g. 1E+2 -> "100").
            if not o.is_finite():
                raise ValueError("non-finite Decimal cannot be a key/hash input")
            return format(o.normalize(), "f")
        raise TypeError(f"not JSON-serializable: {type(o).__name__}")

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=default, ensure_ascii=True
    )


def derive_idempotency_key(workflow_id: str, step: str, payload: Any) -> str:
    """Deterministic Stripe idempotency key: ``sha256(workflow_id|step|payload)``."""
    if not workflow_id or not step:
        raise ValueError("workflow_id and step are required")
    material = f"{workflow_id}\x1f{step}\x1f{canonical_json(payload)}"
    return "frthg_" + hashlib.sha256(material.encode("utf-8")).hexdigest()
