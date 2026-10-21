import asyncio
from types import SimpleNamespace

import pytest

from wizwalker import HookNotReady, MemoryReadError
from wizwalker.client import Client
from wizwalker.memory.memory_objects import duel as duel_module
from wizwalker.memory.memory_objects.duel import CurrentDuel
from wizwalker.memory.memory_objects.enums import DuelPhase

MANAGER_POINTER = 0x1000
MANAGER = 0x2000
MY_ID = 0xABCDEF


class ClientObject:
    def __init__(self, global_id=MY_ID, error: Exception = None):
        self.global_id = global_id
        self.error = error
        self.reads = 0

    async def global_id_full(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.global_id


def make_duel(process, hook_handler, client_object) -> CurrentDuel:
    hook_handler.client = SimpleNamespace(client_object=client_object)
    process.put(MANAGER_POINTER, "Q", MANAGER)
    process.map(MANAGER, size=0x40)
    current = CurrentDuel(hook_handler)
    # Skips the pattern scan for where the manager pointer lives
    current._duel_manager_addr = MANAGER_POINTER
    return current


def add_duels(process, duels: dict[int, list[int]]):
    """Duels by base address, each with its participants' owner ids."""
    for base, owners in duels.items():
        process.map(base, size=0x100)
        participants = []
        for owner in owners:
            participant = process.allocate(0x100)
            process.put(participant + 112, "Q", owner)
            participants.append(participant)
        process.shared_vector(base + 80, participants)
    process.std_map(MANAGER + 8, {i: base for i, base in enumerate(duels)})


async def test_finds_the_duel_this_client_is_in(process, hook_handler):
    current = make_duel(process, hook_handler, ClientObject())
    add_duels(process, {0x10000: [1, 2], 0x20000: [3, MY_ID]})
    assert await current.read_base_address() == 0x20000


async def test_reads_zero_when_in_no_duel(process, hook_handler):
    current = make_duel(process, hook_handler, ClientObject())
    add_duels(process, {0x10000: [1, 2]})
    assert await current.read_base_address() == 0


async def test_retries_a_read_that_fails_mid_change(process, hook_handler):
    client_object = ClientObject()
    current = make_duel(process, hook_handler, client_object)
    add_duels(process, {0x10000: [MY_ID]})

    async def flaky():
        client_object.reads += 1
        if client_object.reads == 1:
            raise MemoryReadError(0)
        return MY_ID

    client_object.global_id_full = flaky
    assert await current.read_base_address() == 0x10000
    assert client_object.reads == 2


async def test_gives_up_on_a_map_that_never_reads(process, hook_handler, monkeypatch):
    # Before the client hook has run, HookNotReady (a MemoryReadError) came back
    # forever, and in_battle() with it.
    monkeypatch.setattr(duel_module, "DUEL_LOOKUP_TIMEOUT", 0.1)
    current = make_duel(process, hook_handler, ClientObject(error=HookNotReady("Client")))
    add_duels(process, {0x10000: [MY_ID]})

    # Bounded here too, so a regression fails rather than hanging the suite
    with pytest.raises(MemoryReadError):
        await asyncio.wait_for(current.read_base_address(), 5)
    assert await asyncio.wait_for(current.duel_phase(), 5) is DuelPhase.ended


async def test_in_battle_answers_false_when_the_duel_never_reads(process, hook_handler, monkeypatch):
    monkeypatch.setattr(duel_module, "DUEL_LOOKUP_TIMEOUT", 0.1)
    current = make_duel(process, hook_handler, ClientObject(error=HookNotReady("Client")))
    add_duels(process, {0x10000: [MY_ID]})
    assert await asyncio.wait_for(Client.in_battle(SimpleNamespace(duel=current)), 5) is False


async def test_in_battle_reads_the_phase(process, hook_handler):
    current = make_duel(process, hook_handler, ClientObject())
    add_duels(process, {0x10000: [MY_ID]})
    process.put(0x10000 + 196, "i", DuelPhase.planning.value)
    assert await current.duel_phase() is DuelPhase.planning
    assert await Client.in_battle(SimpleNamespace(duel=current)) is True
