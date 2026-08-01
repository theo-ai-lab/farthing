"""Fault-injection harness: honest doubles for the two durable edges.

The point of this module is that the crash matrix exercises **production code**.
The real :class:`~farthing.idempotency_store.PostgresIdempotencyStore` and the
real :mod:`farthing.pg_ledger` run against :class:`FakeConn`, so the sequence
under test is the one that ships — not a re-implementation in a test file.

Two properties make the doubles honest rather than convenient:

* :class:`FakeConn` models the transaction semantics the ordering argument
  depends on. Uncommitted writes are visible to the writing transaction and are
  **lost when the process dies**. A fake that quietly persisted them would turn
  every "crash before commit" case green for the wrong reason.
* :class:`FakeStripe` records refunds that actually *applied* separately from
  create *calls*, so a double-create is visible even when the provider
  deduplicates it. It can also be run with its idempotency window **closed**,
  which is how we prove exactly-once is ours and not borrowed from Stripe.

``FakeConn.execute`` refuses SQL it does not recognize. If a production statement
changes shape, the harness fails loudly instead of silently returning no rows.
"""

from __future__ import annotations

from typing import Any

from farthing.domain import ActionType, Disposition, PolicyDecision, PreparedAction, RefundResult
from farthing.execution_guard import ApprovalToken, issue_approval_token
from farthing.idempotency import derive_idempotency_key
from farthing.ledger import LedgerEntry


class ProcessDied(BaseException):
    """Simulated process death.

    Deliberately a ``BaseException``: real death cannot be caught by an
    ``except Exception`` somewhere on the money path, and neither can this.
    """


class KillAt:
    """A step observer that kills the process at exactly one boundary, once."""

    def __init__(self, point: str | None = None) -> None:
        self.point = point
        self.fired = False
        self.seen: list[str] = []

    def __call__(self, step: str) -> None:
        self.seen.append(step)
        if step == self.point and not self.fired:
            self.fired = True
            raise ProcessDied(step)


# ---- Durable edge -----------------------------------------------------------

class _Result:
    def __init__(self, rows: list[tuple[Any, ...]], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeDatabase:
    """Committed state. Survives process death; a connection's pending writes do not."""

    def __init__(self) -> None:
        self.reserved_keys: list[str] = []
        self.ledger_rows: list[tuple[Any, ...]] = []  # tuples in pg_ledger._COLS order


class FakeConn:
    """The psycopg surface pg_ledger / PostgresIdempotencyStore actually use,
    with real transaction semantics. Discarding the object models process death:
    everything not committed is gone."""

    def __init__(self, db: FakeDatabase) -> None:
        self._db = db
        self._pending_keys: list[str] = []
        self._pending_rows: list[tuple[Any, ...]] = []

    # -- visibility: committed rows plus this transaction's own uncommitted ones
    def _visible_keys(self) -> list[str]:
        return self._db.reserved_keys + self._pending_keys

    def _visible_rows(self) -> list[tuple[Any, ...]]:
        return self._db.ledger_rows + self._pending_rows

    def execute(self, query: Any, params: Any = None, /) -> _Result:
        sql = " ".join(str(query).split())
        if sql.startswith("CREATE TABLE"):
            return _Result([])
        if sql.startswith("INSERT INTO farthing_idempotency"):
            key = str(params[0])
            if key in self._visible_keys():
                return _Result([], rowcount=0)
            self._pending_keys.append(key)
            return _Result([], rowcount=1)
        if sql.startswith("SELECT seq, entry_hash FROM farthing_ledger"):
            rows = self._visible_rows()
            if not rows:
                return _Result([])
            tail = max(rows, key=lambda r: int(r[0]))
            return _Result([(tail[0], tail[-1])])
        if sql.startswith("INSERT INTO farthing_ledger"):
            self._pending_rows.append(tuple(params))
            return _Result([], rowcount=1)
        if sql.startswith("SELECT") and "disposition = 'refunded'" in sql:
            start, end = params
            sel = [r for r in self._visible_rows() if r[4] == "refunded" and start <= r[1] <= end]
            return _Result(sorted(sel, key=lambda r: int(r[0])))
        raise AssertionError(f"FakeConn saw SQL it does not model: {sql!r}")

    def commit(self) -> None:
        self._db.reserved_keys.extend(self._pending_keys)
        self._db.ledger_rows.extend(self._pending_rows)
        self._pending_keys = []
        self._pending_rows = []


def committed_entries(db: FakeDatabase) -> list[LedgerEntry]:
    """Every committed ledger entry, in chain order, for verify_chain."""
    return [LedgerEntry(*row) for row in sorted(db.ledger_rows, key=lambda r: int(r[0]))]


# ---- Payment provider edge --------------------------------------------------

class FakeStripe:
    """A refund gateway that records what actually happened at the provider.

    ``applied`` is the money that really moved. ``create_calls`` is every request
    that reached the provider, so a duplicate create is visible even when the
    provider's idempotency window collapses it.
    """

    def __init__(self, *, idempotency_window_open: bool = True) -> None:
        self.idempotency_window_open = idempotency_window_open
        self.applied: list[RefundResult] = []
        self.create_calls: list[dict[str, Any]] = []
        self.lookups: list[dict[str, str]] = []
        self._charges: dict[str, str] = {}  # refund id -> the charge it landed on
        self._die_on_next_create = False
        self._n = 0

    def arm_death_on_next_create(self) -> None:
        """The next create applies at the provider and then the process dies
        before the response is ever observed — the classic in-flight death."""
        self._die_on_next_create = True

    # -- RefundGateway surface (structurally identical to ExecuteClient) --------
    def create_refund(self, *, charge_id: str, amount_minor: int, idempotency_key: str) -> RefundResult:
        self.create_calls.append(
            {"charge": charge_id, "amount": amount_minor, "idempotency_key": idempotency_key}
        )
        deduped: RefundResult | None = None
        if self.idempotency_window_open:
            for r in self.applied:
                if r["idempotency_key"] == idempotency_key:
                    deduped = r
                    break
        if deduped is None:
            self._n += 1
            deduped = RefundResult(
                id=f"re_{self._n}", amount=amount_minor, status="succeeded",
                currency=charge_currency(charge_id), idempotency_key=idempotency_key,
                api_version="2026-06-24.dahlia",
            )
            self.applied.append(deduped)
            self._charges[deduped["id"]] = charge_id
        if self._die_on_next_create:
            self._die_on_next_create = False
            raise ProcessDied("provider_create_applied_then_died")
        return deduped

    def find_refund_by_key(self, *, charge_id: str, idempotency_key: str) -> RefundResult | None:
        self.lookups.append({"charge": charge_id, "idempotency_key": idempotency_key})
        for r in self.applied:
            if r["idempotency_key"] == idempotency_key and self._charges.get(r["id"]) == charge_id:
                return r
        return None

    # -- reconciliation surface (mirrors PrepareClient.list_refunds_in_window) --
    def list_refunds_in_window(self) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "amount": r["amount"], "currency": r["currency"],
             "idempotency_key": r["idempotency_key"]}
            for r in self.applied
        ]

    def refunds_on(self, charge_id: str) -> list[RefundResult]:
        return [r for r in self.applied if self._charges.get(r["id"]) == charge_id]


def charge_currency(charge_id: str) -> str:
    """Every charge in the harness is USD; a refund always carries the charge's
    own currency, exactly as Stripe does."""
    del charge_id
    return "USD"


# ---- Case builder -----------------------------------------------------------

SECRET = b"crash-matrix-secret"
NOW_TS = 1_800_000_000
POLICY_VERSION = "2026.07.1"


class Case:
    """One approved refund, ready to settle: the signed token plus the decision
    and prepared action it is bound to."""

    def __init__(
        self,
        *,
        ticket_id: str = "t-crash",
        amount_minor: int = 4200,
        currency: str = "USD",
        charge_id: str = "ch_matrix",
        approver: str = "reviewer@example.com",
        key_ticket_id: str | None = None,
        issued_ts: int = NOW_TS,
    ) -> None:
        self.prepared = PreparedAction(
            ticket_id=ticket_id, action_type=ActionType.REFUND,
            subscription_id="sub_matrix", invoice_id="in_matrix",
            amount_minor=amount_minor, currency=currency,
            policy_version=POLICY_VERSION, charge_id=charge_id,
            reason_codes=("within_window",),
        )
        self.decision = PolicyDecision(
            disposition=Disposition.ELIGIBLE, amount_minor=amount_minor,
            currency=currency, reason_codes=("within_window",),
            policy_version=POLICY_VERSION,
        )
        # The production approval inbox derives the key from the ticket alone
        # (approval/app.py); mirror that exactly so the harness tests the real
        # key-scoping decision rather than an invented one.
        kt = key_ticket_id or ticket_id
        self.idempotency_key = derive_idempotency_key(
            f"farthing-ticket-{kt}", "execute_refund", {"ticket": kt},
        )
        self.token: ApprovalToken = issue_approval_token(
            SECRET, prepared=self.prepared, idempotency_key=self.idempotency_key,
            approver=approver, issued_ts=issued_ts,
        )
        self.charge_id = charge_id
        self.amount_minor = amount_minor
