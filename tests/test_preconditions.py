"""A precondition that is not met must say so, at the point it is not met.

Two live-only failures motivated this, both observed on 2026-08-02 against a real
``temporal server start-dev``:

1. ``scripts/register_search_attribute.sh`` ends every run with "Done. The inbox
   can now list workflows ...". Its ``|| echo`` swallows EVERY failure, including
   an unreachable server, so it prints that claim after registering nothing.
2. ``tests/test_workflow_sandbox.py`` guards the server but not the search
   attribute, so a server WITHOUT ``farthing_state`` produced
   ``RPCError: Unable to query workflow due to Workflow Task in failed state``
   whose actual cause (``WORKFLOW_TASK_FAILED_CAUSE_BAD_SEARCH_ATTRIBUTES``) was
   only visible by dumping the workflow history by hand.

Same defect class either way: a success-shaped report not backed by the thing
being true. Pure stdlib, so this runs in the zero-install gate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from farthing.preconditions import REQUIRED_SEARCH_ATTRIBUTE, missing_search_attribute

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "register_search_attribute.sh"

# The script shells out to the Temporal CLI. `make verify` promises to run
# anywhere Python exists with nothing installed, so these two tests must skip
# rather than fail when that CLI is absent — otherwise this file would break the
# repo's headline zero-install claim on every clean clone.
_HAVE_TEMPORAL_CLI = shutil.which("temporal") is not None


class MissingSearchAttributeTest(unittest.TestCase):
    def test_absent_attribute_is_reported_with_the_exact_remediation(self) -> None:
        msg = missing_search_attribute([])
        self.assertIsNotNone(msg)
        assert msg is not None
        # Naming the attribute is not enough — the reader needs the fix.
        self.assertIn(REQUIRED_SEARCH_ATTRIBUTE, msg)
        self.assertIn("scripts/register_search_attribute.sh", msg)

    def test_present_attribute_reports_nothing(self) -> None:
        self.assertIsNone(missing_search_attribute([REQUIRED_SEARCH_ATTRIBUTE]))

    def test_other_attributes_do_not_satisfy_the_requirement(self) -> None:
        # A namespace with SOME custom attributes is not a namespace with ours.
        self.assertIsNotNone(missing_search_attribute(["CustomKeywordField", "BatcherUser"]))


@unittest.skipUnless(_HAVE_TEMPORAL_CLI, "temporal CLI not installed")
class RegisterScriptHonestyTest(unittest.TestCase):
    """The script must not claim success it did not achieve."""

    def _run_against_dead_address(self) -> subprocess.CompletedProcess[str]:
        # Port 1 is reserved and never has a Temporal server on it. HOME is passed
        # through deliberately: without it the CLI dies on "failed getting user
        # config dir" instead, and this test would pass for the wrong reason.
        env = dict(os.environ)
        env["TEMPORAL_ADDRESS"] = "127.0.0.1:1"
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_unreachable_server_fails_loudly(self) -> None:
        proc = self._run_against_dead_address()
        # Prove the run really failed to REACH the server, not for some other reason.
        self.assertIn("connection refused", (proc.stdout + proc.stderr).lower())
        self.assertNotEqual(proc.returncode, 0, "registering against a dead server must not exit 0")

    def test_unreachable_server_does_not_claim_the_inbox_will_work(self) -> None:
        proc = self._run_against_dead_address()
        combined = (proc.stdout + proc.stderr).lower()
        self.assertNotIn("the inbox can now list", combined)


if __name__ == "__main__":
    unittest.main()
