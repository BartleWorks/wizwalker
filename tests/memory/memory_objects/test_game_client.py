import struct

import pytest
import regex

from wizwalker import PatternFailed
from wizwalker.memory.memory_object import DynamicMemoryObject
from wizwalker.memory.memory_objects import game_client
from wizwalker.memory.memory_objects.game_client import GameClient

BASE = 0x2000_0000
CODE = 0x1400_0000

# Made-up offsets, so a pattern that names one of r814353's would not find them
ELASTIC, FREE, SELECTED, FREECAM = 0x30010, 0x30020, 0x30040, 0x30058
SELECT_SLOT = 0x4B0  # the vtable slot the switch calls


def _d(offset: int) -> str:
    return struct.pack("<i", offset).hex(" ")


# The client's camera switches, assembled with the instruction forms r812087 and r814353
# have and the offsets above
SELECT_ELASTIC = bytes.fromhex(
    f"48 8b 93 {_d(ELASTIC)}"  # mov rdx, [rbx+elastic]
    f"ff 90 {_d(SELECT_SLOT)}"  # call [rax+select_slot]
    f"c6 83 {_d(FREECAM)} 00"  # mov byte [rbx+freecam], 0
)
SELECT_FREE = bytes.fromhex(
    f"48 8b 8b {_d(FREE)}"  # mov rcx, [rbx+free]
    "e8 11 11 11 11"  # call
    f"c6 83 {_d(FREECAM)} 01"  # mov byte [rbx+freecam], 1
    "c6 83 50 24 02 00 01"  # mov byte [rbx+?], 1
)
TOGGLE = bytes.fromhex(
    f"48 39 93 {_d(SELECTED)}"  # cmp [rbx+selected], rdx
    "75 11"  # jne
    f"48 8b 93 {_d(ELASTIC)}"  # mov rdx, [rbx+elastic]
    "ff 90 98 04 00 00"  # call [rax+0x498]
)
FILLER = b"\xcc" * 16
CLIENT_CODE = FILLER + SELECT_ELASTIC + FILLER + SELECT_FREE + FILLER + TOGGLE + FILLER


def _scan(pattern: bytes, data: bytes) -> list[int]:
    return [m.start() for m in regex.finditer(pattern, data, regex.DOTALL)]


@pytest.mark.parametrize(
    "pattern, read_at, offset",
    [
        (game_client.SELECT_ELASTIC_PATTERN, 3, ELASTIC),
        (game_client.SELECT_FREE_PATTERN, 3, FREE),
        (game_client.SELECTED_IS_ELASTIC_PATTERN, 3, SELECTED),
        (game_client.FREECAM_CLEAR_PATTERN, 2, FREECAM),
        (game_client.SELECT_ELASTIC_PATTERN, 9, SELECT_SLOT),
    ],
    ids=["elastic", "free", "selected", "freecam", "select slot"],
)
def test_each_camera_pattern_finds_its_offset_once_in_the_clients_switches(pattern, read_at, offset):
    found = _scan(pattern, CLIENT_CODE)
    assert len(found) == 1
    assert struct.unpack_from("<i", CLIENT_CODE, found[0] + read_at)[0] == offset


class _GameClient(GameClient):
    async def read_base_address(self) -> int:
        return BASE


@pytest.fixture
def client_code(process, monkeypatch):
    """Scans for patterns over `code` as the client would; None for a build none matches."""

    def install(code: bytes):
        process.map(CODE, code)

        async def scan(self, pattern, *, module=None, return_multiple=False):
            found = _scan(pattern, code)
            if not found:
                raise PatternFailed(pattern)
            return CODE + found[0]

        monkeypatch.setattr(GameClient, "pattern_scan", scan)

    return install


def _controllers(process, layout: dict[int, int]):
    process.map(BASE, size=0x40000)
    for offset, pointer in layout.items():
        process.put(BASE + offset, "Q", pointer)


@pytest.mark.parametrize(
    "accessor, offset",
    [
        ("elastic_camera_controller", ELASTIC),
        ("free_camera_controller", FREE),
        ("selected_camera_controller", SELECTED),
    ],
)
async def test_a_camera_controller_is_read_where_the_clients_code_keeps_it(
    process, hook_handler, client_code, accessor, offset
):
    client_code(CLIENT_CODE)
    _controllers(process, {offset: 0x5555_0000})
    controller = await getattr(_GameClient(hook_handler), accessor)()
    assert isinstance(controller, DynamicMemoryObject)
    assert controller.base_address == 0x5555_0000


async def test_the_freecam_flag_is_read_and_written_where_the_clients_code_keeps_it(
    process, hook_handler, client_code
):
    client_code(CLIENT_CODE)
    _controllers(process, {})
    client = _GameClient(hook_handler)
    await client.write_is_freecam(True)
    assert process.get(BASE + FREECAM, "?") is True
    assert await client.is_freecam() is True


# Where r814353 keeps each, which a build that no pattern matches is read at: the
# controllers are shared pointers from 0x222D0, each 16 bytes, object then refcount
R814353 = {
    "elastic_camera_controller": 0x222D0,
    "free_camera_controller": 0x222E0,
    "selected_camera_controller": 0x22300,
}


@pytest.mark.parametrize("accessor, offset", R814353.items())
async def test_without_a_match_the_camera_is_read_at_r814353s_offsets(
    process, hook_handler, client_code, accessor, offset
):
    client_code(FILLER)
    # Each shared pointer's refcount block, which the old offsets read, is a decoy
    _controllers(process, {offset: 0x5555_0000, offset + 8: 0x6666_0000})
    controller = await getattr(_GameClient(hook_handler), accessor)()
    assert controller.base_address == 0x5555_0000


async def test_without_a_match_the_freecam_flag_is_r814353s(process, hook_handler, client_code):
    client_code(FILLER)
    _controllers(process, {})
    process.put(BASE + 0x22318, "?", True)
    assert await _GameClient(hook_handler).is_freecam() is True


async def test_the_fishing_manager_is_the_pointer_the_client_calls_through(process, hook_handler):
    _controllers(process, {0x231A8: 0x0396_D2C1, 0x231D0: 0x5555_0000})
    manager = await _GameClient(hook_handler).fishing_manager()
    assert await manager.read_base_address() == 0x5555_0000


@pytest.mark.parametrize(
    "accessor, offset",
    [("user_id", 0x21258), ("player_gid", 0x213A8), ("machine_id", 0x21508)],
)
async def test_the_login_ids_are_read_where_r814353_keeps_them(process, hook_handler, accessor, offset):
    _controllers(process, {offset: 0x0123_4567_89AB_CDEF})
    assert await getattr(_GameClient(hook_handler), accessor)() == 0x0123_4567_89AB_CDEF


@pytest.mark.parametrize(
    "accessor, offset",
    [("root_client_object", 0x21378), ("character_registry", 0x227A0)],
)
async def test_the_root_object_and_the_registry_are_read_where_r814353_keeps_them(
    process, hook_handler, accessor, offset
):
    _controllers(process, {offset: 0x5555_0000})
    found = await getattr(_GameClient(hook_handler), accessor)()
    assert await found.read_base_address() == 0x5555_0000


async def test_the_select_camera_slot_is_read_from_the_clients_switch(process, hook_handler, client_code):
    client_code(CLIENT_CODE)
    assert await _GameClient(hook_handler).select_camera_slot() == SELECT_SLOT


async def test_without_a_match_the_select_camera_slot_is_r814353s(process, hook_handler, client_code):
    client_code(FILLER)
    assert await _GameClient(hook_handler).select_camera_slot() == 0x498
