from datetime import datetime, timedelta, timezone
import hashlib
import re
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from .errors import AppError
from .models import WritingAccount, WritingReservation, new_id, utcnow
from .serialization import stamp

DAILY_LIMIT = 9000
ATTEMPT_COST = 300
MIDNIGHT_GUARD = timedelta(minutes=5)


def account_scope(account_id: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
        raise AppError("The managed writing account is not configured correctly.", 422)
    return "cf:" + hashlib.sha256(account_id.lower().encode()).hexdigest()


def budget_days(at: datetime) -> tuple[str, str | None, datetime]:
    at = at.astimezone(timezone.utc)
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = at - start
    adjacent = start - timedelta(days=1) if elapsed < MIDNIGHT_GUARD else start + timedelta(days=1) if timedelta(days=1) - elapsed <= MIDNIGHT_GUARD else None
    return start.date().isoformat(), adjacent.date().isoformat() if adjacent else None, start + timedelta(days=1) + MIDNIGHT_GUARD


async def scoped_usage(session, scope: str, at: datetime) -> dict:
    current, adjacent, reset = budget_days(at)
    reserved = 0
    blocked_reset = None
    for day in [current] + ([adjacent] if adjacent else []):
        total = await session.scalar(select(func.coalesce(func.sum(WritingReservation.reserved_neurons), 0)).where(WritingReservation.account_scope == scope, or_(WritingReservation.utc_day == day, WritingReservation.adjacent_day == day)))
        reserved = max(reserved, int(total))
        if total + ATTEMPT_COST > DAILY_LIMIT:
            blocked = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1) + MIDNIGHT_GUARD
            blocked_reset = max(blocked_reset, blocked) if blocked_reset else blocked
    return {"reservedNeurons": reserved, "limitNeurons": DAILY_LIMIT, "remainingNeurons": max(0, DAILY_LIMIT - reserved), "resetsAt": stamp(blocked_reset or reset)}


async def read_budget(database, account_id: str, at: datetime | None = None) -> dict:
    try:
        async with database.sessions() as session:
            return await scoped_usage(session, account_scope(account_id), at or utcnow())
    except AppError:
        raise
    except Exception:
        raise AppError("The writing budget is unavailable. Writing has been stopped safely; please try again later.", 503) from None


async def reserve_attempt(database, account_id: str, at: datetime | None = None) -> dict:
    date = at or utcnow()
    scope = account_scope(account_id)
    try:
        async with database.sessions() as session:
            async with session.begin():
                insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
                await session.execute(insert(WritingAccount).values(account_scope=scope).on_conflict_do_nothing(index_elements=["account_scope"]))
                # A shared account row serializes the aggregate check + immutable
                # reservation INSERT across every API process and concurrent visitor.
                await session.scalar(select(WritingAccount).where(WritingAccount.account_scope == scope).with_for_update())
                usage = await scoped_usage(session, scope, date)
                if usage["remainingNeurons"] < ATTEMPT_COST:
                    raise AppError(f"The shared free writing allowance is used up for today. Try again after {usage['resetsAt'].replace('T', ' ').replace('.000Z', ' UTC')}. Your queued articles and saved drafts are safe.", 429)
                current, adjacent, _ = budget_days(date)
                session.add(WritingReservation(id=new_id(), account_scope=scope, utc_day=current, adjacent_day=adjacent, reserved_neurons=ATTEMPT_COST, created_at=date))
                await session.flush()
                result = await scoped_usage(session, scope, date)
            return result
    except AppError:
        raise
    except Exception:
        raise AppError("The writing budget is unavailable. Writing has been stopped safely; please try again later.", 503) from None
