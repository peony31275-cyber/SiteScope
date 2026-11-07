"""Small asyncio helpers with no dependencies (usable from every package, ``crawler`` included)."""

import asyncio
from typing import Any


async def cancel_and_wait(task: "asyncio.Task[Any]") -> None:
    """Cancel a helper task and wait until it has finished.

    The helper's own cancellation is absorbed, but a cancellation of the *calling* task while it
    waits is not: ``contextlib.suppress(CancelledError)`` around the wait would swallow that one
    too, and the caller would carry on as if it had not been cancelled. An error that ended the
    helper before the cancellation is re-raised.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
