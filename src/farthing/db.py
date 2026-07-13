"""Structural type for the psycopg3 connection surface the edge adapters use.

The durable adapters (:mod:`farthing.pg_ledger`, :mod:`farthing.dead_letter`,
:mod:`farthing.idempotency_store`) take an injected connection rather than opening
their own, so they stay unit-testable against an in-memory fake. This Protocol is
the minimal surface they rely on: a live ``psycopg.Connection`` satisfies it
structurally, and so does the test fake — neither adapter needs to import psycopg.
"""

from __future__ import annotations

from typing import Any, Protocol


class DBConn(Protocol):
    """The ``execute`` + ``commit`` slice of a psycopg3 connection. ``execute``
    returns the statement handle (``.fetchone()`` / ``.fetchall()`` / ``.rowcount``);
    it is typed ``Any`` here because that handle's row shape is query-specific and
    is narrowed by the caller into a typed domain object immediately."""

    def execute(self, query: Any, params: Any = ..., /) -> Any: ...

    def commit(self) -> None: ...
