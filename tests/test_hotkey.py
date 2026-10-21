import asyncio

import pytest

from wizwalker import Keycode, hotkey
from wizwalker.hotkey import HotkeyListener, ModifierKeys
from wizwalker.utils import poll_until


class FakeUser32:
    """RegisterHotKey and friends, with no Windows message queue behind them."""

    def __init__(self):
        self.registered: dict[int, tuple[int, int]] = {}

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, keycode):
        if (modifiers & ~ModifierKeys.NOREPEAT, keycode) in [
            (m & ~ModifierKeys.NOREPEAT, k) for m, k in self.registered.values()
        ]:
            return 0
        self.registered[hotkey_id] = (modifiers, keycode)
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id):
        return 1 if self.registered.pop(hotkey_id, None) else 0

    def PeekMessageW(self, *args):
        return 0

    def DispatchMessageW(self, *args):
        return 0


@pytest.fixture
def user32(monkeypatch) -> FakeUser32:
    fake = FakeUser32()
    monkeypatch.setattr(hotkey, "user32", fake)
    # Fresh module state for each test: both are process-wide
    monkeypatch.setattr(hotkey, "_hotkey_id_manager", hotkey._GlobalHotkeyIdentifierManager())
    loop = hotkey._GlobalHotkeyMessageLoop()
    loop.set_message_loop_delay(0.01)
    monkeypatch.setattr(hotkey, "_hotkey_message_loop", loop)
    return fake


def press(keycode: Keycode, modifiers: int = 0):
    """What the message loop records when Windows reports the hotkey."""
    hotkey._hotkey_message_loop.messages.append((keycode.value, modifiers))


async def test_hotkey_ids_are_reused_once_freed():
    ids = hotkey._GlobalHotkeyIdentifierManager()
    assert [await ids.get_id() for _ in range(3)] == [1, 2, 3]
    await ids.free_id(2)
    assert await ids.get_id() == 2
    for hotkey_id in (1, 2, 3):
        await ids.free_id(hotkey_id)
    assert await ids.get_id() == 1


async def test_the_message_loop_starts_again_after_its_last_listener_left():
    # disconnect cancelled the task but kept it, so connect never started another
    loop = hotkey._GlobalHotkeyMessageLoop()
    loop.connect()
    first = loop.message_loop_task
    loop.disconnect()
    await asyncio.sleep(0)
    assert first.cancelled() or first.cancelling()

    loop.connect()
    try:
        assert loop.message_loop_task is not None and loop.message_loop_task is not first
        assert not loop.message_loop_task.done()
    finally:
        loop.disconnect()


async def test_a_disconnect_too_many_leaves_the_count_at_zero():
    loop = hotkey._GlobalHotkeyMessageLoop()
    loop.disconnect()
    assert loop.connected_instances == 0


async def test_a_pressed_hotkey_runs_its_callback(user32):
    pressed = asyncio.Event()

    async def callback():
        pressed.set()

    listener = HotkeyListener(sleep_time=0.01)
    await listener.add_hotkey(Keycode.A, callback, modifiers=ModifierKeys.CTRL | ModifierKeys.NOREPEAT)
    listener.start()
    try:
        press(Keycode.A, ModifierKeys.CTRL)
        await asyncio.wait_for(pressed.wait(), 2)
    finally:
        await listener.stop()


async def test_a_listener_restarted_still_hears_its_hotkeys(user32):
    presses = []

    async def callback():
        presses.append(1)

    listener = HotkeyListener(sleep_time=0.01)
    await listener.add_hotkey(Keycode.B, callback)
    listener.start()
    await listener.stop()

    listener.start()
    try:
        press(Keycode.B)
        assert await poll_until(lambda: asyncio.sleep(0, presses), timeout=2)
    finally:
        await listener.stop()


async def test_a_second_stop_does_not_disconnect_twice(user32):
    other = HotkeyListener()
    other.start()
    listener = HotkeyListener()
    listener.start()

    await listener.stop()
    await listener.stop()
    try:
        assert hotkey._hotkey_message_loop.connected_instances == 1
    finally:
        await other.stop()


async def test_removing_a_hotkey_drops_its_callback(user32):
    async def callback():
        pass

    listener = HotkeyListener()
    await listener.add_hotkey(Keycode.C, callback, modifiers=ModifierKeys.NOREPEAT)
    await listener.remove_hotkey(Keycode.C, modifiers=ModifierKeys.NOREPEAT)
    assert listener._callbacks == {}
    assert user32.registered == {}


async def test_removing_an_unknown_hotkey_is_a_value_error(user32):
    with pytest.raises(ValueError):
        await HotkeyListener().remove_hotkey(Keycode.D)


async def test_a_hotkey_taken_elsewhere_is_refused(user32):
    async def callback():
        pass

    await HotkeyListener().add_hotkey(Keycode.E, callback)
    with pytest.raises(ValueError):
        await HotkeyListener().add_hotkey(Keycode.E, callback)


async def test_finished_callbacks_are_not_kept(user32):
    async def callback():
        pass

    listener = HotkeyListener()
    await listener.add_hotkey(Keycode.F, callback)
    for _ in range(5):
        await listener._handle_hotkey(Keycode.F.value, 0)
    assert await poll_until(lambda: asyncio.sleep(0, not listener._callback_tasks), timeout=2)
