import pytest

from wizwalker import MemoryReadError
from wizwalker.memory.memory_objects.deck_behavior import ClientDeckBehavior

DECK = 0x50000
# The behavior's bytes up to and past the name, as the live one is allocated.
SIZE = 0x100


async def test_a_decks_name_is_three_wheel_indices_past_its_reflected_fields(process, hook_handler):
    # A deck named "Fire Vanguard Deck 1" on r814353: Class_3, Type_37, Num_1
    process.map(DECK, size=SIZE)
    process.put(DECK + 0xF0, "III", 3, 37, 1)

    assert await ClientDeckBehavior(hook_handler, DECK).name_indices() == (3, 37, 1)


async def test_a_deck_never_named_reads_every_wheel_empty(process, hook_handler):
    process.map(DECK, size=SIZE)

    assert await ClientDeckBehavior(hook_handler, DECK).name_indices() == (0, 0, 0)


async def test_the_bytes_beside_the_name_are_not_part_of_it(process, hook_handler):
    process.map(DECK, bytes([0xFF]) * SIZE)
    process.put(DECK + 0xF0, "III", 0, 2, 0)

    assert await ClientDeckBehavior(hook_handler, DECK).name_indices() == (0, 2, 0)


async def test_a_freed_decks_name_is_a_read_error(process, hook_handler):
    with pytest.raises(MemoryReadError):
        await ClientDeckBehavior(hook_handler, DECK).name_indices()
