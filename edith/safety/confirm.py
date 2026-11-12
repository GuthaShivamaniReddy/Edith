"""Asking the user: confirmations and clarifying questions.

The brain registers a pending confirmation and emits an event to whichever client
is active; the client answers through resolve()/answer(). Silence is a no: an
unanswered confirmation times out as denied. stop() releases everything pending.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal

from edith.observability.logging import get_logger

log = get_logger(__name__)

GrantScope = Literal["once", "session"]


@dataclass(frozen=True)
class ConfirmResponse:
    approved: bool
    scope: GrantScope = "once"
    timed_out: bool = False


DENIED = ConfirmResponse(approved=False)


class ConfirmationBroker:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self._confirms: dict[str, tuple[str, asyncio.Future[ConfirmResponse]]] = {}
        self._questions: dict[str, tuple[str, asyncio.Future[dict[str, str] | None]]] = {}

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    async def wait_confirm(self, confirm_id: str, session_id: str) -> ConfirmResponse:
        fut: asyncio.Future[ConfirmResponse] = asyncio.get_running_loop().create_future()
        self._confirms[confirm_id] = (session_id, fut)
        try:
            return await asyncio.wait_for(fut, self.timeout_s)
        except TimeoutError:
            log.info("confirmation_timed_out", confirm_id=confirm_id)
            return ConfirmResponse(approved=False, timed_out=True)
        finally:
            self._confirms.pop(confirm_id, None)

    async def wait_answer(self, question_id: str, session_id: str) -> dict[str, str] | None:
        fut: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
        self._questions[question_id] = (session_id, fut)
        try:
            return await asyncio.wait_for(fut, self.timeout_s)
        except TimeoutError:
            return None
        finally:
            self._questions.pop(question_id, None)

    def resolve(self, confirm_id: str, approved: bool, scope: GrantScope = "once") -> bool:
        entry = self._confirms.get(confirm_id)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(ConfirmResponse(approved=approved, scope=scope))
        return True

    def answer(self, question_id: str, answers: dict[str, str] | None) -> bool:
        entry = self._questions.get(question_id)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(answers)
        return True

    def cancel_session(self, session_id: str | None = None) -> int:
        """Deny every pending confirmation/question (for one session, or all). Returns count."""
        n = 0
        for _sid, fut in [*self._confirms.values()]:
            if (session_id is None or _sid == session_id) and not fut.done():
                fut.set_result(DENIED)
                n += 1
        for _sid, qfut in [*self._questions.values()]:
            if (session_id is None or _sid == session_id) and not qfut.done():
                qfut.set_result(None)
                n += 1
        return n

    def pending_ids(self) -> list[str]:
        return [*self._confirms, *self._questions]
