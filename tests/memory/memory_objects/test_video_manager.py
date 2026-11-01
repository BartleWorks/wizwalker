"""The video manager, on code assembled here with made-up operands: no game bytes."""

import struct
from types import SimpleNamespace

import pymem.process
import pytest
import regex

from wizwalker import PatternFailed, PatternMultipleResults, VideoManagerError
from wizwalker.memory.memory_objects.video_manager import (
    _FRAME_PATTERN,
    _SLOT_PATTERN,
    _VTABLE_ENTRIES,
    VideoManager,
)
from wizwalker.memory.memory_reader import MemoryReader

MODULE = 0x1_4000_0000
CODE = MODULE + 0x1000
SLOT = MODULE + 0x2000
VTABLE = MODULE + 0x3000
MANAGER = 0x2_0000_0000
NODE = 0x3_0000_0000
STRING = 0x3_0000_1000

PENDING = 0x2AB01
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


def frame_function(tested: int = PENDING, cleared: int = PENDING) -> bytes:
    """The frame function's opening, with the sites it tests and clears the byte at."""
    code = bytearray(0x1A8)
    opening = (
        b"\x48\x8B\xC4\x48\x89\x58\x10\x48\x89\x70\x18\x55\x57\x41\x56"
        b"\x48\x8D\x68\x88\x48\x81\xEC\x60\x01\x00\x00"
        b"\x0F\x29\x70\xD8\x0F\x29\x78\xC8\x44\x0F\x29\x40\xB8"
        b"\x48\x8B\x05\x55\x66\x77\x08\x48\x33\xC4\x48\x89\x45\x20\x48\x8B\xD9"
    )
    code[: len(opening)] = opening
    code[0x3D:0x44] = b"\x80\xB9" + struct.pack("<I", tested) + b"\x00"
    code[0x1A0:0x1A7] = b"\xC6\x83" + struct.pack("<I", cleared) + b"\x00"
    return bytes(code)


@pytest.fixture
def game(process, monkeypatch):
    """A module with both sites, the slot holding a manager whose vtable names the frame function."""

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
    vtable = [0] * _VTABLE_ENTRIES
    vtable[90] = CODE + FRAME_AT
    process.map(VTABLE, struct.pack(f"<{_VTABLE_ENTRIES}Q", *vtable))
    process.map(MANAGER, struct.pack("<Q", VTABLE), size=PENDING + 0x10)

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


async def test_a_slot_at_either_side_of_the_code_is_found(game, manager):
    # The displacement is signed, and a slot before the code is as good as one after it
    below = MODULE + 0x800
    game.map(below, struct.pack("<Q", MANAGER))
    replace_code(game, SLOT_AT, slot_store(CODE + SLOT_AT, below))
    assert await manager.read_base_address() == MANAGER


async def test_an_empty_slot_raises(game, manager):
    game.put(SLOT, "Q", 0)
    with pytest.raises(VideoManagerError, match="empty"):
        await manager.read_base_address()


async def test_a_vtable_without_the_frame_function_raises(game, manager):
    game.put(VTABLE + 90 * 8, "Q", 0x1234)
    with pytest.raises(VideoManagerError, match="vtable"):
        await manager.read_base_address()


async def test_the_frame_function_is_found_at_any_entry(game, manager):
    game.put(VTABLE + 90 * 8, "Q", 0)
    game.put(VTABLE + 89 * 8, "Q", CODE + FRAME_AT)
    assert await manager.read_base_address() == MANAGER


async def test_a_new_object_in_the_slot_is_checked_again(game, manager):
    await manager.read_base_address()
    game.map(0x2_8000_0000, struct.pack("<Q", 0x2_8000_1000))
    game.map(0x2_8000_1000, size=_VTABLE_ENTRIES * 8)
    game.put(SLOT, "Q", 0x2_8000_0000)
    with pytest.raises(VideoManagerError, match="vtable"):
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


# --- the pending byte's offset -----------------------------------------------


@pytest.mark.parametrize("offset", [0x2AB01, 0x3CD07])
async def test_the_offset_comes_from_the_code(game, manager, offset):
    replace_code(game, FRAME_AT, frame_function(offset, offset))
    assert await manager.pending_flag_offset() == offset


async def test_sites_that_disagree_raise(game, manager):
    replace_code(game, FRAME_AT, frame_function(0x2AB01, 0x2AB09))
    with pytest.raises(VideoManagerError, match="tested at 0x2ab01 and cleared at 0x2ab09"):
        await manager.pending_flag_offset()


@pytest.mark.parametrize(
    ("site", "found"),
    [
        (0x3D, b"\x80\xBA\x01\xAB\x02\x00\x00"),  # another register
        (0x3D, b"\x80\xB9\x01\xAB\x02\x00\x01"),  # another value
        (0x1A0, b"\xC6\x87\x01\xAB\x02\x00\x00"),  # another register
        (0x1A0, b"\xC6\x83\x01\xAB\x02\x00\x01"),  # a set, not a clear
    ],
)
async def test_code_that_is_not_the_expected_instruction_raises(game, manager, site, found):
    replace_code(game, FRAME_AT + site, found)
    with pytest.raises(VideoManagerError, match="Expected"):
        await manager.pending_flag_offset()


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
        lambda game: game.put(VTABLE + 90 * 8, "Q", 0),
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
