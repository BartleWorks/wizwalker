import asyncio
import io
import math
import struct
import zlib
from functools import partial

import pytest

from wizwalker import ExceptionalTimeout, Keycode, utils
from wizwalker.constants import WM_KEYDOWN, WM_KEYUP
from wizwalker.memory.memory_objects.conditionals import school_id_to_names
from wizwalker.utils import XYZ, Rectangle


class Calls:
    """An awaitable that answers from a script, one item per call; an exception is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.count = 0

    async def __call__(self):
        self.count += 1
        answer = self.answers[min(self.count, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def sleeps(monkeypatch):
    """Every asyncio.sleep's duration, while still yielding to the loop."""
    durations = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, result=None):
        durations.append(delay)
        await real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    return durations


# --- poll_until --------------------------------------------------------------


async def test_poll_until_returns_as_soon_as_the_predicate_holds():
    pred = Calls(False, False, True)
    assert await utils.poll_until(pred, timeout=5, interval=0.01) is True
    assert pred.count == 3


async def test_poll_until_gives_up_at_the_deadline():
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await utils.poll_until(Calls(False), timeout=0.15, interval=0.05) is False
    assert 0.14 <= loop.time() - started < 1.0


async def test_poll_until_reads_once_even_with_no_time():
    pred = Calls(True)
    assert await utils.poll_until(pred, timeout=0) is True
    assert pred.count == 1


async def test_poll_until_treats_an_error_as_not_yet():
    pred = Calls(ValueError("mid-change"), True)
    assert await utils.poll_until(pred, timeout=5, interval=0.01) is True


async def test_poll_until_can_let_an_error_through():
    with pytest.raises(ValueError):
        await utils.poll_until(Calls(ValueError("no")), timeout=5, ignore_errors=False)


async def test_poll_until_never_sleeps_past_the_deadline(sleeps):
    await utils.poll_until(Calls(False), timeout=0.01, interval=10)
    assert all(d <= 0.01 for d in sleeps)


# --- the older wait helpers --------------------------------------------------


async def test_wait_for_value_returns_once_it_matches():
    coro = Calls(1, 2, 3)
    assert await utils.wait_for_value(coro, 3, sleep_time=0) == 3


async def test_wait_for_value_yields_between_mismatches(sleeps):
    # Without a sleep after a mismatch the loop held the event loop.
    await utils.wait_for_value(Calls(1, 1, 2), 2, sleep_time=0.1)
    assert sleeps == [0.1, 0.1]


async def test_maybe_wait_for_value_returns_the_wanted_value():
    assert await utils.maybe_wait_for_value_with_timeout(Calls(0, 5), sleep_time=0, value=5) == 5


async def test_maybe_wait_for_value_returns_the_first_other_value():
    coro = Calls(5, 5, 7)
    assert await utils.maybe_wait_for_value_with_timeout(coro, sleep_time=0, value=5, inverse_value=True) == 7


async def test_maybe_wait_for_value_can_wait_for_none():
    # value=None without inverse_value matched nothing and never returned
    coro = Calls(1, None)
    assert await utils.maybe_wait_for_value_with_timeout(coro, sleep_time=0, timeout=1) is None
    assert coro.count == 2


async def test_maybe_wait_for_value_sleeps_once_after_an_error(sleeps):
    coro = Calls(ValueError("a"), ValueError("b"), 3)
    await utils.maybe_wait_for_value_with_timeout(coro, sleep_time=0.2, value=3)
    assert sleeps == [0.2, 0.2]


async def test_maybe_wait_for_value_times_out_with_the_last_error():
    error = ValueError("unreadable")
    with pytest.raises(ExceptionalTimeout) as caught:
        await utils.maybe_wait_for_value_with_timeout(Calls(error), sleep_time=0.01, value=1, timeout=0.05)
    assert caught.value.possible_exception is error


async def test_maybe_wait_for_value_times_out_cleanly_on_a_partial():
    # Its message read coro.__name__, which a partial lacks: AttributeError, not a timeout
    async def read(_):
        return 0

    with pytest.raises(ExceptionalTimeout):
        await utils.maybe_wait_for_value_with_timeout(partial(read, 1), sleep_time=0.01, value=1, timeout=0.05)


async def test_maybe_wait_for_any_value_returns_the_first_non_none():
    assert await utils.maybe_wait_for_any_value_with_timeout(Calls(None, [], 4), sleep_time=0) == []


async def test_maybe_wait_for_any_value_times_out_cleanly_on_a_partial():
    async def read(_):
        return None

    with pytest.raises(ExceptionalTimeout):
        await utils.maybe_wait_for_any_value_with_timeout(partial(read, 1), sleep_time=0.01, timeout=0.05)


async def test_maybe_wait_for_any_value_can_let_an_error_through():
    with pytest.raises(KeyError):
        await utils.maybe_wait_for_any_value_with_timeout(Calls(KeyError("x")), ignore_exceptions=False)


async def test_wait_for_non_error_retries_until_it_succeeds():
    assert await utils.wait_for_non_error(Calls(OSError(), OSError(), "ok"), sleep_time=0) == "ok"


# --- sorting and geometry ----------------------------------------------------


async def test_async_sorted_keeps_equal_and_unhashable_items():
    async def key(item):
        return item["k"]

    items = [{"k": 2}, {"k": 1}, {"k": 2}]
    assert await utils.async_sorted(items, key=key) == [{"k": 1}, {"k": 2}, {"k": 2}]


async def test_async_sorted_without_a_key_is_sorted():
    assert await utils.async_sorted([3, 1, 2], reverse=True) == [3, 2, 1]


def test_xyz_distance_ignores_height():
    assert XYZ(0, 0, 0).distance(XYZ(3, 4, 100)) == 5
    assert XYZ(0, 0, 0) - XYZ(3, 4, 0) == 5


def test_xyz_refuses_another_type():
    with pytest.raises(ValueError):
        XYZ(0, 0, 0).distance((1, 1, 1))


@pytest.mark.parametrize(
    "target, yaw",
    [
        (XYZ(0, -10, 0), 0),
        (XYZ(-10, 0, 0), math.pi / 2),
        (XYZ(0, 10, 0), math.pi),
        (XYZ(10, 0, 0), 3 * math.pi / 2),
    ],
)
def test_perfect_yaw_faces_the_target(target, yaw):
    assert utils.calculate_perfect_yaw(XYZ(0, 0, 0), target) == pytest.approx(yaw)


def test_perfect_yaw_is_zero_for_a_target_underfoot():
    assert utils.calculate_perfect_yaw(XYZ(5, 5, 0), XYZ(5.2, 5.1, 9)) == 0


def test_perfect_yaw_due_north_and_south_does_not_raise():
    # acos's argument rounded past +-1 for these and raised ValueError
    for dy in (1e9, -1e9, 12345.678):
        assert 0 <= utils.calculate_perfect_yaw(XYZ(0.1, 0.2, 0), XYZ(0.1, 0.2 + dy, 0)) < math.tau


def test_relative_yaw_keeps_the_axis_not_given():
    assert XYZ(0, 0, 0).relative_yaw(y=10) == pytest.approx(math.pi)


def test_rectangle_iterates_in_constructor_order():
    rect = Rectangle(1, 2, 3, 4)
    assert tuple(rect) == (1, 2, 3, 4)
    assert tuple(Rectangle(*rect)) == (1, 2, 3, 4)


def test_rectangle_center():
    assert Rectangle(10, 20, 30, 60).center() == (20, 40)


def test_rectangle_scales_through_its_parents():
    child = Rectangle(5, 5, 15, 25)
    scaled = child.scale_to_client([Rectangle(100, 200, 0, 0), Rectangle(10, 20, 0, 0)], 2.0)
    assert tuple(scaled) == (230, 450, 250, 490)


def test_order_clients_sorts_top_to_bottom_then_left_to_right():
    class Client:
        def __init__(self, rect):
            self.window_rectangle = rect

    a, b, c = Client(Rectangle(500, 0, 0, 0)), Client(Rectangle(0, 0, 0, 0)), Client(Rectangle(0, 300, 0, 0))
    assert utils.order_clients([a, c, b]) == [b, a, c]


# --- file parsers --------------------------------------------------------------


def _node_entry(x: float, y: float, z: float, number: int) -> bytes:
    entry = bytearray(50)
    entry[0:4] = b"\xFE\xDB\xAE\x04"
    entry[16:28] = struct.pack("<fff", x, y, z)
    entry[48:50] = struct.pack("<H", number)
    return bytes(entry)


def test_node_data_reads_every_entry():
    # It never moved past the first marker, so a file with any node looped forever
    data = b"\x00" * 20 + _node_entry(1, 2, 3, 7) + _node_entry(-4, 5.5, 0, 9)
    assert utils.pharse_node_data(data) == {7: (1, 2, 3), 9: (-4, 5.5, 0)}


def test_node_data_with_no_nodes():
    assert utils.pharse_node_data(b"\x00" * 20) == {}


def test_nav_data_reads_vertices_and_edges():
    data = b"\x00\x00" + struct.pack("<i", 2)
    data += struct.pack("<fffh", 1, 2, 3, 0) + struct.pack("<fffh", 4, 5, 6, 1)
    data += struct.pack("<i", 2) + struct.pack("<hh", 0, 1) + struct.pack("<hh", 1, 0)
    vertices, edges = utils.pharse_nav_data(data)
    assert [tuple(v) for v in vertices] == [(1, 2, 3), (4, 5, 6)]
    assert edges == [(0, 1), (1, 0)]


def test_nav_data_refuses_a_vertex_out_of_order():
    data = b"\x00\x00" + struct.pack("<i", 1) + struct.pack("<fffh", 1, 2, 3, 5) + struct.pack("<i", 0)
    with pytest.raises(RuntimeError):
        utils.pharse_nav_data(data)


def test_template_id_file_maps_ids_to_names():
    body = io.BytesIO()
    body.write(b"\x00" * 0x24)
    for name, entry_id in (("Sword", 42), ("Shield", -7)):
        encoded = name.encode()
        body.write(bytes([len(encoded) * 2]) + encoded + b"\x00" * 8 + struct.pack("<i", entry_id) + b"\x00" * 0x10)
    data = b"BINd" + b"\x00" * 9 + zlib.compress(body.getvalue())
    assert utils.pharse_template_id_file(data) == {42: "Sword", -7: "Shield"}


def test_template_id_file_refuses_another_format():
    with pytest.raises(RuntimeError):
        utils.pharse_template_id_file(b"KIWAD....")


# --- ids and matrices --------------------------------------------------------


def test_string_ids_match_the_games_own():
    # These ids are read from the game, so this checks the hash against it
    for name, string_id in school_id_to_names.items():
        assert utils.make_string_id(name) == string_id


def test_sign_extend():
    assert utils.sign_extend(0x7FFF_FFFF) == 0x7FFF_FFFF
    assert utils.sign_extend(0xFFFF_FFFF) == -1


def test_ypr_matrix_of_no_rotation_is_the_base():
    identity = [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0]
    assert utils.make_ypr_matrix(identity, utils.Orient(0, 0, 0)) == pytest.approx(identity)


def test_a_quarter_yaw_turns_x_onto_z():
    turned = utils.yaw_matrix(math.pi / 2)
    assert turned == pytest.approx([0, 0, -1, 0, 1, 0, 1, 0, 0], abs=1e-12)


# --- keys --------------------------------------------------------------------


class RecordingUser32:
    def __init__(self):
        self.messages = []

    def SendMessageW(self, handle, message, wparam, lparam):
        self.messages.append((message, wparam))
        return 0


@pytest.fixture
def user32(monkeypatch) -> RecordingUser32:
    fake = RecordingUser32()
    monkeypatch.setattr(utils, "user32", fake)
    return fake


async def test_send_hotkey_holds_the_modifiers_around_the_key(user32):
    await utils.send_hotkey(1, [Keycode.CTRL, Keycode.SHIFT], Keycode.A)
    assert user32.messages == [
        (WM_KEYDOWN, Keycode.CTRL.value),
        (WM_KEYDOWN, Keycode.SHIFT.value),
        (WM_KEYDOWN, Keycode.A.value),
        (WM_KEYUP, Keycode.A.value),
        (WM_KEYUP, Keycode.CTRL.value),
        (WM_KEYUP, Keycode.SHIFT.value),
    ]


async def test_timed_send_key_ends_with_a_key_up(user32):
    await utils.timed_send_key(1, Keycode.W, 0.12)
    assert user32.messages[0] == (WM_KEYDOWN, Keycode.W.value)
    assert user32.messages[-1] == (WM_KEYUP, Keycode.W.value)
    assert len(user32.messages) >= 3


async def test_timed_send_key_lets_the_key_up_when_cancelled(user32):
    task = asyncio.create_task(utils.timed_send_key(1, Keycode.W, 60))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert user32.messages[-1] == (WM_KEYUP, Keycode.W.value)


# --- folders -----------------------------------------------------------------


def test_the_cache_folder_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "_CACHE_FOLDER_OVERRIDE", None)
    utils.override_cache_folder(tmp_path / "cache")
    try:
        assert utils.get_cache_folder() == (tmp_path / "cache").absolute()
        assert (tmp_path / "cache").is_dir()
    finally:
        utils.override_cache_folder(None)


def test_the_install_location_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "_OVERRIDE_PATH", None)
    utils.override_wiz_install_location(str(tmp_path))
    try:
        assert utils.get_wiz_install() == tmp_path.absolute()
    finally:
        utils.override_wiz_install_location(None)
