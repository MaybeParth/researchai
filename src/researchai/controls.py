"""Pause/resume for a running session. Tools wait here before acting.

The agent can only be paused between tool calls (a model call already in flight finishes),
so pausing takes effect at the agent's next step.
"""

import asyncio


class Controls:
    def __init__(self) -> None:
        self._running = asyncio.Event()
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    async def wait_if_paused(self) -> None:
        await self._running.wait()
