"""Generic two-level lazy cache with per-key initialisation locking.

Consolidates a pattern that previously lived (in identical form) at three
sites: ProjectRegistry, SessionTracker, and AppState's per-project tracker
cache. Each site needs the same shape: a fast lock-free dict lookup on the
hot path, a per-key asyncio.Lock guarding first-init, and a single brief
guard around the per-key lock dict itself.

Design assumptions (the single source of truth for these now lives here, not
duplicated at each call site):

- Single asyncio event loop / single uvicorn worker. ``asyncio.Lock`` is NOT
  cross-process safe; multi-worker deployments invalidate the per-key locking
  guarantee. SecondSight Phase 1 is single-process by contract.
- CPython's GIL makes ``dict.__contains__`` atomic for hashable keys, AND acts
  as a full memory barrier between coroutines. The fast-path read at
  ``key in self._values`` is guaranteed to observe a fully-initialised value
  written by the slow path. A free-threaded Python build (PEP 703 / no-GIL)
  invalidates BOTH the atomicity and the memory-ordering assumption — every
  caller of this utility must be redesigned before running under no-GIL.

If these assumptions stop holding, the first thing to rot is: concurrent
first-init for the same key produces multiple materialised values racing for
the same slot — the loser's value is silently dropped, but its side-effects
(DBEngine WAL races, warm_start side reads) are not.

Failure semantics worth noting:

- A materialiser that raises does NOT cache the failing key. The next
  ``get(key)`` re-invokes the materialiser. This is intentional: caching
  failures would silently turn a transient error into a permanent poisoned
  slot.
- ``aclose()`` is idempotent. The optional finaliser fires exactly once per
  cached value across any number of ``aclose()`` calls. After ``aclose()``,
  ``get()`` raises ``RuntimeError``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

Materialiser = Callable[[K], Awaitable[V]]
"""Async callable that constructs the value for an unseen key.

Invoked at most once per key under normal operation. If it raises, the
exception propagates to the caller of ``get(key)`` and the key is NOT
recorded — a subsequent ``get(key)`` re-invokes the materialiser.
"""

Finaliser = Callable[[K, V], Awaitable[None] | None]
"""Optional callable invoked on each cached (key, value) pair during ``aclose()``.

May be sync or async. Exceptions raised by the finaliser are propagated to
the first caller; remaining values are not finalised. Callers that want
best-effort multi-value cleanup should wrap their finaliser with their own
try/except.
"""


class LazyCacheWithLocking(Generic[K, V]):
    """Async lazy cache: fast-path dict read, slow-path per-key init lock.

    The slow path uses two locks:

    1. ``self._locks_guard`` — held only while creating the per-key
       ``asyncio.Lock`` for an unseen key (a brief dict insertion).
    2. ``self._key_locks[key]`` — the per-key materialisation lock; held
       across the materialiser call so concurrent ``get(key)`` for an unseen
       key invokes the materialiser exactly once.

    The fast path (``key in self._values``) holds neither lock; it relies on
    the GIL atomicity assumption documented at the module level.

    Type parameters:
        K: Hashable key type.
        V: Cached value type.
    """

    def __init__(
        self,
        materialiser: Materialiser[K, V],
        *,
        finaliser: Finaliser[K, V] | None = None,
    ) -> None:
        self._materialiser = materialiser
        self._finaliser = finaliser
        self._values: dict[K, V] = {}
        self._key_locks: dict[K, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock = asyncio.Lock()
        self._closed: bool = False

    async def get(self, key: K) -> V:
        """Return the cached value for ``key``, materialising on first sight.

        Concurrent ``get(key)`` calls for the same unseen key share one
        materialiser invocation. Calls for different keys do not serialise
        on each other.

        Raises:
            RuntimeError: if ``aclose()`` has been called.
            Exception: any exception raised by the materialiser propagates
                to the caller. The key is NOT cached on failure.
        """
        if self._closed:
            raise RuntimeError("LazyCacheWithLocking is closed; cannot serve get().")

        # Fast path — see module docstring for the GIL atomicity assumption
        # that makes this lock-free read safe.
        if key in self._values:
            return self._values[key]

        # Slow path: ensure a per-key lock exists, then materialise under it.
        async with self._locks_guard:
            if key not in self._key_locks:
                self._key_locks[key] = asyncio.Lock()
        key_lock = self._key_locks[key]

        async with key_lock:
            # Double-check inside the per-key lock: another coroutine may
            # have materialised the value while we were waiting.
            if key in self._values:
                return self._values[key]

            value = await self._materialiser(key)
            self._values[key] = value
            return value

    def invalidate(self, key: K) -> None:
        """Drop the cached value for ``key`` without touching the per-key lock.

        Synchronous and non-locking. Any in-flight materialisation that
        already holds the per-key lock will complete normally and re-cache
        its value; the next ``get(key)`` after that completion sees it.

        The per-key ``asyncio.Lock`` is intentionally NOT removed: a
        concurrent ``get(key)`` may hold it, and removing+recreating would
        produce two independent locks for the same key, breaking mutual
        exclusion during the overlap window.
        """
        self._values.pop(key, None)

    async def aclose(self) -> None:
        """Mark the cache closed and run the finaliser over each cached value.

        Idempotent: a second call returns immediately and does not re-fire
        the finaliser. After ``aclose()``, ``get()`` raises ``RuntimeError``.
        """
        if self._closed:
            return
        self._closed = True

        if self._finaliser is None:
            return

        for key, value in list(self._values.items()):
            result = self._finaliser(key, value)
            if inspect.isawaitable(result):
                await result


__all__ = ["Finaliser", "LazyCacheWithLocking", "Materialiser"]
