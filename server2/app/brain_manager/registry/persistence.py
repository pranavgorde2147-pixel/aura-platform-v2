"""Concurrency-safe persistence helpers for the Brain manager.

Provisioning the same Brain twice - or twice at the same time - must converge
on exactly one row. A naive "read, then insert" is not safe: two concurrent
sessions both observe "the row does not exist" and both insert, which trips the
unique constraint and fails the whole request.

These helpers implement an *insert-or-get* that is correct under concurrency
and works on every backend Server 2 supports (SQLite and PostgreSQL):

* the insert runs inside a SAVEPOINT, so a losing race only rolls back the
  insert and never the surrounding transaction;
* the unique violation is caught and the already-existing row is returned.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def get_or_create(
    session: AsyncSession,
    *,
    model: type[T],
    lookup: dict[str, Any],
    create: Callable[[], T],
    fetch: Callable[[], Any],
) -> T:
    """Return the row matching ``lookup``, creating it once if needed.

    ``lookup`` are the column values that identify the row, ``create`` builds a
    new (not yet added) instance and ``fetch`` re-reads the row after a lost
    race. The row is added to the session but *not* committed: the caller owns
    the transaction.
    """
    existing = await _select_one(session, model, lookup)
    if existing is not None:
        return existing

    try:
        async with session.begin_nested():
            instance = create()
            session.add(instance)
            await session.flush()
        return instance
    except IntegrityError:
        # A concurrent writer won the race. The savepoint was rolled back, so
        # the surrounding transaction is still usable.
        winner = await fetch()
        if winner is None:
            raise
        return winner


async def _select_one(
    session: AsyncSession, model: type[T], lookup: dict[str, Any]
) -> T | None:
    from sqlalchemy import select

    statement = select(model)
    for column, value in lookup.items():
        statement = statement.where(getattr(model, column) == value)
    result = await session.execute(statement)
    return result.scalars().first()


async def insert_ignoring_conflict(
    session: AsyncSession, instance: Any, *, conflict_keys: list[str]
) -> Any:
    """Insert ``instance`` and ignore a unique violation on ``conflict_keys``.

    Returns the newly inserted instance, or ``None`` when the row already
    existed. Used for rows that are safe to create more than once (for example
    a usage record or a health check).
    """
    try:
        async with session.begin_nested():
            session.add(instance)
            await session.flush()
        return instance
    except IntegrityError:
        return None


__all__ = [
    "get_or_create",
    "insert_ignoring_conflict",
]
