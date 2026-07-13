from __future__ import annotations

import unittest
from types import SimpleNamespace

try:
    from farthing.config import assert_credential_split
    _HAVE_CONFIG = True
except ImportError:
    # config.py needs pydantic-settings (an edge dependency). Offline, skip — this
    # runs in CI after `uv sync`.
    _HAVE_CONFIG = False


@unittest.skipUnless(_HAVE_CONFIG, "pydantic-settings not installed")
class TestCredentialSplit(unittest.TestCase):
    """The startup guardrail that the agent worker must NOT see the execute key."""

    def test_execute_key_visible_to_worker_fails_startup(self) -> None:
        worker = SimpleNamespace(stripe_execute_key="rk_execute_leaked")
        with self.assertRaises(RuntimeError):
            assert_credential_split(worker)  # type: ignore[arg-type]

    def test_no_execute_key_is_fine(self) -> None:
        worker = SimpleNamespace(stripe_execute_key=None)
        assert_credential_split(worker)  # type: ignore[arg-type]  # must not raise


if __name__ == "__main__":
    unittest.main()
