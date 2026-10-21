import asyncio
from types import SimpleNamespace

import pytest

from wizwalker import mouse_handler as mouse_module
from wizwalker.memory.hooks import MouselessCursorMoveHook
from wizwalker.mouse_handler import MouseHandler

LBUTTONDOWN, LBUTTONUP, RBUTTONDOWN, MOUSEMOVE = 0x201, 0x202, 0x204, 0x200


class User32:
    def __init__(self):
        self.messages = []

    def SendMessageW(self, handle, message, wparam, lparam):
        self.messages.append(message)

    PostMessageW = SendMessageW

    def ClientToScreen(self, handle, point):
        return 1


class Hooks:
    """The two mouseless calls a MouseHandler makes, counted."""

    def __init__(self):
        self.active = False
        self.activations = 0
        self.positions = []

    def _check_if_hook_active(self, hook_type):
        assert hook_type is MouselessCursorMoveHook
        return self.active

    async def activate_mouseless_cursor_hook(self):
        self.active = True
        self.activations += 1

    async def deactivate_mouseless_cursor_hook(self):
        self.active = False

    async def write_mouse_position(self, x, y):
        self.positions.append((x, y))


@pytest.fixture
def user32(monkeypatch) -> User32:
    fake = User32()
    monkeypatch.setattr(mouse_module, "user32", fake)
    return fake


@pytest.fixture
def mouse(user32) -> MouseHandler:
    handler = MouseHandler(SimpleNamespace(hook_handler=Hooks(), window_handle=1))
    handler.click_predelay = 0
    return handler


async def test_nested_holds_activate_the_hook_once(mouse):
    hooks = mouse.client.hook_handler
    async with mouse:
        async with mouse:
            assert hooks.active
        assert hooks.active
    assert not hooks.active
    assert hooks.activations == 1


async def test_a_held_hook_cannot_be_released_by_hand(mouse):
    async with mouse:
        with pytest.raises(RuntimeError):
            await mouse.deactivate_mouseless()


async def test_a_click_presses_releases_and_moves_away(mouse, user32):
    await mouse.click(10, 20)
    assert user32.messages == [MOUSEMOVE, LBUTTONDOWN, LBUTTONUP, MOUSEMOVE]
    assert mouse.client.hook_handler.positions == [(10, 20), (-100, -100)]


async def test_a_right_click(mouse, user32):
    await mouse.click(1, 1, right_click=True)
    assert RBUTTONDOWN in user32.messages and RBUTTONDOWN + 1 in user32.messages


async def test_a_held_click_cut_short_still_lets_go(mouse, user32):
    task = asyncio.create_task(mouse.click(1, 1, sleep_duration=60))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert user32.messages[-1] == LBUTTONUP


async def test_clicks_do_not_overlap(mouse, user32):
    await asyncio.gather(mouse.click(1, 1, sleep_duration=0.02), mouse.click(2, 2, sleep_duration=0.02))
    downs_and_ups = [m for m in user32.messages if m in (LBUTTONDOWN, LBUTTONUP)]
    assert downs_and_ups == [LBUTTONDOWN, LBUTTONUP, LBUTTONDOWN, LBUTTONUP]
