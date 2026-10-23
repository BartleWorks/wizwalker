import struct
from enum import Enum

import pytest

from wizwalker import MemoryReadError, ReadingEnumFailed
from wizwalker.constants import Primitive
from wizwalker.memory.memory_object import MAX_STRING, DynamicMemoryObject, PropertyClass
from wizwalker.utils import XYZ, Color, Orient

from ..fakes import pack_string

BASE = 0x10000


@pytest.fixture
def obj(process, hook_handler) -> DynamicMemoryObject:
    process.map(BASE, size=0x200)
    return DynamicMemoryObject(hook_handler, BASE)


def test_a_zero_base_address_is_refused(hook_handler):
    with pytest.raises(RuntimeError):
        DynamicMemoryObject(hook_handler, 0)


# --- strings -----------------------------------------------------------------


async def test_reads_an_inline_string(obj, process):
    process.write_bytes(BASE, pack_string("Ferme"), 32)
    assert await obj.read_string(BASE) == "Ferme"


async def test_reads_a_heap_string(obj, process):
    process.write_bytes(BASE, pack_string("Sorcelleville!!!", heap=0x50000, process=process), 32)
    assert await obj.read_string(BASE) == "Sorcelleville!!!"


async def test_a_short_string_in_a_heap_buffer_is_read_from_the_heap(obj, process):
    # Capacity, not length, says where the characters are: a control that once held
    # a long string keeps its heap buffer while showing a short one.
    process.write_bytes(BASE, pack_string("MeuhShu", heap=0x50000, process=process), 32)
    process.put(BASE + 24, "Q", 40)
    assert await obj.read_string(BASE) == "MeuhShu"


async def test_reads_inline_and_heap_wide_strings(obj, process):
    process.write_bytes(BASE, pack_string("Ice", wide=True), 32)
    process.write_bytes(BASE + 32, pack_string("Unicorn Way", wide=True, heap=0x60000, process=process), 32)
    assert await obj.read_wide_string(BASE) == "Ice"
    assert await obj.read_wide_string(BASE + 32) == "Unicorn Way"


@pytest.mark.parametrize("length", [0, -1, MAX_STRING + 1])
async def test_a_length_out_of_bounds_reads_empty(obj, process, length):
    process.put(BASE + 16, "qQ", length, 15)
    assert await obj.read_string(BASE) == ""


def _long_wide_string(process, text: str) -> None:
    process.write_bytes(BASE, pack_string(text, wide=True, heap=0x80000, process=process), 32)


async def test_a_wide_string_past_max_string_reads_with_a_raised_bound(obj, process):
    text = "x" * (MAX_STRING + 1)
    _long_wide_string(process, text)
    assert await obj.read_wide_string(BASE) == ""
    assert await obj.read_wide_string(BASE, max_len=MAX_STRING * 10) == text


async def test_a_wide_tail_reads_only_the_end_and_says_it_was_cut(obj, process):
    text = "".join(f"line {i}\n" for i in range(2000))  # past MAX_STRING
    _long_wide_string(process, text)
    assert await obj.read_wide_string_tail(BASE, 100, max_len=len(text)) == (text[-100:], True)


async def test_a_wide_tail_longer_than_the_string_reads_all_of_it(obj, process):
    _long_wide_string(process, "Unicorn Way")
    assert await obj.read_wide_string_tail(BASE, 100) == ("Unicorn Way", False)


async def test_a_short_inline_string_reads_whole_as_a_tail(obj, process):
    process.write_bytes(BASE, pack_string("Ice", wide=True), 32)
    assert await obj.read_wide_string_tail(BASE, 100) == ("Ice", False)


async def test_a_wide_tail_still_refuses_a_length_past_its_bound(obj, process):
    _long_wide_string(process, "x" * (MAX_STRING + 1))
    assert await obj.read_wide_string_tail(BASE, 100) == ("", False)


async def test_a_wide_tail_cut_inside_a_surrogate_pair_drops_its_second_half(obj, process):
    # "🐉" is two UTF-16 code units, so a 3-unit tail starts on the low half.
    _long_wide_string(process, "Dragon 🐉ab")
    assert await obj.read_wide_string_tail(BASE, 3) == ("ab", True)


async def test_an_inline_string_longer_than_its_buffer_reads_empty(obj, process):
    process.put(BASE + 16, "QQ", 20, 15)
    assert await obj.read_string(BASE) == ""


async def test_undecodable_bytes_read_empty(obj, process):
    process.write_bytes(BASE, b"\xff\xfe\xfd" + b"\x00" * 13 + struct.pack("<QQ", 3, 15), 32)
    assert await obj.read_string(BASE) == ""


async def test_reads_a_string_at_an_offset(obj, process):
    process.write_bytes(BASE + 80, pack_string("WorldView"), 32)
    assert await obj.read_string_from_offset(80) == "WorldView"


async def test_writing_a_string_that_fits_inline(obj, process):
    process.write_bytes(BASE, pack_string("old"), 32)
    await obj.write_string(BASE, "newer")
    assert await obj.read_string(BASE) == "newer"
    assert process.get(BASE + 16, "Q") == 5


async def test_writing_into_a_heap_buffer_with_room(obj, process):
    process.write_bytes(BASE, pack_string("a long enough name", heap=0x50000, process=process), 32)
    await obj.write_string(BASE, "short")
    assert process.get(BASE, "Q") == 0x50000
    assert await obj.read_string(BASE) == "short"


async def test_writing_a_string_that_outgrows_its_buffer_moves_it(obj, process):
    process.write_bytes(BASE, pack_string("tiny"), 32)
    await obj.write_string(BASE, "far too long to fit inline")
    pointer, length, capacity = process.get(BASE, "Q"), process.get(BASE + 16, "Q"), process.get(BASE + 24, "Q")
    assert pointer != 0 and capacity >= length == 26
    assert await obj.read_string(BASE) == "far too long to fit inline"


async def test_a_wide_write_carries_no_byte_order_mark(obj, process):
    process.write_bytes(BASE, pack_string("", wide=True), 32)
    await obj.write_wide_string(BASE, "Hi", encoding="utf-16")
    assert process.read_bytes(BASE, 4) == "Hi".encode("utf-16-le")
    assert await obj.read_wide_string(BASE) == "Hi"


async def test_reads_a_null_terminated_string(obj, process):
    process.write_bytes(BASE, b"Window\x00junk", 11)
    assert await obj.read_null_terminated_string(BASE, 11) == "Window"


async def test_an_empty_null_terminated_string(obj, process):
    assert await obj.read_null_terminated_string(BASE, 8) == ""


async def test_a_null_terminated_string_without_its_end(obj, process):
    process.write_bytes(BASE, b"x" * 8, 8)
    with pytest.raises(MemoryReadError):
        await obj.read_null_terminated_string(BASE, 8)


# --- plain values --------------------------------------------------------------


async def test_vector_types_round_trip(obj):
    await obj.write_xyz(0, XYZ(1.5, -2, 3))
    await obj.write_orient(12, Orient(0.25, 0.5, 0.75))
    await obj.write_color(24, Color(10, 20, 30))
    assert tuple(await obj.read_xyz(0)) == (1.5, -2, 3)
    assert tuple(await obj.read_orient(12)) == (0.25, 0.5, 0.75)
    assert tuple(await obj.read_color(24)) == (10, 20, 30)


class Shade(Enum):
    light = 1
    dark = 2


async def test_enums_round_trip(obj):
    await obj.write_enum(0, Shade.dark)
    assert await obj.read_enum(0, Shade) is Shade.dark


async def test_an_unknown_enum_value_is_reported(obj, process):
    process.put(BASE, "i", 9)
    with pytest.raises(ReadingEnumFailed):
        await obj.read_enum(0, Shade)


# --- containers ----------------------------------------------------------------


async def test_reads_a_vector_of_shared_pointers(obj, process):
    process.shared_vector(BASE + 0x40, [0x111, 0x222, 0x333])
    assert await obj.read_shared_vector(0x40) == [0x111, 0x222, 0x333]


async def test_an_empty_or_released_shared_vector_reads_empty(obj, process):
    process.put(BASE + 0x40, "qq", 0x5000, 0x5000)
    process.put(BASE + 0x50, "qq", 0x5000, 0x4000)
    assert await obj.read_shared_vector(0x40) == []
    assert await obj.read_shared_vector(0x50) == []


async def test_a_shared_vector_past_its_bound_is_refused(obj, process):
    process.put(BASE + 0x40, "qq", 0x5000, 0x5000 + 16 * 11)
    with pytest.raises(ValueError):
        await obj.read_shared_vector(0x40, max_size=10)


async def test_reads_a_dynamic_vector_in_one_read(obj, process):
    process.vector(BASE + 0x40, "i", [5, -6, 7])
    assert await obj.read_dynamic_vector(0x40, Primitive.int32) == [5, -6, 7]


async def test_a_dynamic_vector_past_its_bound_is_refused(obj, process):
    process.put(BASE + 0x40, "qq", 0x5000, 0x5000 + 8 * 4)
    with pytest.raises(ValueError):
        await obj.read_dynamic_vector(0x40, max_size=3)


async def test_reads_an_inlined_vector_as_objects(obj, process, hook_handler):
    begin = process.vector(BASE + 0x40, "Q", [0, 0, 0])
    items = await obj.read_inlined_vector(0x40, 8, DynamicMemoryObject)
    assert [await item.read_base_address() for item in items] == [begin, begin + 8, begin + 16]


async def test_an_inlined_vector_past_its_bound_is_refused(obj, process):
    process.put(BASE + 0x40, "QQ", 0x5000, 0x5000 + 0x28 * 6)
    with pytest.raises(ValueError):
        await obj.read_inlined_vector(0x40, 0x28, DynamicMemoryObject, max_size=5)


async def test_reads_a_std_map(obj, process):
    process.std_map(BASE + 0x40, {7: 0x7000, 3: 0x3000, 9: 0x9000})
    mapped = await obj.read_std_map(0x40, DynamicMemoryObject)
    assert {key: value.base_address for key, value in mapped.items()} == {7: 0x7000, 3: 0x3000, 9: 0x9000}


async def test_an_empty_std_map(obj, process):
    process.std_map(BASE + 0x40, {})
    assert await obj.read_std_map(0x40, DynamicMemoryObject) == {}


def _linked_list(process, at: int, objects: list[int]):
    """A std::list: a pointer to its head node and a count; each node links on at +0."""
    head = process.allocate(0x18)
    nodes = [process.allocate(0x18) for _ in objects]
    process.put(head, "Q", nodes[0] if nodes else head)
    for index, (node, value) in enumerate(zip(nodes, objects)):
        following = nodes[index + 1] if index + 1 < len(nodes) else head
        process.put(node, "QQQ", following, 0, value)
    process.put(at, "Qi", head, len(objects))
    return nodes


async def test_reads_a_linked_list_of_inline_objects(obj, process):
    nodes = _linked_list(process, BASE + 0x40, [0, 0])
    assert await obj.read_linked_list(0x40) == [nodes[0] + 16, nodes[1] + 16]


async def test_an_empty_linked_list(obj, process):
    _linked_list(process, BASE + 0x40, [])
    assert await obj.read_linked_list(0x40) == []


async def test_reads_a_linked_list_of_shared_pointers(obj, process):
    _linked_list(process, BASE + 0x40, [0xAAA, 0xBBB, 0xCCC])
    assert await obj.read_shared_linked_list(0x40) == [0xAAA, 0xBBB, 0xCCC]


def _hashset(process, at: int, values: list[int]):
    """The tree read_hashset_basic walks: leaves are flagged at +0x19, values at +0x1C."""
    leaf = process.allocate(0x20)
    process.put(leaf + 0x19, "B", 1)

    def node(value, left, right):
        address = process.allocate(0x20)
        process.put(address, "QQQ", left, 0, right)
        process.put(address + 0x18, "BB", 0, 0)
        process.put(address + 0x1C, "I", value)
        return address

    top = leaf
    for value in values:
        top = node(value, top, leaf)
    root = process.allocate(0x10)
    process.put(root + 8, "Q", top)
    process.put(at, "Q", root)


async def test_reads_a_hash_set(obj, process):
    _hashset(process, BASE + 0x40, [11, 22, 33])
    assert await obj.read_hashset_basic(0x40, Primitive.uint32) == {11, 22, 33}


async def test_a_hash_set_past_its_bound_is_refused(obj, process):
    _hashset(process, BASE + 0x40, list(range(20)))
    with pytest.raises(ValueError):
        await obj.read_hashset_basic(0x40, Primitive.uint32, max_size=5)


# --- type names ---------------------------------------------------------------


class Widget(DynamicMemoryObject, PropertyClass):
    pass


def _type_name_function(process, name: str, *, through_jmp: bool) -> int:
    """A vtable whose first function leads to `name` the way the game's do."""
    body = process.allocate(0x100)
    name_address = process.allocate(0x40)
    process.write_bytes(name_address, name.encode() + b"\x00", len(name) + 1)
    # lea rcx, [rip + offset] at +63; rip is the next instruction, at +70
    process.put(body + 66, "i", name_address - (body + 63 + 7))

    first = body
    if through_jmp:
        first = process.allocate(0x10)
        process.write_bytes(first, b"\xE9" + struct.pack("<i", body - (first + 5)), 5)
    vtable = process.allocate(8)
    process.put(vtable, "Q", first)
    return vtable


@pytest.mark.parametrize("through_jmp", [False, True])
async def test_reads_a_type_name_from_its_vtable(process, hook_handler, through_jmp):
    vtable = _type_name_function(process, "ControlButton", through_jmp=through_jmp)
    process.put(BASE, "Q", vtable)
    assert await Widget(hook_handler, BASE).read_type_name() == "ControlButton"


async def test_a_type_name_is_read_once_per_vtable(process, hook_handler):
    vtable = _type_name_function(process, "Window", through_jmp=False)
    process.put(BASE, "Q", vtable)
    process.put(BASE + 0x100, "Q", vtable)
    assert await Widget(hook_handler, BASE).read_type_name() == "Window"

    # The function is gone, so only the cache can answer
    process.put(vtable, "Q", 0xDEAD0000)
    assert await Widget(hook_handler, BASE + 0x100).read_type_name() == "Window"


async def test_maybe_read_type_name_is_empty_when_unreadable(process, hook_handler):
    process.put(BASE, "Q", 0x4444_0000)
    assert await Widget(hook_handler, BASE).maybe_read_type_name() == ""
