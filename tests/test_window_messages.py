import asyncio
import threading

import pytest

from wizwalker.window_messages import forget_window, send_message


class Window:
    """A window's message handling: records what it was sent, and can stop answering."""

    def __init__(self):
        self.handled = []
        self.threads = set()
        self.answering = threading.Event()
        self.answering.set()

    def __call__(self, handle, message, wparam, lparam):
        self.answering.wait(5)
        self.threads.add(threading.get_ident())
        self.handled.append((handle, message))
        return message


@pytest.fixture
def window():
    fake = Window()
    yield fake
    fake.answering.set()
    for handle in (101, 102):
        forget_window(handle)


async def test_a_window_handles_its_messages_in_order_off_the_loop(window):
    results = [await send_message(window, 101, message) for message in (1, 2, 3)]
    assert results == [1, 2, 3]
    assert window.handled == [(101, 1), (101, 2), (101, 3)]
    assert threading.get_ident() not in window.threads


async def test_a_window_not_answering_holds_up_only_its_own_messages(window):
    window.answering.clear()
    stuck = asyncio.ensure_future(send_message(window, 101, 1))
    await asyncio.sleep(0.05)  # the loop runs on meanwhile
    other = Window()
    assert await send_message(other, 102, 2) == 2
    assert not stuck.done()
    window.answering.set()
    assert await stuck == 1


async def test_a_cancelled_send_is_still_delivered_before_what_follows_it(window):
    window.answering.clear()
    first = asyncio.ensure_future(send_message(window, 101, 1))
    second = asyncio.ensure_future(send_message(window, 101, 2))
    await asyncio.sleep(0.01)
    first.cancel()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    after = asyncio.ensure_future(send_message(window, 101, 3))
    window.answering.set()
    await after
    assert window.handled == [(101, 1), (101, 2), (101, 3)], "the queued one was not dropped"


async def test_a_forgotten_window_can_be_sent_to_again(window):
    await send_message(window, 101, 1)
    forget_window(101)
    forget_window(101)  # twice is harmless
    assert await send_message(window, 101, 2) == 2
