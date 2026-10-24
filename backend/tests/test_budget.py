import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.budget import ATTEMPT_COST, DAILY_LIMIT, account_scope, read_budget, reserve_attempt
from app.errors import AppError
from app.models import WritingReservation, new_id
from .helpers import ACCOUNT_ID


def at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def test_postgresql_serializes_concurrent_account_budget(database):
    assert database.engine.dialect.name == "postgresql", "Critical concurrency tests must use real PostgreSQL."
    date = at("2026-10-24T12:00:00Z")
    results = await asyncio.gather(*(reserve_attempt(database, ACCOUNT_ID, date) for _ in range(40)), return_exceptions=True)
    accepted = [result for result in results if isinstance(result, dict)]
    rejected = [result for result in results if isinstance(result, Exception)]
    assert len(accepted) == 30 and len(rejected) == 10
    assert all(isinstance(error, AppError) and error.status == 429 for error in rejected)
    usage = await read_budget(database, ACCOUNT_ID, date)
    assert usage["reservedNeurons"] == DAILY_LIMIT == 9000 and usage["remainingNeurons"] == 0
    assert ATTEMPT_COST == 300 and account_scope(ACCOUNT_ID.upper()) == account_scope(ACCOUNT_ID)
    async with database.sessions() as session:
        reservations = (await session.scalars(select(WritingReservation))).all()
        assert len(reservations) == 30
        assert all(row.reserved_neurons == 300 and row.account_scope != ACCOUNT_ID for row in reservations)


async def test_midnight_reservations_are_atomic_across_both_utc_days(database):
    before, after = at("2026-10-24T23:58:00Z"), at("2026-10-25T00:02:00Z")
    results = await asyncio.gather(*(reserve_attempt(database, ACCOUNT_ID, before if index % 2 else after) for index in range(35)), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 30
    assert all(isinstance(result, dict) or isinstance(result, AppError) and result.status == 429 for result in results)
    for date in [before, after, at("2026-10-25T05:32:00+05:30")]:
        usage = await read_budget(database, ACCOUNT_ID, date)
        assert usage["reservedNeurons"] == 9000
    assert (await read_budget(database, ACCOUNT_ID, after))["resetsAt"] == "2026-10-26T00:05:00.000Z"
    with pytest.raises(AppError) as error:
        await reserve_attempt(database, ACCOUNT_ID, after)
    assert error.value.status == 429
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WritingReservation)) == 30


async def test_new_utc_day_and_database_fixed_cost_constraint(database):
    for _ in range(30):
        await reserve_attempt(database, ACCOUNT_ID, at("2026-10-24T12:00:00Z"))
    with pytest.raises(AppError):
        await reserve_attempt(database, ACCOUNT_ID, at("2026-10-25T00:02:00Z"))
    usage = await reserve_attempt(database, ACCOUNT_ID, at("2026-10-25T00:06:00Z"))
    assert usage["reservedNeurons"] == 300 and usage["remainingNeurons"] == 8700
    async with database.sessions() as session:
        session.add(WritingReservation(id=new_id(), account_scope=account_scope(ACCOUNT_ID), utc_day="2026-10-25", adjacent_day=None, reserved_neurons=301, created_at=datetime.now(timezone.utc)))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
