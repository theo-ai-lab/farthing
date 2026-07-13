from __future__ import annotations

import unittest
from dataclasses import replace

from farthing.domain import ActionType, Disposition, PolicyDecision, PreparedAction
from farthing.execution_guard import (
    ExecutionRefused,
    InMemoryIdempotencyStore,
    authorize_execution,
    issue_approval_token,
)

SECRET = b"approval-service-secret"
POLICY_VERSION = "2026.07.1"
NOW = 1000


def _eligible(amount: int = 5000) -> tuple[PolicyDecision, PreparedAction]:
    decision = PolicyDecision(Disposition.ELIGIBLE, amount, "USD", ("ok",), POLICY_VERSION)
    prepared = PreparedAction(
        ticket_id="t1", action_type=ActionType.REFUND, subscription_id="sub",
        invoice_id="in", amount_minor=amount, currency="USD",
        policy_version=POLICY_VERSION, charge_id="ch_test",
    )
    return decision, prepared


def _token(prepared: PreparedAction, key: str = "frthg_k", issued: int = NOW):
    return issue_approval_token(
        SECRET, prepared=prepared, idempotency_key=key, approver="alice", issued_ts=issued
    )


class TestExecutionGuard(unittest.TestCase):
    def test_happy_path_authorizes(self) -> None:
        decision, prepared = _eligible()
        instr = authorize_execution(
            SECRET, token=_token(prepared), decision=decision, prepared=prepared,
            store=InMemoryIdempotencyStore(), now_ts=NOW,
        )
        self.assertEqual(instr.amount_minor, 5000)
        self.assertEqual(instr.approver, "alice")
        self.assertEqual(instr.action_type, "refund")

    def test_forged_amount_fails_signature(self) -> None:
        decision, prepared = _eligible()
        tok = _token(prepared)
        forged = replace(tok, amount_minor=999999)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=forged, decision=replace(decision, amount_minor=999999),
                prepared=replace(prepared, amount_minor=999999),
                store=InMemoryIdempotencyStore(), now_ts=NOW,
            )
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_amount_drift_between_token_and_decision(self) -> None:
        decision, prepared = _eligible(5000)
        tok = _token(replace(prepared, amount_minor=4000))
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=tok, decision=decision, prepared=prepared,
                store=InMemoryIdempotencyStore(), now_ts=NOW,
            )
        self.assertIn(ctx.exception.code, ("amount_drift", "preview_drift"))

    def test_replay_blocked_by_prereserved_key(self) -> None:
        decision, prepared = _eligible()
        store = InMemoryIdempotencyStore()
        store.reserve("frthg_k")  # already executed
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=_token(prepared), decision=decision, prepared=prepared,
                store=store, now_ts=NOW,
            )
        self.assertEqual(ctx.exception.code, "replay")

    def test_check_and_set_is_atomic_second_call_refused(self) -> None:
        # The real fix: a single store, two authorizations of the same token —
        # the first reserves and succeeds, the second is refused.
        decision, prepared = _eligible()
        store = InMemoryIdempotencyStore()
        tok = _token(prepared)
        authorize_execution(SECRET, token=tok, decision=decision, prepared=prepared,
                            store=store, now_ts=NOW)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(SECRET, token=tok, decision=decision, prepared=prepared,
                                store=store, now_ts=NOW)
        self.assertEqual(ctx.exception.code, "replay")

    def test_refused_authorization_does_not_consume_key(self) -> None:
        # A bad-signature refusal must NOT reserve the key (reservation is last).
        decision, prepared = _eligible()
        store = InMemoryIdempotencyStore()
        bad = replace(_token(prepared), signature="deadbeef")
        with self.assertRaises(ExecutionRefused):
            authorize_execution(SECRET, token=bad, decision=decision, prepared=prepared,
                                store=store, now_ts=NOW)
        # The key is still free, so a later valid authorization can use it.
        instr = authorize_execution(SECRET, token=_token(prepared), decision=decision,
                                    prepared=prepared, store=store, now_ts=NOW)
        self.assertEqual(instr.amount_minor, 5000)

    def test_expired_token_refused(self) -> None:
        decision, prepared = _eligible()
        tok = _token(prepared, issued=NOW)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(SECRET, token=tok, decision=decision, prepared=prepared,
                                store=InMemoryIdempotencyStore(), now_ts=NOW + 901)
        self.assertEqual(ctx.exception.code, "expired")

    def test_future_token_refused(self) -> None:
        decision, prepared = _eligible()
        tok = _token(prepared, issued=NOW + 500)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(SECRET, token=tok, decision=decision, prepared=prepared,
                                store=InMemoryIdempotencyStore(), now_ts=NOW)
        self.assertEqual(ctx.exception.code, "future_token")

    def test_non_eligible_never_executes(self) -> None:
        decision = PolicyDecision(Disposition.ESCALATE, 0, "USD", ("x",), POLICY_VERSION)
        _, prepared = _eligible(0)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=_token(replace(prepared, amount_minor=0)),
                decision=decision, prepared=replace(prepared, amount_minor=0),
                store=InMemoryIdempotencyStore(), now_ts=NOW,
            )
        self.assertEqual(ctx.exception.code, "not_eligible")

    def test_action_type_binding(self) -> None:
        decision, prepared = _eligible()
        tok = _token(prepared)  # signed as refund
        # Swap the prepared action to cancellation without re-signing.
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=tok, decision=decision,
                prepared=replace(prepared, action_type=ActionType.CANCELLATION),
                store=InMemoryIdempotencyStore(), now_ts=NOW,
            )
        # Signature covers action_type, so this trips bad_signature first — either
        # way it is refused, which is the point.
        self.assertIn(ctx.exception.code, ("bad_signature", "action_mismatch"))

    def test_charge_binding(self) -> None:
        # Token is signed for charge ch_1; executing against ch_2 must be refused.
        decision, prepared = _eligible()
        prepared = replace(prepared, charge_id="ch_1")
        tok = _token(prepared)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=tok, decision=decision,
                prepared=replace(prepared, charge_id="ch_2"),
                store=InMemoryIdempotencyStore(), now_ts=NOW,
            )
        self.assertIn(ctx.exception.code, ("bad_signature", "charge_mismatch"))

    def test_bound_charge_flows_to_instruction(self) -> None:
        decision, prepared = _eligible()
        prepared = replace(prepared, charge_id="ch_bound")
        instr = authorize_execution(
            SECRET, token=_token(prepared), decision=decision, prepared=prepared,
            store=InMemoryIdempotencyStore(), now_ts=NOW,
        )
        self.assertEqual(instr.charge_id, "ch_bound")

    def test_refund_without_bound_charge_refused(self) -> None:
        # A refund whose signed charge is None must fail closed, never fall back
        # to an unsigned charge.
        decision, prepared = _eligible()
        prepared = replace(prepared, charge_id=None)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(
                SECRET, token=_token(prepared), decision=decision, prepared=prepared,
                store=InMemoryIdempotencyStore(), now_ts=NOW,
            )
        self.assertEqual(ctx.exception.code, "unbound_charge")

    def test_wrong_secret_rejected(self) -> None:
        decision, prepared = _eligible()
        tok = issue_approval_token(b"other-secret", prepared=prepared,
                                   idempotency_key="frthg_k", approver="alice", issued_ts=NOW)
        with self.assertRaises(ExecutionRefused) as ctx:
            authorize_execution(SECRET, token=tok, decision=decision, prepared=prepared,
                                store=InMemoryIdempotencyStore(), now_ts=NOW)
        self.assertEqual(ctx.exception.code, "bad_signature")


if __name__ == "__main__":
    unittest.main()
