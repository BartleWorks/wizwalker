"""
Window messages sent off the event loop

SendMessageW returns only once the window's own thread has handled the message, and a
game client's thread can go seconds without doing so: BartleBot saw one hold a friend
teleport's click for six. Called on the event loop, that holds every coroutine in the
process for as long -- every other client's input, and in a GUI app the window itself.
So every message to a client goes through :func:`send_message`, which makes the same
call on a worker thread and awaits it.

Each window has a worker of its own. The messages sent to one window keep the order they
were sent in, so a button-down is handled before its button-up, and a window that is not
answering holds up only its own input.

A message handed over is delivered even if the coroutine awaiting it is cancelled:
cancelling ends the wait, not the send. A key-up or button-up sent after a cancel is
therefore still handled after the key-down it follows.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

_workers: dict[int, ThreadPoolExecutor] = {}


def _worker(window_handle: int) -> ThreadPoolExecutor:
    worker = _workers.get(window_handle)
    if worker is None:
        worker = _workers[window_handle] = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"wizwalker-input-{window_handle:#x}"
        )
    return worker


async def send_message(
    send: Callable[[int, int, int, int], int],
    window_handle: int,
    message: int,
    wparam: int = 0,
    lparam: int = 0,
) -> int:
    """
    Call ``send(window_handle, message, wparam, lparam)`` on the window's worker thread

    Args:
        send: user32.SendMessageW, or anything shaped like it
        window_handle: The window the message is for
        message: The message
        wparam: Its wParam
        lparam: Its lParam

    Returns:
        What send returned
    """
    loop = asyncio.get_running_loop()
    delivery = loop.run_in_executor(_worker(window_handle), send, window_handle, message, wparam, lparam)
    # shielded, or a cancel would take a message still queued behind a slow one off the queue
    return await asyncio.shield(delivery)


def forget_window(window_handle: int):
    """
    Let a window's worker thread go, once nothing will be sent to it again

    Messages already handed over are still delivered. Sending to the window afterwards
    starts a new worker.
    """
    worker = _workers.pop(window_handle, None)
    if worker is not None:
        worker.shutdown(wait=False)
