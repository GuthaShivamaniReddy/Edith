"""Brain interface (PLAN.md section 6).

Keeping the Agent SDK behind this interface is what makes it swappable
(LocalBrain on Ollama in Phase 6; a plain Messages API loop if the SDK breaks us).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Self

from edith.core.events import Event, Request


class BrainError(RuntimeError):
    pass


class Brain(ABC):
    """A conversational session. Use as an async context manager."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def run(self, request: Request) -> AsyncIterator[Event]:
        """Stream events for one request. Ends after a FinalEvent or ErrorEvent."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the reply in progress (the 'Edith, stop' kill switch)."""

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
