"""cancel_and_wait: stopping a helper task without swallowing the caller's own cancellation."""

import asyncio

import pytest

from app.core.tasks import cancel_and_wait

pytestmark = pytest.mark.anyio


async def test_the_helper_is_cancelled_and_awaited() -> None:
    helper = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    await cancel_and_wait(helper)
    assert helper.cancelled()


async def test_a_helper_that_already_finished_is_fine() -> None:
    helper = asyncio.create_task(asyncio.sleep(0))
    await helper
    await cancel_and_wait(helper)
    assert helper.done()
    assert not helper.cancelled()


async def test_an_error_that_ended_the_helper_is_raised() -> None:
    async def failing() -> None:
        raise RuntimeError("boom")

    helper = asyncio.create_task(failing())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="boom"):
        await cancel_and_wait(helper)


async def test_a_cancellation_of_the_caller_while_it_waits_is_not_swallowed() -> None:
    """``contextlib.suppress(CancelledError)`` around the wait would swallow it, and the caller
    would carry on as if it had not been cancelled."""
    stopping = asyncio.Event()
    carried_on: list[bool] = []

    async def slow_to_stop() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await asyncio.Event().wait()  # a cleanup that takes a while

    async def caller() -> None:
        helper = asyncio.create_task(slow_to_stop())
        await asyncio.sleep(0)
        await cancel_and_wait(helper)
        carried_on.append(True)

    task = asyncio.create_task(caller())
    await stopping.wait()
    task.cancel()  # while the caller waits for the helper's cleanup
    with pytest.raises(asyncio.CancelledError):
        await task
    assert carried_on == []
