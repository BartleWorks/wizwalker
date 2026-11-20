"""MemoryReader.pattern_scan's module scans, remembered across clients of one build."""

import asyncio
import itertools
import struct
import threading
from types import SimpleNamespace

import pymem.process
import pytest
import regex

from wizwalker import PatternFailed, PatternMultipleResults
from wizwalker.memory import memory_reader
from wizwalker.memory.memory_reader import MemoryReader

from ..fakes import FakeProcess

EXE = "WizardGraphicalClient.exe"
MODULE_SIZE = 0x3000
BASE = 0x7FF6_1000_0000
# Each client's module at its own base, as ASLR puts them
STRIDE = 0x100_0000

SITE = 0x1100
SITE_BYTES = bytes.fromhex("48 8b 05 11 22 33 44 48 85 c0 74 0a")
SITE_PATTERN = rb"\x48\x8B\x05....\x48\x85\xC0\x74."
ELSEWHERE = 0x2200

BUILD = {"time_date_stamp": 0x6730_1A2B, "checksum": 0x0251_7C3D, "size_of_image": MODULE_SIZE, "entry_point": 0x1000}


def pe_headers(base: int, *, time_date_stamp: int, checksum: int, size_of_image: int, entry_point: int) -> bytes:
    """The headers' page of an x64 image the loader put at `base`."""
    header = bytearray(0x1000)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHI", header, 0x84, 0x8664, 1, time_date_stamp)
    optional = 0x98
    struct.pack_into("<H", header, optional, 0x20B)
    struct.pack_into("<I", header, optional + 16, entry_point)
    # The loader writes the base it chose here, so it differs between clients
    struct.pack_into("<Q", header, optional + 24, base)
    struct.pack_into("<III", header, optional + 56, size_of_image, 0x400, checksum)
    return bytes(header)


def hook(client, at: int):
    """Write a hook's jump over what is at `at`, as installing one does."""
    jump = b"\xE9\x00\x10\x00\x00" + b"\x90" * 2
    client.process.write_bytes(client.base + at, jump, len(jump))


class Clients:
    """Game clients whose module scans are counted, each its own process."""

    def __init__(self):
        self.scans: list[tuple[int, bytes]] = []
        self.modules: dict[int, SimpleNamespace] = {}
        # A client's scan waits on this event, if it has one
        self.blocked: dict[int, threading.Event] = {}
        self._handles = itertools.count(100)

    def add(self, base: int, *, build: dict = None, sites=(SITE,), headers: bool = True) -> SimpleNamespace:
        process = FakeProcess()
        process.process_handle = next(self._handles)
        data = pe_headers(base, **(build or BUILD)) if headers else b""
        process.map(base, data, size=MODULE_SIZE)
        for at in sites:
            process.write_bytes(base + at, SITE_BYTES, len(SITE_BYTES))
        module = SimpleNamespace(lpBaseOfDll=base, SizeOfImage=MODULE_SIZE)
        self.modules[process.process_handle] = module
        return SimpleNamespace(base=base, process=process, reader=MemoryReader(process))

    def scan(self, reader, handle, module, pattern):
        self.scans.append((handle, pattern))
        if (event := self.blocked.get(handle)) is not None:
            event.wait(5)
        image = reader.process.read_bytes(module.lpBaseOfDll, module.SizeOfImage)
        return [module.lpBaseOfDll + m.start() for m in regex.finditer(pattern, image, regex.DOTALL)]


@pytest.fixture
def clients(monkeypatch) -> Clients:
    clients = Clients()

    def scan(self, handle, module, pattern):
        return clients.scan(self, handle, module, pattern)

    monkeypatch.setattr(pymem.process, "module_from_name", lambda handle, name: clients.modules[handle])
    monkeypatch.setattr(MemoryReader, "_scan_entire_module", scan)
    return clients


async def test_a_second_client_of_the_build_gets_its_own_address_without_a_scan(clients):
    first, second = clients.add(BASE), clients.add(BASE + STRIDE)

    assert await first.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + SITE
    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + STRIDE + SITE
    assert len(clients.scans) == 1


@pytest.mark.parametrize("field", ["time_date_stamp", "checksum", "size_of_image", "entry_point"])
async def test_clients_of_different_builds_never_share_a_result(clients, field):
    # The pattern sits at the same offset in both, so only the build keeps them apart
    other_build = {**BUILD, field: BUILD[field] + 0x10}
    first, second = clients.add(BASE), clients.add(BASE + STRIDE, build=other_build)

    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)
    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + STRIDE + SITE
    assert len(clients.scans) == 2


async def test_the_module_name_is_part_of_the_key(clients):
    first, second = clients.add(BASE), clients.add(BASE + STRIDE)

    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)
    await second.reader.pattern_scan(SITE_PATTERN, module="Other.dll")
    assert len(clients.scans) == 2


async def test_a_hooked_site_is_scanned_for_again_and_the_real_answer_returned(clients):
    first = clients.add(BASE)
    # Hooked at the cached site, with another match the real scan finds
    second = clients.add(BASE + STRIDE, sites=(SITE, ELSEWHERE))
    hook(second, SITE)
    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)

    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + STRIDE + ELSEWHERE
    assert len(clients.scans) == 2


async def test_a_hooked_site_with_nothing_else_fails_as_it_always_has(clients):
    first, second = clients.add(BASE), clients.add(BASE + STRIDE)
    hook(second, SITE)
    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)

    with pytest.raises(PatternFailed):
        await second.reader.pattern_scan(SITE_PATTERN, module=EXE)


async def test_one_clients_different_answer_does_not_replace_the_entry(clients):
    first = clients.add(BASE)
    odd = clients.add(BASE + STRIDE, sites=(SITE, ELSEWHERE))
    hook(odd, SITE)
    third = clients.add(BASE + 2 * STRIDE)

    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)
    await odd.reader.pattern_scan(SITE_PATTERN, module=EXE)
    assert await third.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + 2 * STRIDE + SITE
    assert len(clients.scans) == 2


async def test_the_same_client_unhooked_gets_its_hit_back(clients):
    client = clients.add(BASE)
    original = client.process.read_bytes(BASE + SITE, 7)
    await client.reader.pattern_scan(SITE_PATTERN, module=EXE)

    hook(client, SITE)
    with pytest.raises(PatternFailed):
        await client.reader.pattern_scan(SITE_PATTERN, module=EXE)
    client.process.write_bytes(BASE + SITE, original, 7)

    assert await client.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + SITE
    assert len(clients.scans) == 2


async def test_a_pattern_failed_is_not_cached(clients):
    client = clients.add(BASE, sites=())
    with pytest.raises(PatternFailed):
        await client.reader.pattern_scan(SITE_PATTERN, module=EXE)

    client.process.write_bytes(BASE + SITE, SITE_BYTES, len(SITE_BYTES))
    assert await client.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + SITE
    assert len(clients.scans) == 2


async def test_multiple_results_for_one_asked_for_are_not_cached(clients):
    client = clients.add(BASE, sites=(SITE, ELSEWHERE))
    for _ in range(2):
        with pytest.raises(PatternMultipleResults):
            await client.reader.pattern_scan(SITE_PATTERN, module=EXE)
    assert len(clients.scans) == 2


async def test_return_multiple_results_round_trip_to_each_clients_base(clients):
    first = clients.add(BASE, sites=(SITE, ELSEWHERE))
    second = clients.add(BASE + STRIDE, sites=(SITE, ELSEWHERE))

    assert await first.reader.pattern_scan(SITE_PATTERN, module=EXE, return_multiple=True) == [
        BASE + SITE,
        BASE + ELSEWHERE,
    ]
    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE, return_multiple=True) == [
        BASE + STRIDE + SITE,
        BASE + STRIDE + ELSEWHERE,
    ]
    assert len(clients.scans) == 1


async def test_a_return_multiple_hit_with_one_result_patched_is_scanned_for_again(clients):
    first = clients.add(BASE, sites=(SITE, ELSEWHERE))
    second = clients.add(BASE + STRIDE, sites=(SITE, ELSEWHERE))
    hook(second, ELSEWHERE)

    await first.reader.pattern_scan(SITE_PATTERN, module=EXE, return_multiple=True)
    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE, return_multiple=True) == [
        BASE + STRIDE + SITE
    ]
    assert len(clients.scans) == 2


async def test_return_multiple_is_part_of_the_key(clients):
    first, second = clients.add(BASE), clients.add(BASE + STRIDE)

    assert await first.reader.pattern_scan(SITE_PATTERN, module=EXE, return_multiple=True) == [BASE + SITE]
    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + STRIDE + SITE
    assert len(clients.scans) == 2


async def test_concurrent_asks_for_one_pattern_do_one_scan(clients):
    readers = [clients.add(BASE + index * STRIDE) for index in range(4)]

    found = await asyncio.gather(*(c.reader.pattern_scan(SITE_PATTERN, module=EXE) for c in readers))

    assert found == [BASE + index * STRIDE + SITE for index in range(4)]
    assert len(clients.scans) == 1


async def test_concurrent_asks_after_a_failed_scan_each_scan_for_themselves(clients):
    hooked = clients.add(BASE)
    hook(hooked, SITE)
    others = [clients.add(BASE + index * STRIDE) for index in (1, 2)]

    found = await asyncio.gather(
        *(c.reader.pattern_scan(SITE_PATTERN, module=EXE) for c in [hooked, *others]),
        return_exceptions=True,
    )

    assert isinstance(found[0], PatternFailed)
    assert found[1:] == [BASE + STRIDE + SITE, BASE + 2 * STRIDE + SITE]


async def test_a_cancelled_scan_lets_the_clients_waiting_on_it_scan_for_themselves(clients):
    leader = clients.add(BASE)
    release = clients.blocked[leader.process.process_handle] = threading.Event()
    others = [clients.add(BASE + index * STRIDE) for index in (1, 2)]

    try:
        leading = asyncio.create_task(leader.reader.pattern_scan(SITE_PATTERN, module=EXE))
        while not clients.scans:
            await asyncio.sleep(0.001)
        waiting = [asyncio.create_task(c.reader.pattern_scan(SITE_PATTERN, module=EXE)) for c in others]
        await asyncio.sleep(0)

        leading.cancel()
        found = await asyncio.wait_for(asyncio.gather(*waiting), 5)
    finally:
        release.set()

    assert found == [BASE + STRIDE + SITE, BASE + 2 * STRIDE + SITE]
    assert leading.cancelled()
    # Each scans for itself, unless the second wakes after the first has remembered
    # what it found, and checks that instead
    assert len(clients.scans) in (2, 3)


def test_clients_on_different_event_loops_share_the_cache(clients):
    first, second = clients.add(BASE), clients.add(BASE + STRIDE)

    assert asyncio.run(first.reader.pattern_scan(SITE_PATTERN, module=EXE)) == BASE + SITE
    assert asyncio.run(second.reader.pattern_scan(SITE_PATTERN, module=EXE)) == BASE + STRIDE + SITE
    assert len(clients.scans) == 1


@pytest.mark.parametrize(
    "pattern, code, found_at",
    [
        # As RenderContextHook's and GameClient's freecam patterns look behind
        (rb"(?<=\xFF\x50\x10\x84\xC0)\x74.", bytes.fromhex("ff 50 10 84 c0 74 05"), 5),
        (rb"\xFF\x50\x18\x84\xC0(?=\x74)", bytes.fromhex("ff 50 18 84 c0 74 05"), 0),
    ],
    ids=["lookbehind", "lookahead"],
)
async def test_a_pattern_that_looks_around_its_match_is_checked_with_the_context(clients, pattern, code, found_at):
    first, second = clients.add(BASE, sites=()), clients.add(BASE + STRIDE, sites=())
    for client in (first, second):
        client.process.write_bytes(client.base + SITE, code, len(code))

    assert await first.reader.pattern_scan(pattern, module=EXE) == BASE + SITE + found_at
    assert await second.reader.pattern_scan(pattern, module=EXE) == BASE + STRIDE + SITE + found_at
    assert len(clients.scans) == 1


async def test_a_lookbehind_whose_context_changed_is_scanned_for_again(clients):
    pattern = rb"(?<=\xFF\x50\x10\x84\xC0)\x74."
    code = bytes.fromhex("ff 50 10 84 c0 74 05")
    first, second = clients.add(BASE, sites=()), clients.add(BASE + STRIDE, sites=())
    for client in (first, second):
        client.process.write_bytes(client.base + SITE, code, len(code))
    # Only the bytes before the match differ
    second.process.write_bytes(second.base + SITE, b"\xCC", 1)
    await first.reader.pattern_scan(pattern, module=EXE)

    with pytest.raises(PatternFailed):
        await second.reader.pattern_scan(pattern, module=EXE)


async def test_a_pattern_with_more_results_than_are_worth_checking_is_scanned_for_each_time(clients):
    count = memory_reader._MAX_CACHED_RESULTS + 1
    first, second = clients.add(BASE, sites=()), clients.add(BASE + STRIDE, sites=())
    for client in (first, second):
        client.process.write_bytes(client.base + SITE, b"\xCC" * count, count)

    for client in (first, second):
        assert len(await client.reader.pattern_scan(rb"\xCC", module=EXE, return_multiple=True)) == count
    assert len(clients.scans) == 2


async def test_a_module_whose_headers_tell_no_build_is_scanned_for_each_time(clients):
    first, second = clients.add(BASE, headers=False), clients.add(BASE + STRIDE, headers=False)

    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)
    assert await second.reader.pattern_scan(SITE_PATTERN, module=EXE) == BASE + STRIDE + SITE
    assert len(clients.scans) == 2


async def test_a_whole_process_scan_is_never_cached(clients, monkeypatch):
    client = clients.add(BASE)
    heap_scans = []

    def scan_all(self, handle, pattern, return_multiple=False):
        heap_scans.append(pattern)
        return [0x2000_0000]

    monkeypatch.setattr(MemoryReader, "_scan_all", scan_all)

    for _ in range(2):
        assert await client.reader.pattern_scan(SITE_PATTERN) == 0x2000_0000
    assert len(heap_scans) == 2
    assert not memory_reader._MODULE_SCANS


async def test_clearing_the_cache_forgets_every_result(clients):
    first, second = clients.add(BASE), clients.add(BASE + STRIDE)
    await first.reader.pattern_scan(SITE_PATTERN, module=EXE)

    memory_reader.clear_module_scan_cache()

    await second.reader.pattern_scan(SITE_PATTERN, module=EXE)
    assert len(clients.scans) == 2


async def test_the_cache_keeps_only_the_most_recent_keys(clients, monkeypatch):
    monkeypatch.setattr(memory_reader, "_MODULE_SCAN_LIMIT", 2)
    client = clients.add(BASE)
    patterns = [SITE_PATTERN, rb"\x48\x8B\x05", rb"\x48\x85\xC0"]

    for pattern in patterns:
        await client.reader.pattern_scan(pattern, module=EXE)

    assert [key[2] for key in memory_reader._MODULE_SCANS] == patterns[1:]
