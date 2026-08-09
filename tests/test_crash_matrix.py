"""Kill the process at every boundary on the money path, then recover.

This is the falsifiable form of the claim the architecture makes: *money moves
exactly once, and a crash anywhere leaves a state we can recover from.* Prose and
a code comment cannot fail; this can.

The matrix kills the process at every durability boundary in
:func:`farthing.settlement.settle_refund` — and at the boundary we do not own,
where the provider applied the refund and the response never came back — then
runs recovery the way Temporal would (retry the same activity with the same
token) and asserts the money invariant after **every** attempt, not just the last.

The kill points are not a hand-written list. They are read from
``settlement.KILL_POINTS``, which is cross-checked against the ``on_step("…")``
calls in the settlement source with :mod:`ast`. Adding a boundary to the sequence
extends this matrix automatically; adding one without declaring it fails
``test_declared_kill_points_match_the_source``; declaring one the sequence never
crosses fails ``test_matrix_reaches_every_declared_kill_point``.

The invariant asserted at every point:

  (a) at most one refund exists for the ticket;
  (b) the hash chain over everything committed still verifies;
  (c) recorded state matches the provider, or reconciliation raises an incident —
      concretely, money NEVER moved without a committed audit row;
  (d) no money moved beyond the policy-decided, human-approved amount.

Everything here is offline and deterministic. It proves the *logic* survives
death at every boundary; it does not prove a deployment does. The live
kill-the-container drill against real Stripe is a separate, deliberately
out-of-scope exercise.
"""

from __future__ import annotations

import ast
import random
import unittest
from pathlib import Path

from _fault_injection import (
    NOW_TS,
    SECRET,
    Case,
    FakeConn,
    FakeDatabase,
    FakeStripe,
    KillAt,
    ProcessDied,
    committed_entries,
)
from farthing import settlement
from farthing.domain import RefundResult
from farthing.execution_guard import ExecutionRefused
from farthing.idempotency_store import PostgresIdempotencyStore
from farthing.ledger import verify_chain
from farthing.reconcile import reconcile
from farthing.settlement import settle_refund

API_VERSION = "2026-06-24.dahlia"

#: The boundary we do not own: the provider applied the refund and the process
#: died before the response was ever observed. It cannot be expressed as an
#: ``on_step`` call inside our own process, so it is declared here and the matrix
#: runs the union.
PROVIDER_DEATH = "provider_create_applied_then_died"
PROVIDER_KILL_POINTS: tuple[str, ...] = (PROVIDER_DEATH,)

ALL_KILL_POINTS: tuple[str, ...] = settlement.KILL_POINTS + PROVIDER_KILL_POINTS

#: Mirrors the workflow's RetryPolicy(maximum_attempts=6) for execute_refund.
MAX_ATTEMPTS = 6


def kill_points_in_settlement_source() -> list[str]:
    """Every boundary the settlement source actually announces, read from the AST.

    Enforcing "a single string literal" is what keeps the boundary set
    enumerable: a computed argument would make the matrix silently incomplete.
    """
    source = Path(str(settlement.__file__)).read_text(encoding="utf-8")
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "on_step":
            continue
        arg = node.args[0] if len(node.args) == 1 and not node.keywords else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            raise AssertionError(
                "on_step must be called with exactly one string literal so the "
                f"kill points stay enumerable (line {node.lineno})"
            )
        found.append(arg.value)
    return found


class _Attempt:
    """The outcome of one activity attempt."""

    def __init__(
        self,
        survived: bool,
        steps: list[str],
        provider_died: bool,
        result: RefundResult | None = None,
        refused: str | None = None,
    ) -> None:
        self.survived = survived
        self.steps = steps
        self.provider_died = provider_died
        self.result = result
        self.refused = refused


def run_attempt(case: Case, db: FakeDatabase, stripe: FakeStripe, kill: str | None) -> _Attempt:
    """One execute_refund attempt, killed at ``kill`` if it gets that far.

    Process death is modelled by discarding the connection: everything this
    attempt had not committed is gone, exactly as psycopg would leave it. A
    refusal is captured rather than raised, because the workflow treats it as a
    terminal outcome (dead-letter), not as a crash.
    """
    observer = KillAt(kill if kill in settlement.KILL_POINTS else None)
    if kill == PROVIDER_DEATH:
        stripe.arm_death_on_next_create()
    conn = FakeConn(db)
    provider_died = False
    survived = True
    result: RefundResult | None = None
    refused: str | None = None
    try:
        result = settle_refund(
            SECRET, token=case.token, decision=case.decision, prepared=case.prepared,
            conn=conn, store=PostgresIdempotencyStore(conn), gateway=stripe,
            now_ts=NOW_TS, trace_id="trace-crash", api_version=API_VERSION,
            on_step=observer,
        )
    except ProcessDied as died:
        survived = False
        provider_died = str(died) == PROVIDER_DEATH
    except ExecutionRefused as e:
        survived = False
        refused = e.code
    stripe.disarm_death()  # an armed death the attempt never reached does not linger
    return _Attempt(survived, observer.seen, provider_died, result, refused)


class CrashMatrixAssertions(unittest.TestCase):
    """Shared invariant checker. Every scenario below funnels through it."""

    def assert_money_invariant(
        self, case: Case, db: FakeDatabase, stripe: FakeStripe, *, where: str
    ) -> None:
        entries = committed_entries(db)

        # (b) the chain over everything committed still verifies.
        self.assertTrue(verify_chain(entries), f"{where}: ledger chain does not verify")

        # (a) at most one refund exists for the ticket.
        self.assertLessEqual(
            len(stripe.applied), 1, f"{where}: {len(stripe.applied)} refunds exist for one ticket"
        )
        self.assertLessEqual(
            len(entries), 1, f"{where}: {len(entries)} audit rows exist for one reservation"
        )

        # (d) no money moved beyond the approved, policy-decided amount.
        for refund in stripe.applied:
            self.assertEqual(refund["amount"], case.amount_minor, f"{where}: refund amount drifted")
            self.assertEqual(refund["currency"], case.prepared.currency, f"{where}: currency drifted")
            self.assertEqual(refund["idempotency_key"], case.idempotency_key, f"{where}: key drifted")
        self.assertLessEqual(
            sum(r["amount"] for r in stripe.applied), case.amount_minor,
            f"{where}: total money moved exceeds the approved amount",
        )

        # (c) recorded state matches the provider, or reconciliation raises an
        # incident. The one divergence this design permits is an audit row ahead
        # of the money; money ahead of the audit row is the failure that must
        # never happen, and is exactly reconcile's unmatched_in_stripe.
        report = reconcile(
            entries, stripe.list_refunds_in_window(),
            window_start_ts=NOW_TS - 1, window_end_ts=NOW_TS + 1,
        )
        self.assertEqual(
            report.unmatched_in_stripe, (),
            f"{where}: money moved with no committed audit row (approved-but-invisible payment)",
        )
        self.assertEqual(report.mismatched, (), f"{where}: ledger and provider disagree on the amount")
        self.assertEqual(report.duplicate_keys, (), f"{where}: an idempotency key was used twice")
        self.assertEqual(report.malformed, (), f"{where}: malformed provider record")
        if report.unmatched_in_ledger:
            self.assertEqual(
                stripe.applied, [],
                f"{where}: an audit row is unmatched while the provider holds a refund",
            )


class TestKillPointDeclaration(CrashMatrixAssertions):
    def test_declared_kill_points_match_the_source(self) -> None:
        """The matrix is built from KILL_POINTS, so KILL_POINTS must be the truth.
        A boundary added to the sequence without being declared here would leave a
        hole in the matrix that nothing else would notice."""
        in_source = kill_points_in_settlement_source()
        self.assertEqual(
            len(in_source), len(set(in_source)), "a kill point is announced twice in the source"
        )
        self.assertEqual(
            len(settlement.KILL_POINTS), len(set(settlement.KILL_POINTS)),
            "KILL_POINTS contains a duplicate",
        )
        self.assertEqual(
            sorted(in_source), sorted(settlement.KILL_POINTS),
            "settlement.KILL_POINTS does not match the boundaries the source announces",
        )


class TestCrashMatrix(CrashMatrixAssertions):
    """The matrix proper."""

    def _drive_to_completion(
        self, case: Case, db: FakeDatabase, stripe: FakeStripe, kills: list[str | None], where: str
    ) -> set[str]:
        """Run attempts with the given kill schedule, then retry uninjected until
        one survives — the recovery Temporal performs. Asserts the invariant after
        every attempt. Returns the boundaries actually reached."""
        reached: set[str] = set()
        for attempt_no in range(1, MAX_ATTEMPTS + 1):
            kill = kills[attempt_no - 1] if attempt_no <= len(kills) else None
            outcome = run_attempt(case, db, stripe, kill)
            if outcome.refused is not None:
                self.fail(f"{where}: attempt {attempt_no} was refused ({outcome.refused})")
            reached.update(outcome.steps)
            if outcome.provider_died:
                reached.add(PROVIDER_DEATH)
            self.assert_money_invariant(case, db, stripe, where=f"{where} after attempt {attempt_no}")
            if outcome.survived:
                break
        else:
            self.fail(f"{where}: never settled within {MAX_ATTEMPTS} attempts")

        # End state: the refund happened, once, with one audit row that matches it.
        self.assertEqual(len(stripe.applied), 1, f"{where}: end state is not exactly one refund")
        entries = committed_entries(db)
        self.assertEqual(len(entries), 1, f"{where}: end state is not exactly one audit row")
        report = reconcile(
            entries, stripe.list_refunds_in_window(),
            window_start_ts=NOW_TS - 1, window_end_ts=NOW_TS + 1,
        )
        self.assertTrue(report.clean, f"{where}: end state does not reconcile: {report}")
        return reached

    def test_matrix_reaches_every_declared_kill_point(self) -> None:
        """Kill at every boundary, twice over: once on the first attempt and once
        during recovery, because a crash while recovering is the case a
        single-fault matrix silently misses. Run the whole thing with the
        provider's idempotency window open and closed — with it closed, nothing is
        borrowed from Stripe's 24h dedup, so exactly-once has to be ours."""
        reached: set[str] = set()
        for window_open in (True, False):
            for first in ALL_KILL_POINTS:
                for second in ALL_KILL_POINTS:
                    where = f"[window_open={window_open}] kill {first} then {second}"
                    with self.subTest(window_open=window_open, first=first, second=second):
                        case = Case()
                        db = FakeDatabase()
                        stripe = FakeStripe(idempotency_window_open=window_open)
                        reached |= self._drive_to_completion(case, db, stripe, [first, second], where)

        missing = set(ALL_KILL_POINTS) - reached
        self.assertEqual(
            missing, set(),
            "declared kill points that the matrix never actually crossed — either "
            "unreachable code or a boundary the harness cannot inject",
        )

    def test_random_deep_crash_walks(self) -> None:
        """Longer interleavings than the pairwise matrix reaches: crash at a random
        boundary on every attempt until the budget runs out, then recover. Seeded,
        so a failure is reproducible."""
        rng = random.Random(20260801)
        for trial in range(400):
            window_open = rng.random() < 0.5
            depth = rng.randint(1, MAX_ATTEMPTS - 1)
            kills: list[str | None] = [rng.choice(ALL_KILL_POINTS) for _ in range(depth)]
            where = f"trial {trial} window_open={window_open} kills={kills}"
            with self.subTest(trial=trial):
                case = Case()
                db = FakeDatabase()
                stripe = FakeStripe(idempotency_window_open=window_open)
                self._drive_to_completion(case, db, stripe, kills, where)

    def test_death_during_the_recovery_refund_call_is_survivable(self) -> None:
        """The nastiest single case, called out by name: the first attempt commits
        its audit row and dies before paying; recovery re-drives the provider, the
        provider applies the refund, and recovery dies before it sees the response.
        The money is out the door and nobody in our process knows."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe(idempotency_window_open=False)
        first = run_attempt(case, db, stripe, "after_commit")
        self.assertFalse(first.survived)
        self.assertEqual(stripe.applied, [], "no money should have moved yet")
        self.assertEqual(len(committed_entries(db)), 1, "the audit row must already be durable")

        second = run_attempt(case, db, stripe, PROVIDER_DEATH)
        self.assertFalse(second.survived)
        self.assertTrue(second.provider_died)
        self.assertIn("replay_before_lookup", second.steps, "recovery must take the replay path")
        self.assertEqual(len(stripe.applied), 1, "the refund did apply at the provider")
        self.assert_money_invariant(case, db, stripe, where="after in-flight provider death")

        third = run_attempt(case, db, stripe, None)
        self.assertTrue(third.survived)
        self.assertEqual(len(stripe.applied), 1, "recovery must not pay a second time")
        self.assertEqual(len(stripe.create_calls), 1, "recovery must not even ask twice")

    def test_permanent_provider_failure_is_raised_as_an_incident(self) -> None:
        """The deliberate cost of committing the audit row before the money: a
        permanently failing provider leaves a row with no refund. The architecture
        says reconciliation turns that into an incident. Prove it, rather than
        asserting it in a document."""

        class _Broken(FakeStripe):
            def create_refund(self, **kw: object):  # type: ignore[override]
                raise RuntimeError("provider permanently rejected the refund")

        case, db, stripe = Case(), FakeDatabase(), _Broken()
        for _ in range(MAX_ATTEMPTS):
            conn = FakeConn(db)
            with self.assertRaises(RuntimeError):
                settle_refund(
                    SECRET, token=case.token, decision=case.decision, prepared=case.prepared,
                    conn=conn, store=PostgresIdempotencyStore(conn), gateway=stripe,
                    now_ts=NOW_TS, trace_id="trace-crash", api_version=API_VERSION,
                )

        self.assertEqual(stripe.applied, [], "no money moved")
        entries = committed_entries(db)
        self.assertEqual(len(entries), 1, "the audit row is committed exactly once, ahead of the money")
        self.assertTrue(verify_chain(entries))
        report = reconcile(
            entries, stripe.list_refunds_in_window(),
            window_start_ts=NOW_TS - 1, window_end_ts=NOW_TS + 1,
        )
        self.assertFalse(report.clean, "an audit row with no refund must not reconcile clean")
        self.assertEqual(report.unmatched_in_ledger, (case.idempotency_key,))
        self.assertEqual(report.unmatched_in_stripe, (), "and no money moved off-ledger")


class TestReplayAgainstADifferentAction(CrashMatrixAssertions):
    """The replay path assumes a taken reservation means "the same refund, again".

    That assumption is only as good as the key. The approval inbox derives the
    idempotency key from the ticket alone
    (``derive_idempotency_key(f"farthing-ticket-{t}", "execute_refund", {"ticket": t})``),
    so re-driving the same ticket — which the shipped demo does with a constant
    ``ticket_id``, and which Temporal's default id-reuse policy permits once a run
    has closed — produces the *same* key for a *different* action.

    These two cases ask what the recovery path does when the reservation it is
    replaying does not belong to the refund in hand.
    """

    def _settle(self, case: Case, db: FakeDatabase, stripe: FakeStripe) -> _Attempt:
        return run_attempt(case, db, stripe, None)

    def test_replay_on_a_different_charge_never_moves_money_off_ledger(self) -> None:
        """A later run of the same ticket against a NEW charge (a new invoice)
        replays a reservation that belongs to the old charge. The provider lookup
        is scoped to the new charge, so it finds nothing — and the fallback path
        creates a refund with no reservation of its own and no audit row."""
        first = Case(charge_id="ch_first", amount_minor=4200)
        db = FakeDatabase()
        stripe = FakeStripe(idempotency_window_open=False)  # a later billing period
        self.assertTrue(self._settle(first, db, stripe).survived)

        second = Case(charge_id="ch_second", amount_minor=3100)
        self.assertEqual(
            second.idempotency_key, first.idempotency_key,
            "precondition: the ticket-scoped key collides across runs",
        )
        outcome = self._settle(second, db, stripe)

        self.assertEqual(
            outcome.refused, "replay_action_drift",
            "a reservation spent on another charge must be refused, not resolved",
        )
        entries = committed_entries(db)
        self.assertTrue(verify_chain(entries))
        report = reconcile(
            entries, stripe.list_refunds_in_window(),
            window_start_ts=NOW_TS - 1, window_end_ts=NOW_TS + 1,
        )
        self.assertEqual(
            report.unmatched_in_stripe, (),
            "money moved with no committed audit row — an approved-but-invisible payment",
        )
        self.assertEqual(
            len(stripe.applied), 1,
            "the ticket now has more than one refund, against different charges",
        )

    def test_replay_at_a_different_amount_never_reports_a_false_success(self) -> None:
        """A later run of the same ticket at a DIFFERENT amount replays a
        reservation whose refund is for the old amount. Returning that refund
        reports a success for money that was never moved."""
        first = Case(amount_minor=4200)
        db, stripe = FakeDatabase(), FakeStripe()
        self.assertTrue(self._settle(first, db, stripe).survived)

        second = Case(amount_minor=3100)
        self.assertEqual(second.idempotency_key, first.idempotency_key)
        outcome = self._settle(second, db, stripe)

        self.assertEqual(len(stripe.applied), 1, "no second refund should exist")
        if outcome.refused is None:
            self.assertIsNotNone(outcome.result)
            assert outcome.result is not None
            self.assertEqual(
                outcome.result["amount"], second.amount_minor,
                "settlement reported success for an amount the provider never moved",
            )
        else:
            self.assertEqual(
                outcome.refused, "replay_action_drift",
                "a reservation spent on another amount must be refused, not resolved",
            )

    def test_a_second_approval_of_the_same_action_pays_once(self) -> None:
        """The approval-signal boundary, from the settlement side. If the approval
        service dies between issuing a token and signalling the workflow, the
        reviewer clicks again — and a second reviewer approving the same ticket
        looks identical from here: a different token, differently signed, for the
        same action. The key is derived from the ticket, so both land on the same
        reservation and only the first one's money moves."""
        # Both issued before the settlement clock and inside the freshness window;
        # a token issued after it is a separate refusal (future_token).
        first = Case(approver="alice@example.com", issued_ts=NOW_TS - 10)
        second = Case(approver="bob@example.com", issued_ts=NOW_TS - 5)
        self.assertEqual(second.idempotency_key, first.idempotency_key)
        self.assertNotEqual(second.token.signature, first.token.signature)

        db, stripe = FakeDatabase(), FakeStripe(idempotency_window_open=False)
        self.assertTrue(self._settle(first, db, stripe).survived)
        outcome = self._settle(second, db, stripe)

        self.assertTrue(outcome.survived, f"the second approval was refused: {outcome.refused}")
        self.assertEqual(len(stripe.applied), 1, "two approvals must not pay twice")
        self.assertEqual(len(stripe.create_calls), 1, "and must not even ask twice")
        entries = committed_entries(db)
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0].approver, "alice@example.com",
            "the audit row must name the approver whose authorization actually moved the money",
        )

    def test_a_reserved_key_with_no_audit_row_is_refused(self) -> None:
        """The reservation and its audit row commit together, so a reserved key
        without a row cannot happen — which is exactly why reaching it must fail
        loudly. Paying against a reservation nothing can account for is the worse
        of the two options."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        db.reserved_keys.append(case.idempotency_key)  # reservation with no row
        outcome = self._settle(case, db, stripe)
        self.assertEqual(outcome.refused, "replay_without_record")
        self.assertEqual(stripe.applied, [], "no money moved")

    def test_a_provider_refund_that_is_not_this_action_is_refused(self) -> None:
        """Defense in depth on the other side: if the provider's refund for this
        key is not the amount this settlement is for, report a failure rather than
        a success for money that was never moved."""
        case, db, stripe = Case(), FakeDatabase(), FakeStripe()
        self.assertTrue(self._settle(case, db, stripe).survived)
        # The provider's record drifts from ours after the fact.
        stripe.applied[0]["amount"] = case.amount_minor + 1
        outcome = self._settle(case, db, stripe)
        self.assertEqual(outcome.refused, "replay_refund_drift")


if __name__ == "__main__":
    unittest.main()
