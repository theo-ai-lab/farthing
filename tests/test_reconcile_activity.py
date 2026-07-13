from __future__ import annotations

import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# activities.py imports temporalio (an edge dependency). Offline, skip — this runs
# in CI after `uv sync`. find_spec checks availability without importing the stack.
_HAVE_TEMPORAL = importlib.util.find_spec("temporalio") is not None


@unittest.skipUnless(_HAVE_TEMPORAL, "temporalio not installed")
class TestReconcileActivityGuard(unittest.TestCase):
    """reconcile_window is the nightly safety net. Its database URL is an OPTIONAL
    setting; if it is unset the activity must fail LOUD and non-retryable, not
    crash every night with an AttributeError inside a retried activity (which is
    what an unguarded ``None.get_secret_value()`` did)."""

    def test_missing_database_url_raises_non_retryable(self) -> None:
        from temporalio.exceptions import ApplicationError

        from farthing.activities import reconcile_window

        def stub_settings() -> SimpleNamespace:
            return SimpleNamespace(reconcile_database_url=None)

        with (
            patch("farthing.activities.worker_settings", stub_settings),
            self.assertRaises(ApplicationError) as ctx,
        ):
            asyncio.run(reconcile_window(0, 100))
        self.assertTrue(ctx.exception.non_retryable)
        self.assertIn("RECONCILE_DATABASE_URL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
