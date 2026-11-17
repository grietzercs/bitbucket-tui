"""Lazy pager used by list screens: loads one page on demand."""

from __future__ import annotations

from typing import Awaitable, Callable, Generic, List, Optional, TypeVar

from .models import Page

T = TypeVar("T")

FetchPage = Callable[[int, int], Awaitable[Page[T]]]


class Pager(Generic[T]):
    """Accumulates items from a Bitbucket paged endpoint.

    ``fetch(start, limit)`` must return a :class:`Page`. ``load_next`` loads at
    most one page; the caller decides when (e.g. when the cursor nears the end).
    """

    def __init__(self, fetch: FetchPage, limit: int = 50) -> None:
        self._fetch = fetch
        self.limit = limit
        self.items: List[T] = []
        self.next_start: Optional[int] = 0
        self.pages_loaded = 0

    @property
    def exhausted(self) -> bool:
        return self.next_start is None

    def reset(self, fetch: Optional[FetchPage] = None) -> None:
        if fetch is not None:
            self._fetch = fetch
        self.items = []
        self.next_start = 0
        self.pages_loaded = 0

    async def load_next(self) -> List[T]:
        if self.next_start is None:
            return []
        start = self.next_start
        page = await self._fetch(start, self.limit)
        self.items.extend(page.values)
        self.pages_loaded += 1
        nxt = None if page.is_last_page else page.next_page_start
        if nxt is not None and nxt <= start:
            # Server returned a non-advancing cursor; stop rather than loop forever.
            nxt = None
        self.next_start = nxt
        return list(page.values)
