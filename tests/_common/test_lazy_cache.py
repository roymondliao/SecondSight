"""Death tests + unit tests for LazyCacheWithLocking (GUR-116).

These tests pin the four invariants the consolidated two-level lazy cache MUST hold.
They are written BEFORE the implementation and MUST fail red (ImportError) until
`secondsight._common.lazy_cache.LazyCacheWithLocking` exists.

Invariants pinned:
  DT-1  Same key, concurrent first-init → exactly one materialiser invocation.
  DT-2  Different keys, no cross-coupling → K2 does not serialise on K1's slow path.
  DT-3  Materialiser failure → not cached; subsequent get(K) re-invokes the materialiser.
  DT-4  aclose() finaliser idempotency → finaliser fires exactly once per cached value;
        a second aclose() is a no-op.

Why these specifically: each one corresponds to a silent-rot path that a naive
"single global lock" or "cache-on-failure" implementation would still pass under
single-key happy-path tests but fail under realistic load.
"""

from __future__ import annotations

import asyncio

import pytest

from secondsight._common.lazy_cache import LazyCacheWithLocking


# ---------------------------------------------------------------------------
# DT-1: same key, concurrent first-init → exactly one materialiser invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_death_same_key_concurrent_first_init_invokes_materialiser_once() -> None:
    """50 concurrent get(K) for an unseen K must result in exactly ONE
    materialiser call and all 50 callers must see the same returned value.

    A single global lock implementation would pass this; a "no lock at all"
    implementation would fail with multiple materialiser invocations.
    """
    invocations = 0
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def materialise(key: str) -> object:
        nonlocal invocations
        invocations += 1
        # Block first invocation so concurrent callers pile up before the
        # cached value is published.
        started.set()
        await proceed.wait()
        return object()

    cache: LazyCacheWithLocking[str, object] = LazyCacheWithLocking(
        materialiser=materialise,
    )

    callers = [asyncio.create_task(cache.get("alpha")) for _ in range(50)]

    # Ensure at least one caller has entered the slow path before we release.
    await asyncio.wait_for(started.wait(), timeout=2.0)
    proceed.set()

    results = await asyncio.gather(*callers)

    assert invocations == 1, (
        f"Materialiser invoked {invocations} times for one key; "
        f"expected exactly 1 (concurrent first-init must serialise)."
    )
    first = results[0]
    for i, value in enumerate(results):
        assert value is first, f"Caller {i} got a different instance than caller 0."


# ---------------------------------------------------------------------------
# DT-2: different keys → no cross-coupling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_death_different_keys_do_not_serialise() -> None:
    """get(K1) holding the slow path (mid-materialise) must NOT block get(K2)
    from completing its own first-init.

    A single global lock implementation would FAIL this: K2 would block until
    K1 finishes. A correct per-key lock implementation lets K2 finish first.
    """
    k1_release = asyncio.Event()

    async def materialise(key: str) -> str:
        if key == "k1":
            await k1_release.wait()
            return "v1"
        return f"v_{key}"

    cache: LazyCacheWithLocking[str, str] = LazyCacheWithLocking(
        materialiser=materialise,
    )

    k1_task = asyncio.create_task(cache.get("k1"))
    # Yield so k1 enters its materialiser and is parked on k1_release.
    for _ in range(5):
        await asyncio.sleep(0)

    # k2 must NOT serialise behind k1.
    v2 = await asyncio.wait_for(cache.get("k2"), timeout=1.0)
    assert v2 == "v_k2"
    assert not k1_task.done(), (
        "k1 should still be parked; if it finished, the timing assumption is broken."
    )

    k1_release.set()
    v1 = await asyncio.wait_for(k1_task, timeout=1.0)
    assert v1 == "v1"


# ---------------------------------------------------------------------------
# DT-3: materialiser failure → not cached; retry re-invokes materialiser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_death_materialiser_failure_is_not_cached() -> None:
    """If the materialiser raises, the failing key MUST NOT be cached as a
    sentinel. A subsequent get(K) MUST re-invoke the materialiser.

    Caching failures would silently turn a transient error into a permanent
    poisoned slot for the lifetime of the process.
    """
    calls = 0

    class TransientError(RuntimeError):
        pass

    async def materialise(key: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientError("first attempt fails")
        return f"v_{key}"

    cache: LazyCacheWithLocking[str, str] = LazyCacheWithLocking(
        materialiser=materialise,
    )

    with pytest.raises(TransientError):
        await cache.get("alpha")

    # Second call MUST re-invoke materialiser, not return a cached failure.
    value = await cache.get("alpha")
    assert value == "v_alpha"
    assert calls == 2, f"Expected 2 materialiser calls, got {calls}."


# ---------------------------------------------------------------------------
# DT-4: aclose() finaliser idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_death_aclose_finaliser_runs_exactly_once_and_is_idempotent() -> None:
    """Two consecutive aclose() calls must not raise; the registered finaliser
    must fire exactly once per cached value, regardless of aclose() call count.

    A finaliser that fires twice on the same value can double-dispose
    DBEngines (close-on-closed errors). A finaliser that never fires leaks
    resources at shutdown.
    """
    finalised: list[tuple[str, str]] = []

    async def materialise(key: str) -> str:
        return f"v_{key}"

    def finalise(key: str, value: str) -> None:
        finalised.append((key, value))

    cache: LazyCacheWithLocking[str, str] = LazyCacheWithLocking(
        materialiser=materialise,
        finaliser=finalise,
    )

    await cache.get("a")
    await cache.get("b")

    await cache.aclose()
    await cache.aclose()  # idempotent: must not raise, must not double-fire.

    assert sorted(finalised) == [("a", "v_a"), ("b", "v_b")], (
        f"Finaliser results unexpected: {finalised!r}"
    )

    # After aclose, get() must raise.
    with pytest.raises(RuntimeError):
        await cache.get("c")


# ---------------------------------------------------------------------------
# Unit: async finaliser is awaited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_finaliser_is_awaited() -> None:
    """A coroutine finaliser must be awaited (otherwise resources leak as
    un-awaited coroutines and a RuntimeWarning is silently emitted)."""
    finalised: list[str] = []

    async def materialise(key: str) -> str:
        return f"v_{key}"

    async def finalise(key: str, value: str) -> None:
        await asyncio.sleep(0)
        finalised.append(key)

    cache: LazyCacheWithLocking[str, str] = LazyCacheWithLocking(
        materialiser=materialise,
        finaliser=finalise,
    )
    await cache.get("a")
    await cache.aclose()

    assert finalised == ["a"]


# ---------------------------------------------------------------------------
# DT-5: invalidate(key) drops the value but keeps the per-key lock alive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_death_invalidate_preserves_per_key_lock() -> None:
    """invalidate(K) must drop the cached value (forcing re-materialisation
    on next get) WITHOUT removing the per-key asyncio.Lock from
    ``_key_locks``. Removing + recreating the lock under concurrent
    invalidate+get would produce two independent locks for the same key,
    breaking mutual exclusion (split-brain).

    Pinning this invariant guards SessionTracker.reset_session() semantics:
    a bind() in flight on the old state's mutation lock must coexist with a
    fresh materialisation on the same per-key lock.
    """
    calls = 0

    async def materialise(key: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    cache: LazyCacheWithLocking[str, int] = LazyCacheWithLocking(
        materialiser=materialise,
    )

    await cache.get("k")
    assert calls == 1
    lock_before = cache._key_locks["k"]  # noqa: SLF001 — invariant probe.

    cache.invalidate("k")

    # Lock object identity preserved.
    assert cache._key_locks["k"] is lock_before, (  # noqa: SLF001
        "invalidate() must not remove the per-key lock object."
    )

    # Next get re-invokes the materialiser.
    await cache.get("k")
    assert calls == 2

    # Same lock still after re-materialisation.
    assert cache._key_locks["k"] is lock_before  # noqa: SLF001


# ---------------------------------------------------------------------------
# Unit: get() returns cached value cheaply on the fast path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_cached_value_after_first_init() -> None:
    """After first init, repeated get(K) must NOT re-invoke the materialiser."""
    calls = 0

    async def materialise(key: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    cache: LazyCacheWithLocking[str, int] = LazyCacheWithLocking(
        materialiser=materialise,
    )

    first = await cache.get("k")
    second = await cache.get("k")
    third = await cache.get("k")
    assert first == second == third == 1
    assert calls == 1
