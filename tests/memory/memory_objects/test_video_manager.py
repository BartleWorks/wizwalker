"""The video manager, on code assembled here with made-up operands: no game bytes."""

import struct
from types import SimpleNamespace

import pymem.process
import pytest
import regex

from wizwalker import MemoryReadError, PatternFailed, PatternMultipleResults, VideoManagerError
from wizwalker.memory.memory_objects.video_manager import _FRAME_PATTERN, _SLOT_PATTERN, VideoManager
from wizwalker.memory.memory_reader import MemoryReader

MODULE = 0x1_4000_0000
CODE = MODULE + 0x1000
SLOT = MODULE + 0x2000
MANAGER = 0x2_0000_0000
DEVICE = 0x2_8000_0000
NODE = 0x3_0000_0000
STRING = 0x3_0000_1000

PENDING = 0x2AB01
DEVICE_FIELD = 0x2AC10
WIDTH_FIELD = 0x24
HEIGHT_FIELD = 0x28
SLOT_AT = 0x20
FRAME_AT = 0x100


def slot_store(at: int, slot: int = SLOT) -> bytes:
    """The constructor's store of `this` in the slot, written to run at `at`."""
    return (
        b"\x48\x89\x3D" + struct.pack("<i", slot - (at + 7))
        + b"\x48\x8D\x05" + struct.pack("<i", 0x111)
        + b"\x48\x89\x07"
        + b"\x48\x8D\x05" + struct.pack("<i", 0x222)
        + b"\x48\x89\x47\x08"
    )


def frame_function(tested: int = PENDING, cleared: int = PENDING, device: int = DEVICE_FIELD) -> bytes:
    """The frame function: its opening, and the sites that say where each field is."""
    code = bytearray(0x1A8)
    opening = (
        b"\x48\x8B\xC4\x48\x89\x58\x10\x48\x89\x70\x18\x55\x57\x41\x56"
        b"\x48\x8D\x68\x88\x48\x81\xEC\x60\x01\x00\x00"
        b"\x0F\x29\x70\xD8\x0F\x29\x78\xC8\x44\x0F\x29\x40\xB8"
        b"\x48\x8B\x05\x55\x66\x77\x08\x48\x33\xC4\x48\x89\x45\x20\x48\x8B\xD9"
    )
    code[: len(opening)] = opening
    code[0x3D:0x44] = b"\x80\xB9" + struct.pack("<I", tested) + b"\x00"
    code[0x4A:0x51] = b"\x48\x8B\x89" + struct.pack("<I", device)
    code[0x150:0x157] = bytes([0x8B, 0x70, WIDTH_FIELD, 0x44, 0x8B, 0x70, HEIGHT_FIELD])
    code[0x1A0:0x1A7] = b"\xC6\x83" + struct.pack("<I", cleared) + b"\x00"
    return bytes(code)


@pytest.fixture
def game(process, monkeypatch):
    """A module with both sites, and the slot holding a manager with a device 1280x720."""

    def scan(self, handle, module, pattern):
        code = process.read_bytes(CODE, 0x1000)
        return [CODE + match.start() for match in regex.finditer(pattern, code, regex.DOTALL)]

    monkeypatch.setattr(pymem.process, "module_from_name", lambda handle, name: SimpleNamespace())
    monkeypatch.setattr(MemoryReader, "_scan_entire_module", scan)

    code = bytearray(0x1000)
    code[SLOT_AT : SLOT_AT + 24] = slot_store(CODE + SLOT_AT)
    frame = frame_function()
    code[FRAME_AT : FRAME_AT + len(frame)] = frame
    process.map(CODE, bytes(code))

    process.map(SLOT, struct.pack("<Q", MANAGER))
    process.map(MANAGER, size=DEVICE_FIELD + 0x10)
    process.put(MANAGER + DEVICE_FIELD, "Q", DEVICE)
    process.map(DEVICE, size=0x40)
    process.put(DEVICE + WIDTH_FIELD, "II", 1280, 720)

    process.map(NODE, size=0x60)
    process.put(NODE + 0x18, "Q", STRING)
    process.map(STRING, b"800x600\0", size=0x20)
    process.put(STRING + 0x10, "QQ", 7, 15)
    return process


@pytest.fixture
def manager(game, hook_handler) -> VideoManager:
    return VideoManager(hook_handler)


def replace_code(process, at: int, data: bytes):
    process.write_bytes(CODE + at, data, len(data))


def count_scans(monkeypatch) -> list:
    scans = []
    real = MemoryReader.pattern_scan

    async def counted(self, pattern, **kwargs):
        scans.append(pattern)
        return await real(self, pattern, **kwargs)

    monkeypatch.setattr(MemoryReader, "pattern_scan", counted)
    return scans


# --- the patterns ------------------------------------------------------------


def test_the_slot_pattern_matches_the_store_and_not_its_vtable_writes_alone():
    assert regex.search(_SLOT_PATTERN, slot_store(CODE), regex.DOTALL)
    assert not regex.search(_SLOT_PATTERN, slot_store(CODE)[7:], regex.DOTALL)


def test_the_frame_pattern_matches_whatever_the_cookie_displacement():
    assert regex.search(_FRAME_PATTERN, frame_function(), regex.DOTALL)
    assert not regex.search(_FRAME_PATTERN, frame_function()[1:], regex.DOTALL)


# --- the manager -------------------------------------------------------------


async def test_the_manager_comes_from_the_slot_the_constructor_stores_to(manager):
    assert await manager.read_base_address() == MANAGER


async def test_a_slot_before_the_code_is_found_too(game, manager):
    # The displacement is signed
    below = MODULE + 0x800
    game.map(below, struct.pack("<Q", MANAGER))
    replace_code(game, SLOT_AT, slot_store(CODE + SLOT_AT, below))
    assert await manager.read_base_address() == MANAGER


async def test_an_empty_slot_raises(game, manager):
    game.put(SLOT, "Q", 0)
    with pytest.raises(VideoManagerError, match="empty"):
        await manager.read_base_address()


async def test_an_object_with_no_device_raises(game, manager):
    game.put(MANAGER + DEVICE_FIELD, "Q", 0)
    with pytest.raises(VideoManagerError, match="no device"):
        await manager.read_base_address()


async def test_an_object_whose_device_cannot_be_read_raises(game, manager):
    game.put(MANAGER + DEVICE_FIELD, "Q", 0x7777_0000)
    with pytest.raises(MemoryReadError):
        await manager.read_base_address()


@pytest.mark.parametrize(
    "size",
    [(0, 0), (1280, 0), (99, 720), (1280, 99), (16385, 720), (1280, 16385), (0xFFFFFFFF, 0x40000000)],
)
async def test_a_device_of_no_real_size_raises(game, manager, size):
    game.put(DEVICE + WIDTH_FIELD, "II", *size)
    with pytest.raises(VideoManagerError, match="size"):
        await manager.read_base_address()


@pytest.mark.parametrize("size", [(534, 300), (100, 100), (16384, 16384)])
async def test_a_device_of_any_real_size_is_accepted(game, manager, size):
    game.put(DEVICE + WIDTH_FIELD, "II", *size)
    assert await manager.read_base_address() == MANAGER


async def test_a_new_object_in_the_slot_is_checked_again(game, manager):
    await manager.read_base_address()
    game.map(0x2_4000_0000, size=DEVICE_FIELD + 0x10)
    game.put(SLOT, "Q", 0x2_4000_0000)
    with pytest.raises(VideoManagerError, match="no device"):
        await manager.read_base_address()


async def test_a_pattern_with_no_match_raises(game, manager):
    replace_code(game, SLOT_AT, b"\x90" * 24)
    with pytest.raises(PatternFailed):
        await manager.read_base_address()


async def test_a_pattern_with_two_matches_raises(game, manager):
    replace_code(game, 0x60, slot_store(CODE + 0x60))
    with pytest.raises(PatternMultipleResults):
        await manager.read_base_address()


async def test_each_pattern_is_scanned_once(manager, monkeypatch):
    scans = count_scans(monkeypatch)
    for _ in range(3):
        await manager.read_base_address()
        await manager.pending_flag_offset()
    assert sorted(scans) == sorted([_SLOT_PATTERN, _FRAME_PATTERN])


# --- what the code says ------------------------------------------------------


@pytest.mark.parametrize("offset", [0x2AB01, 0x3CD07])
async def test_the_offset_comes_from_the_code(game, manager, offset):
    replace_code(game, FRAME_AT, frame_function(offset, offset))
    assert await manager.pending_flag_offset() == offset


async def test_sites_that_disagree_raise(game, manager):
    replace_code(game, FRAME_AT, frame_function(0x2AB01, 0x2AB09))
    with pytest.raises(VideoManagerError, match="tested at .* and cleared at"):
        await manager.pending_flag_offset()


@pytest.mark.parametrize(
    ("site", "found"),
    [
        (0x3D, b"\x80\xBA\x01\xAB\x02\x00\x00"),  # another register
        (0x3D, b"\x80\xB9\x01\xAB\x02\x00\x01"),  # another value
        (0x4A, b"\x48\x8B\x8B\x10\xAC\x02\x00"),  # another register
        (0x150, b"\x8B\x78\x24\x44\x8B\x70\x28"),  # another register
        (0x150, b"\x8B\x70\x24\x44\x8B\x78\x28"),  # another register for the height
        (0x1A0, b"\xC6\x87\x01\xAB\x02\x00\x00"),  # another register
        (0x1A0, b"\xC6\x83\x01\xAB\x02\x00\x01"),  # a set, not a clear
    ],
)
async def test_code_that_is_not_the_expected_instruction_raises(game, manager, site, found):
    replace_code(game, FRAME_AT + site, found)
    with pytest.raises(VideoManagerError, match="Expected"):
        await manager.pending_flag_offset()


async def test_the_device_size_comes_from_where_the_code_reads_it(game, manager):
    assert await manager.read_device_size() == (1280, 720)
    game.put(DEVICE + WIDTH_FIELD, "II", 534, 300)
    assert await manager.read_device_size() == (534, 300)


# --- asking for a size -------------------------------------------------------


async def test_a_resolution_writes_the_text_its_size_and_the_byte(game, manager):
    await manager.apply_resolution(NODE, 1024, 768)

    assert game.read_bytes(STRING, 9) == b"1024x768\0"
    assert game.get(STRING + 0x10, "Q") == 8
    assert game.get(MANAGER + PENDING, "B") == 1


async def test_the_longest_size_on_the_ladder_stays_inline(game, manager):
    await manager.apply_resolution(NODE, 2560, 1440)

    assert game.read_bytes(STRING, 10) == b"2560x1440\0"
    assert game.get(STRING + 0x10, "QQ") == (9, 15)


async def test_a_string_on_the_heap_keeps_its_buffer(game, manager):
    heap = 0x3_0000_2000
    game.map(heap, b"autodetect\0", size=0x20)
    game.put(STRING, "Q", heap)
    game.put(STRING + 0x10, "QQ", 10, 31)

    await manager.apply_resolution(NODE, 1280, 720)

    assert game.read_bytes(heap, 9) == b"1280x720\0"
    assert game.get(STRING, "Q") == heap
    assert game.get(STRING + 0x10, "QQ") == (8, 31)


async def test_a_node_with_no_string_writes_nothing(game, manager):
    game.put(NODE + 0x18, "Q", 0)
    with pytest.raises(VideoManagerError, match="no string"):
        await manager.apply_resolution(NODE, 1024, 768)
    assert game.get(MANAGER + PENDING, "B") == 0


@pytest.mark.parametrize(
    "break_it",
    [
        lambda game: game.put(SLOT, "Q", 0),
        lambda game: game.put(DEVICE + WIDTH_FIELD, "II", 0, 0),
        lambda game: replace_code(game, FRAME_AT + 0x1A0, b"\xC6\x83\x09\xAB\x02\x00\x00"),
    ],
)
async def test_a_failed_check_writes_nothing(game, manager, break_it):
    break_it(game)
    with pytest.raises(VideoManagerError):
        await manager.apply_resolution(NODE, 1024, 768)
    assert game.read_bytes(STRING, 8) == b"800x600\0"
    assert game.get(MANAGER + PENDING, "B") == 0


async def test_apply_pending_follows_the_byte(game, manager):
    assert await manager.apply_pending() is False
    await manager.apply_resolution(NODE, 1024, 768)
    assert await manager.apply_pending() is True
    game.put(MANAGER + PENDING, "B", 0)
    assert await manager.apply_pending() is False
