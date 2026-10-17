"""In-process pub/sub between parts. Topics are the hub event models in protocol.py.

    hub.publish(StepChanged(...))
    async for ev in hub.subscribe(StepChanged): ...

ponytail: single process, in-memory, drop-oldest queues. If parts ever run as separate
processes, swap this for Redis pub/sub behind the same two functions.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator

from pydantic import BaseModel

_subs: dict[type, set[asyncio.Queue]] = defaultdict(set)
_last: dict[tuple[type, str], BaseModel] = {}


def publish(ev: BaseModel) -> None:
    _last[(type(ev), getattr(ev, "session", ""))] = ev
    for q in list(_subs[type(ev)]):
        if q.full():  # slow consumer: drop oldest, keep newest (these are all "latest state" events)
            q.get_nowait()
        q.put_nowait(ev)


def last[T: BaseModel](kind: type[T], session: str = "") -> T | None:
    """Most recent event of this type for a session (late subscribers start from here)."""
    return _last.get((kind, session))  # type: ignore[return-value]


async def subscribe[T: BaseModel](kind: type[T], maxsize: int = 8) -> AsyncIterator[T]:
    q: asyncio.Queue = asyncio.Queue(maxsize)
    _subs[kind].add(q)
    try:
        while True:
            yield await q.get()
    finally:
        _subs[kind].discard(q)
