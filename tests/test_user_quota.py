"""The per-user quota, and the one property that distinguishes it from the ingestion
limiter: it fails CLOSED.

Real Postgres, because the whole correctness argument is in one SQL statement — the
increment and the total must not race, and the data-modifying CTE's own row must be
counted. A mocked connection would test the mock.
"""

import pytest

from app.services import user_quota
from app.services.user_quota import QuotaExceededError, consume, release

USER = "usr_quota_test"
RESOURCE = "vlm_verify"


async def test_a_claim_returns_the_running_total(db_pool):
    assert await consume(db_pool, USER, RESOURCE, limit=5) == 1
    assert await consume(db_pool, USER, RESOURCE, limit=5) == 2
    assert await consume(db_pool, USER, RESOURCE, limit=5) == 3


async def test_the_ctes_own_row_is_counted(db_pool):
    """The bug the split sum exists to prevent.

    A data-modifying CTE and the rest of its statement share one snapshot, so a plain
    `SELECT sum(...)` would not see the row just inserted and would undercount by
    exactly this request — letting every caller through one over the limit, forever.
    """
    assert await consume(db_pool, USER, RESOURCE, limit=10) == 1


async def test_the_limit_is_enforced(db_pool):
    for _ in range(3):
        await consume(db_pool, USER, RESOURCE, limit=3)
    with pytest.raises(QuotaExceededError) as exc:
        await consume(db_pool, USER, RESOURCE, limit=3)
    assert exc.value.limit == 3
    assert exc.value.resource == RESOURCE


async def test_a_refused_claim_does_not_burn_allowance(db_pool):
    """A refusal must not consume the unit it refused, or one over-limit attempt
    would permanently cost the caller a call it never made."""
    for _ in range(2):
        await consume(db_pool, USER, RESOURCE, limit=2)
    with pytest.raises(QuotaExceededError):
        await consume(db_pool, USER, RESOURCE, limit=2)
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT sum(count) FROM user_quota WHERE user_id = $1 AND resource = $2",
            USER, RESOURCE,
        )
    assert total == 2


async def test_a_zero_limit_refuses_everything(db_pool):
    # Not "unlimited". rate_limit.py treats an unknown resource as unlimited, which is
    # the opposite policy and exactly what must not be inherited here.
    with pytest.raises(QuotaExceededError):
        await consume(db_pool, USER, RESOURCE, limit=0)


async def test_quotas_are_per_user_and_per_resource(db_pool):
    await consume(db_pool, USER, RESOURCE, limit=1)
    # A different user is unaffected...
    assert await consume(db_pool, "usr_someone_else", RESOURCE, limit=1) == 1
    # ...and so is a different resource for the same user.
    assert await consume(db_pool, USER, "other_resource", limit=1) == 1


async def test_release_hands_a_unit_back(db_pool):
    await consume(db_pool, USER, RESOURCE, limit=2)
    await release(db_pool, USER, RESOURCE)
    # The unit came back, so the next claim is the first again.
    assert await consume(db_pool, USER, RESOURCE, limit=2) == 1


async def test_release_never_goes_negative(db_pool):
    """A concurrent prune can remove the bucket; a negative count would corrupt
    every later total in the window."""
    await release(db_pool, USER, RESOURCE, count=5)
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COALESCE(sum(count), 0) FROM user_quota WHERE user_id = $1", USER
        )
    assert total >= 0


async def test_it_fails_closed_when_the_accounting_itself_fails(db_pool, monkeypatch):
    """THE PROPERTY THIS MODULE EXISTS FOR.

    app/middleware/rate_limit.py catches the same class of error and returns
    "allowed" — correct for ingestion, where dropping a drive because the database
    hiccuped is worse than letting a request through. For an outbound paid API call
    the opposite holds: an unknown quota has to be treated as an exhausted one.
    """
    broken = "SELECT 1 FROM does_not_exist_" + "x" * 8
    monkeypatch.setattr(user_quota, "_CONSUME_SQL", broken)
    with pytest.raises(QuotaExceededError):
        await consume(db_pool, USER, RESOURCE, limit=1000)


async def test_prune_removes_only_expired_buckets(db_pool):
    await consume(db_pool, USER, RESOURCE, limit=5)
    async with db_pool.acquire() as conn:
        await conn.execute(user_quota.PRUNE_SQL)
        remaining = await conn.fetchval(
            "SELECT count(*) FROM user_quota WHERE user_id = $1", USER
        )
    # The current bucket is inside the window, so it survives.
    assert remaining == 1
